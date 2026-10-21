#!/usr/bin/env python3
"""Title covers for article EPUBs: the article's lead image (when there is one)
over the title, site and date, sized for the X4 Pro's e-ink screen.

make_cover(art) returns (jpeg_bytes, "jpg", "image/jpeg"), the same shape as
instapaper_to_epub.fetch_cover(). art["cover"] is the raw lead image from
fetch_cover() or None. Output is a BASELINE RGB JPEG: the reader's decoder is
on an ESP32, and progressive JPEGs are the usual thing such decoders refuse.

Pillow + system fonts (Debian packages fonts-liberation and fonts-lato).
"""
import io
import re
import time

from PIL import Image, ImageDraw, ImageFont, ImageOps

W, H = 600, 900                 # 2:3, scales cleanly to the 480x800 panel
MARGIN = 40
IMAGE_H = 520                   # lead image band when there is an image
SERIF_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"
SANS = "/usr/share/fonts/truetype/lato/Lato-Medium.ttf"
SANS_BOLD = "/usr/share/fonts/truetype/lato/Lato-Bold.ttf"
INK, PAPER, GREY = (0, 0, 0), (255, 255, 255), (90, 90, 90)


def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _wrap(draw, text, font, width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _fit_title(draw, title, width, height, sizes, max_lines):
    """Largest font size whose wrapped title fits the box; ellipsises at the end."""
    for size in sizes:
        font = _font(SERIF_BOLD, size)
        lines = _wrap(draw, title, font, width)
        lh = int(size * 1.15)
        if len(lines) <= max_lines and len(lines) * lh <= height:
            return font, lines, lh
    font = _font(SERIF_BOLD, sizes[-1])
    lh = int(sizes[-1] * 1.15)
    lines = _wrap(draw, title, font, width)
    keep = max(1, min(max_lines, height // lh))
    if len(lines) > keep:
        lines = lines[:keep]
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > width:
            lines[-1] = lines[-1].rsplit(" ", 1)[0] if " " in lines[-1] else lines[-1][:-1]
        lines[-1] += "…"
    return font, lines, lh


def _lead_image(raw):
    """Greyscale, centre-cropped to the image band, contrast lifted for e-ink."""
    if not raw:
        return None
    try:
        img = Image.open(io.BytesIO(raw[0]))
        img = ImageOps.exif_transpose(img)
        if img.mode in ("P", "LA", "RGBA"):
            # Flatten transparency onto white (palette PNGs warn otherwise).
            img = img.convert("RGBA")
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            img = Image.alpha_composite(bg, img)
        img = img.convert("L")
    except Exception:
        return None
    if img.width < 200 or img.height < 120:   # icons and trackers make bad covers
        return None
    img = ImageOps.fit(img, (W, IMAGE_H), Image.LANCZOS, centering=(0.5, 0.4))
    img = ImageOps.autocontrast(img, cutoff=1)
    return img.convert("RGB")


def _date_label(iso):
    try:
        return time.strftime("%b %-d, %Y", time.strptime(iso[:10], "%Y-%m-%d"))
    except (TypeError, ValueError):
        return ""


def make_cover(art):
    title = re.sub(r"\s+", " ", art.get("title") or "Untitled").strip()
    source = "FEEDBIN" if str(art.get("id", "")).startswith("fb") else "INSTAPAPER"
    byline = " · ".join(x for x in (art.get("site") or "", _date_label(art.get("date") or "")) if x)
    author = (art.get("author") or "").strip()

    page = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(page)
    text_w = W - 2 * MARGIN
    lead = _lead_image(art.get("cover"))

    meta_font = _font(SANS, 24)
    label_font = _font(SANS_BOLD, 20)
    meta_h = 34 * (2 if author else 1)

    if lead:
        page.paste(lead, (0, 0))
        top = IMAGE_H + 28
        d.rectangle([MARGIN, top, MARGIN + 60, top + 5], fill=INK)
        top += 26
        box_h = H - top - MARGIN - meta_h - 12
        font, lines, lh = _fit_title(d, title, text_w, box_h, range(46, 25, -2), 5)
    else:
        # Typographic cover: source label, big title, meta at the foot.
        d.rectangle([0, 0, W, 14], fill=INK)
        d.text((MARGIN, 52), source, font=label_font, fill=INK)
        d.rectangle([MARGIN, 88, MARGIN + 60, 93], fill=INK)
        top = 130
        box_h = H - top - MARGIN - meta_h - 40
        font, lines, lh = _fit_title(d, title, text_w, box_h, range(72, 33, -3), 9)

    for i, line in enumerate(lines):
        d.text((MARGIN, top + i * lh), line, font=font, fill=INK)

    y = H - MARGIN - meta_h
    if author:
        d.text((MARGIN, y), author, font=meta_font, fill=GREY)
        y += 34
    if byline:
        d.text((MARGIN, y), byline, font=meta_font, fill=GREY)

    out = io.BytesIO()
    page.save(out, "JPEG", quality=85, progressive=False, optimize=True)
    return out.getvalue(), "jpg", "image/jpeg"
