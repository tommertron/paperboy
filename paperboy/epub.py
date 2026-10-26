"""Article HTML -> clean EPUB 3, sized for small e-ink readers.

Sanitizes article HTML to a safe XHTML subset, embeds the article's images as
greyscale baseline JPEGs (CrossPoint decodes progressive JPEGs at 1/8 scale and
has no WebP decoder), and adds a title cover (see covers.py).
"""
import html
import os
import re
import time
import zipfile
from html.parser import HTMLParser
from urllib.parse import urljoin

from . import covers
from .log import log

UA = "Mozilla/5.0 (compatible; Paperboy; +https://coefficiencies.com/apps/paperboy/)"

def clean_text(s):
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()



IMG_EXT_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp",
}
# src substrings that mark tracking pixels / avatars / chrome, not lead images.
IMG_SKIP = ("pixel", "1x1", "spacer", "blank", "tracking", "beacon", "avatar",
            "gravatar", "logo", "icon", "emoji", "badge", "button", "share",
            "feed-", ".svg", "doubleclick", "smiley")


def _img_src(attrs):
    """Best src from an <img> tag's attrs (prefer data-src / lazy attrs)."""
    d = dict(attrs)
    for k in ("data-src", "data-original", "data-lazy-src", "src"):
        v = (d.get(k) or "").strip()
        if v:
            return v, d
    return "", d


def extract_lead_image(story_html, base_url):
    """First plausible content image URL in the parsed article, or ''."""
    for m in re.finditer(r"<img\b[^>]*>", story_html or "", re.I):
        attrs = []
        for am in re.finditer(r'([\w:-]+)\s*=\s*"([^"]*)"', m.group(0)):
            attrs.append((am.group(1).lower(), am.group(2)))
        src, d = _img_src(attrs)
        if not src or src.startswith("data:"):
            continue
        low = src.lower()
        if any(bad in low for bad in IMG_SKIP):
            continue
        # skip obviously tiny images if dimensions are declared inline
        try:
            if int(d.get("width", "999")) < 200 or int(d.get("height", "999")) < 150:
                continue
        except ValueError:
            pass
        return urljoin(base_url or "", html.unescape(src))
    return ""


def fetch_cover(s, img_url):
    """Download a lead image -> (bytes, ext, mime) or None. Never raises."""
    if not img_url:
        return None
    try:
        r = s.get(img_url, timeout=20, headers={"Referer": img_url})
        if r.status_code != 200 or not r.content:
            return None
        data = r.content
        if len(data) < 5000 or len(data) > 6_000_000:  # skip icons / oversize
            return None
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
               "image/webp": "webp"}.get(ctype)
        if not ext:  # fall back to the URL extension
            m = re.search(r"\.(jpe?g|png|gif|webp)(?:\?|$)", img_url.lower())
            ext = "jpg" if (m and m.group(1) == "jpeg") else (m.group(1) if m else None)
        if ext not in IMG_EXT_MIME:
            return None
        # sanity-check magic bytes for the common raster types
        sig_ok = (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"
                  or data[:6] in (b"GIF87a", b"GIF89a") or data[8:12] == b"WEBP")
        if not sig_ok:
            return None
        return data, ext, IMG_EXT_MIME[ext]
    except Exception:
        return None



ALLOWED = {"p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
           "blockquote", "pre", "code", "em", "strong", "i", "b", "u", "a",
           "figure", "figcaption", "table", "thead", "tbody", "tr", "td", "th",
           "span", "sub", "sup", "div"}
VOID = {"br", "hr"}
DROP_CONTENT = {"script", "style", "iframe", "svg", "noscript", "form", "button",
                "video", "audio", "object", "embed", "head"}


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def esc_attr(s):
    return esc(html.unescape(s)).replace('"', "&quot;")


class Sanitizer(HTMLParser):
    def __init__(self, images=None):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []
        self.skip = 0
        # When a list is passed, <img> becomes a placeholder and its URL is
        # collected for embed_images(); otherwise the old alt-text caption.
        self.images = images

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_CONTENT:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "img":
            if self.images is not None:
                src, d = _img_src(attrs)
                if (not src or src.startswith("data:")) and d.get("srcset"):
                    src = d["srcset"].split(",")[-1].strip().split(" ")[0]
                if src and not src.startswith("data:") and \
                        not any(b in src.lower() for b in INLINE_IMG_SKIP):
                    self.out.append(IMG_TOKEN % len(self.images))
                    self.images.append(html.unescape(src))
                return
            alt = dict(attrs).get("alt", "")
            if alt:
                self.out.append("<p><em>%s</em></p>" % esc(html.unescape(alt)))
            return
        if tag not in ALLOWED:
            return  # transparent: drop the tag, keep its children
        a = ""
        if tag == "a":
            href = dict(attrs).get("href", "")
            if href:
                a = ' href="%s"' % esc_attr(href)
        if tag in VOID:
            self.out.append("<%s%s/>" % (tag, a))
        else:
            self.out.append("<%s%s>" % (tag, a))
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in VOID:
            self.handle_starttag(tag, attrs)
        else:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_CONTENT:
            if self.skip:
                self.skip -= 1
            return
        if self.skip or tag in VOID or tag not in ALLOWED:
            return
        if tag in self.stack:
            while self.stack:
                top = self.stack.pop()
                self.out.append("</%s>" % top)
                if top == tag:
                    break

    def handle_data(self, data):
        if self.skip:
            return
        self.out.append(esc(data))

    def result(self):
        while self.stack:
            self.out.append("</%s>" % self.stack.pop())
        s = "".join(self.out)
        s = re.sub(r"(?:\s*<p>\s*</p>\s*)+", "", s)  # drop empty paragraphs
        return s.strip()


def sanitize_html(fragment):
    if not fragment:
        return ""
    p = Sanitizer()
    p.feed(fragment)
    return p.result()


# ---------- inline images ----------
# The reader (CrossPoint on an ESP32) decodes baseline JPEG at full size but
# progressive JPEG at 1/8 scale, and has no WebP decoder, so every image is
# re-encoded as a small greyscale baseline JPEG whatever it arrived as.

IMG_TOKEN = "\x00IMG%d\x00"
INLINE_IMG_SKIP = ("pixel", "1x1", "spacer", "tracking", "beacon", "doubleclick",
                   "gravatar", "avatar", "emoji", ".svg", "feeds.feedburner")
INLINE_MAX = (600, 800)
INLINE_MIN = (100, 60)        # smaller than this is an icon or a tracker
MAX_INLINE_IMAGES = 40


def _inline_jpeg(data):
    """Raw image bytes -> greyscale baseline JPEG bytes, or None to drop it."""
    from io import BytesIO
    from PIL import Image, ImageOps
    try:
        img = Image.open(BytesIO(data))
        img.seek(0)  # first frame of a GIF
        img = ImageOps.exif_transpose(img)
    except Exception:
        return None
    if img.width < INLINE_MIN[0] or img.height < INLINE_MIN[1]:
        return None
    if img.mode in ("P", "LA", "RGBA"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    img = img.convert("L")
    img.thumbnail(INLINE_MAX, Image.LANCZOS)
    out = BytesIO()
    img.save(out, "JPEG", quality=80, progressive=False, optimize=True)
    return out.getvalue()


def embed_images(body, urls, base_url, session):
    """Swap IMG_TOKEN placeholders for embedded images.
    Returns (body, [(href, jpeg_bytes)]); failed images vanish without a trace."""
    from concurrent.futures import ThreadPoolExecutor

    def fetch(u):
        try:
            u = urljoin(base_url or "", u)
            r = session.get(u, timeout=15, headers={"Referer": base_url or u})
            if r.status_code != 200 or not r.content or len(r.content) > 8_000_000:
                return None
            return _inline_jpeg(r.content)
        except Exception:
            return None

    urls = urls[:MAX_INLINE_IMAGES]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(fetch, urls))
    images, seen = [], {}
    for n, jpeg in enumerate(results):
        tag = ""
        if jpeg:
            if jpeg not in seen:
                seen[jpeg] = "images/img%03d.jpg" % len(images)
                images.append((seen[jpeg], jpeg))
            tag = '<img src="%s" alt=""/>' % seen[jpeg]
        body = body.replace(IMG_TOKEN % n, tag)
    body = re.sub("\x00IMG\\d+\x00", "", body)  # beyond MAX_INLINE_IMAGES
    body = re.sub(r"(?:\s*<p>\s*</p>\s*)+", "", body)
    return body, images


def sanitize_with_images(fragment, base_url, session):
    """sanitize_html(), but with the article's images embedded."""
    if not fragment:
        return "", []
    urls = []
    p = Sanitizer(images=urls)
    p.feed(fragment)
    return embed_images(p.result(), urls, base_url, session)




CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:paperboy:{id}</dc:identifier>
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    <dc:language>en</dc:language>
    <dc:source>{url}</dc:source>
    <dc:date>{date}</dc:date>
    <meta property="dcterms:modified">{moddate}</meta>{cover_meta}
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>{cover_manifest}
    <item id="article" href="article.xhtml" media-type="application/xhtml+xml"/>
    <item id="end" href="end.xhtml" media-type="application/xhtml+xml"/>{image_manifest}
  </manifest>
  <spine>{cover_spine}
    <itemref idref="article"/>
    <itemref idref="end"/>
  </spine>
</package>
"""

COVER_PAGE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Cover</title>
  <style>
    html, body {{ margin: 0; padding: 0; height: 100%; text-align: center; }}
    img {{ max-width: 100%; max-height: 100vh; object-fit: contain; }}
  </style>
</head>
<body><div><img src="{img}" alt="{title}"/></div></body>
</html>
"""

NAV = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">
<head><meta charset="utf-8"/><title>{title}</title></head>
<body>
  <nav epub:type="toc" id="toc"><h1>Contents</h1>
    <ol><li><a href="article.xhtml">{title}</a></li></ol>
  </nav>
</body>
</html>
"""

# A last page of its own. The reader reports progress from the top of the
# current page, so the article's own last page reads as (n-1)/n of it: a
# 12-page piece tops out at 92%. Turning onto this page is what "finished"
# means; finish_at() works out the percent it starts at.
END = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
<head><meta charset="utf-8"/><title>The end</title>
<style>body {{ font-family: Georgia, serif; text-align: center; margin-top: 30%; }}</style></head>
<body><p>&#8212; The end &#8212;</p><p><small>Leave the book now and it&#8217;s marked done.</small></p></body>
</html>
"""
ARTICLE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{title}</title>
  <style>
    body {{ font-family: Georgia, 'Times New Roman', serif; line-height: 1.5; margin: 1em; }}
    h1 {{ font-size: 1.4em; line-height: 1.2; }}
    .meta {{ color: #555; font-size: 0.85em; margin: 0 0 1.5em; }}
    .meta a {{ color: #555; }}
    img {{ max-width: 100%; height: auto; }}
    blockquote {{ margin: 1em; padding-left: 1em; border-left: 3px solid #ccc; }}
    pre {{ white-space: pre-wrap; }}
    figure {{ margin: 1em 0; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <p class="meta">{meta}</p>
  {body}
</body>
</html>
"""


def slugify(s):
    s = re.sub(r"[^\w\s-]", "", s.lower()).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:60].strip("-") or "article"


def finish_at(path):
    """Percent the reader reports on reaching the end page, or None for an
    EPUB without one. CrossPoint weighs each spine item by its uncompressed
    size, so this is the share of the book that comes before end.xhtml."""
    try:
        with zipfile.ZipFile(path) as z:
            sizes = {i.filename: i.file_size for i in z.infolist()}
    except (OSError, zipfile.BadZipFile):
        return None
    spine = [f"OEBPS/{n}" for n in ("cover.xhtml", "article.xhtml", "end.xhtml")
             if f"OEBPS/{n}" in sizes]
    if not spine or spine[-1] != "OEBPS/end.xhtml":
        return None
    total = sum(sizes[n] for n in spine)
    return int(100 * (total - sizes[spine[-1]]) / total)


def build_epub(art, path):
    title_t = esc(art["title"])
    # library_author overrides only the metadata the reader's Library sorts on;
    # the cover and the article page keep the real byline.
    author_t = esc(art.get("library_author") or art["author"] or art["site"] or "Paperboy")
    moddate = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    parts = []
    if art["author"]:
        parts.append(esc(art["author"]))
    if art["site"]:
        src = esc(art["site"])
        if art["url"]:
            src = '<a href="%s">%s</a>' % (esc_attr(art["url"]), src)
        parts.append(src)
    meta = " &#183; ".join(parts) if parts else (
        '<a href="%s">Read original</a>' % esc_attr(art["url"]) if art["url"] else "")

    body = art["body"] or (
        '<p><em>No article text was available.</em></p>'
        + ('<p><a href="%s">Read it online</a></p>' % esc_attr(art["url"]) if art["url"] else ""))

    article_x = ARTICLE.format(title=title_t, meta=meta, body=body)

    # Title cover (lead image + title/site/date), falling back to the bare
    # lead image if Pillow or the fonts are unavailable. See covers.py.
    try:
        cover = covers.make_cover(art)
    except Exception as ex:
        log(f"cover failed ({ex!r}); using plain lead image")
        cover = art.get("cover")
    cover_meta = cover_manifest = cover_spine = ""
    cover_page = cover_name = cover_bytes = None
    if cover:
        cover_bytes, ext, mime = cover
        cover_name = "cover." + ext
        cover_meta = '\n    <meta name="cover" content="cover-img"/>'
        cover_manifest = (
            '\n    <item id="cover-img" href="%s" media-type="%s" properties="cover-image"/>'
            '\n    <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>'
            % (cover_name, mime))
        cover_spine = '\n    <itemref idref="cover" linear="yes"/>'
        cover_page = COVER_PAGE.format(img=cover_name, title=title_t)

    opf = OPF.format(id=art["id"], title=title_t, author=author_t,
                     url=esc(art["url"]), date=art.get("date", ""), moddate=moddate,
                     cover_meta=cover_meta, cover_manifest=cover_manifest,
                     cover_spine=cover_spine,
                     image_manifest="".join(
                         '\n    <item id="img%d" href="%s" media-type="image/jpeg"/>' % (i, h)
                         for i, (h, _) in enumerate(art.get("images") or [])))
    nav = NAV.format(title=title_t)

    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/nav.xhtml", nav)
        if cover_bytes is not None:
            z.writestr("OEBPS/" + cover_name, cover_bytes, compress_type=zipfile.ZIP_STORED)
            z.writestr("OEBPS/cover.xhtml", cover_page)
        z.writestr("OEBPS/article.xhtml", article_x)
        z.writestr("OEBPS/end.xhtml", END.format())
        for href, data in art.get("images") or []:
            z.writestr("OEBPS/" + href, data, compress_type=zipfile.ZIP_STORED)
    os.replace(tmp, path)

