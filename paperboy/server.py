"""Paperboy server: hands a CrossPoint reader's plugin plain JSON and ready-made EPUBs.

CrossPoint's on-device plugins are declarative and can't sign OAuth requests,
so this does the Instapaper/Feedbin side.

  GET  /health                       no auth; "ok"
  GET  /lists?token=T                the reader's list picker (one entry per source)
  GET  /list/<source>?page=&limit=   a source's articles, paged the way the firmware asks
                                     (limit = page_size + 1); page 1 starts with Sync all
  GET  /unread, /starred             aliases for /list/instapaper, /list/feedbin
  GET  /f/<token>/<file>             a file by its on-card name (<Name>.epub or
                                     <Name>.epub.meta.json); what bundle downloads fetch
  GET  /epub/<id>                    one article's EPUB
  POST /progress                     reader event {"id","percent","event_id"}; once
                                     percent >= archive_at, the article is archived
                                     (Instapaper) or unstarred (Feedbin)

Auth is "Authorization: Bearer <token>" except where the firmware can't send a
header: /lists and bundle files carry the token in the URL, and log lines
redact it.

The reader reports EVERY book it closes, and its event queue stops at the first
non-2xx. So /progress answers 200 for anything it can't or needn't act on and
only returns 5xx for failures worth retrying.
"""
import hmac
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, config, epub, reader_sync
from .feedbin import Feedbin
from .instapaper import Instapaper
from .log import log

LIST_TTL = 60          # seconds to reuse a source's list
MAX_PAGE = 16          # firmware page_size is 1..16
WARM_EVERY = 600       # seconds between pre-build passes
EPUB_VERSION = "v3"    # bump when the EPUB design changes; old cache entries go unused
SYNC_ID = "sync-all"
SIDECAR_KEY = "paperboy_id"
LEGACY_SIDECAR_KEYS = ("instapaper_id",)
LIST_TITLES = {"instapaper": "Instapaper", "feedbin": "Feedbin starred"}


def file_stem(title):
    """Readable, URL-safe on-card name. Bundle files are fetched as base + name
    with no escaping, so only [A-Za-z0-9-] survive."""
    s = re.sub(r"['’]", "", title)
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    return s[:70].rstrip("-") or "Article"


class Library:
    """The reading list across sources, plus the EPUB cache and finished ids."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.sources = {}
        if cfg.get("instapaper"):
            self.sources["instapaper"] = Instapaper(cfg["instapaper"])
        if cfg.get("feedbin"):
            self.sources["feedbin"] = Feedbin(cfg["feedbin"])
        self.cache_dir = config.data_path("cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.finished_path = config.data_path("finished.json")
        self._lock = threading.Lock()
        self._lists = {}  # source -> (fetched_at, items)

    # --- lists ---

    def source_of(self, item_id):
        return "feedbin" if item_id.startswith("fb") else "instapaper"

    def items(self, source, force=False):
        with self._lock:
            hit = self._lists.get(source)
            if hit and not force and time.time() - hit[0] < LIST_TTL:
                return hit[1]
        items = self.sources[source].items()
        for i in items:
            i["title"] = epub.clean_text(i["title"]) or f"Article {i['id']}"
            i["description"] = epub.clean_text(i["description"])
            try:
                label = time.strftime("%b %-d", time.strptime(i["date"], "%Y-%m-%d"))
            except ValueError:
                label = ""
            i["subtitle"] = " · ".join(x for x in (i["site"], label) if x)
        with self._lock:
            self._lists[source] = (time.time(), items)
        return items

    def forget_lists(self):
        with self._lock:
            self._lists.clear()

    def catalog(self):
        """Every article that belongs on the card, each with a unique on-card
        "name". Raises if any source can't be read, so callers never mistake an
        outage for an empty reading list."""
        items = [dict(i) for s in self.sources for i in self.items(s)]
        seen = set()
        for i in items:
            name = file_stem(i["title"])
            if name.lower() in seen:
                name = f"{name}-{i['id']}"
            seen.add(name.lower())
            i["name"] = name
        return items

    # --- EPUBs ---

    def cache_path(self, item_id):
        return os.path.join(self.cache_dir, f"{item_id}.{EPUB_VERSION}.epub")

    def cached(self, item_id):
        return os.path.exists(self.cache_path(item_id))

    def epub_path(self, item_id):
        path = self.cache_path(item_id)
        if os.path.exists(path):
            return path
        source = self.source_of(item_id)
        if source not in self.sources:
            return None
        item = next((i for i in self.items(source) if i["id"] == item_id), None)
        if item is None:
            item = next((i for i in self.items(source, force=True) if i["id"] == item_id), None)
        if item is None:
            return None
        import requests
        s = requests.Session()
        s.headers.update({"User-Agent": epub.UA})
        html, lead, author = self.sources[source].article_html(item)
        body, images = epub.sanitize_with_images(html, item["url"], s)
        cover_url = lead or epub.extract_lead_image(html, item["url"])
        art = {"id": item_id, "title": item["title"], "url": item["url"], "site": item["site"],
               "author": epub.clean_text(author or item.get("author") or ""), "date": item["date"],
               "body": body, "images": images,
               "cover": epub.fetch_cover(s, cover_url) if cover_url else None}
        epub.build_epub(art, path)
        log(f"built {item_id} ({item['title'][:60]})")
        return path

    def warm_loop(self):
        """Pre-build every article's EPUB so Sync all only offers ready files,
        and drop cache files nothing needs any more."""
        while True:
            try:
                items = self.catalog()
                for i in items:
                    if not self.cached(i["id"]):
                        try:
                            self.epub_path(i["id"])
                        except Exception as e:
                            log(f"build failed for {i['id']}: {e!r}")
                keep = {os.path.basename(self.cache_path(i["id"])) for i in items}
                for f in os.listdir(self.cache_dir):
                    if f.endswith(".epub") and f not in keep:
                        os.remove(os.path.join(self.cache_dir, f))
            except Exception as e:
                log(f"pre-build skipped ({e!r})")
            time.sleep(WARM_EVERY)

    # --- finishing ---

    def _finished(self):
        try:
            with open(self.finished_path) as f:
                return set(json.load(f))
        except (OSError, ValueError):
            return set()

    def finish(self, item_id, why):
        """Archive/unstar once. Raises on retryable (5xx/network) failures."""
        done = self._finished()
        if item_id in done:
            return
        source = self.source_of(item_id)
        if source in self.sources:
            try:
                self.sources[source].finish(item_id)
            except urllib.error.HTTPError as e:
                if e.code >= 500:
                    raise
                log(f"{source} refused finishing {item_id} ({e.code}); treating as done")
        done.add(item_id)
        tmp = self.finished_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(sorted(done), f)
        os.replace(tmp, self.finished_path)
        self.forget_lists()
        log(f"finished {item_id} ({why})")


def files_for(item):
    return [f"{item['name']}.epub", f"{item['name']}.epub.meta.json"]


def sidecar_bytes(item_id):
    return json.dumps({SIDECAR_KEY: item_id}).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = f"paperboy/{__version__}"

    @property
    def lib(self):
        return self.server.lib

    @property
    def token(self):
        return self.server.lib.cfg["token"]

    def log_message(self, fmt, *args):
        log("%s %s" % (self.address_string(), (fmt % args).replace(self.token, "<token>")))

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        # Behind a proxy that doesn't strip its mount path.
        for prefix in ("/paperboy", "/instapaper"):
            if path == prefix or path.startswith(prefix + "/"):
                path = path[len(prefix):] or "/"
        return path, urllib.parse.parse_qs(u.query)

    def _token_ok(self, given):
        return hmac.compare_digest(given.encode(), self.token.encode())

    def _authed(self):
        got = self.headers.get("Authorization", "")
        if got.startswith("Bearer ") and self._token_ok(got[7:]):
            return True
        self._send(401, {"error": "unauthorized"})
        return False

    def _base(self):
        """Absolute URL the reader can fetch bundle files from."""
        root = self.lib.cfg["public_url"]
        if not root:
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
            proto = self.headers.get("X-Forwarded-Proto") or "http"
            root = f"{proto}://{host}"
        return f"{root}/f/{self.token}/"

    def do_GET(self):
        path, q = self._route()
        try:
            if path == "/health":
                return self._send(200, "ok", "text/plain")
            if path == "/lists":
                if not self._token_ok((q.get("token") or [""])[0]):
                    return self._send(401, {"error": "unauthorized"})
                return self._send(200, {"lists": [
                    {"title": LIST_TITLES[s], "url": "{cfg.server}/list/" + s + "?page={page}&limit={limit}"}
                    for s in self.lib.sources]})
            m = re.fullmatch(r"/f/([^/]+)/([A-Za-z0-9-]+\.epub(?:\.meta\.json)?)", path)
            if m:
                return self._file(m.group(1), m.group(2))
            if not self._authed():
                return
            aliases = {"/unread": "instapaper", "/starred": "feedbin"}
            m = re.fullmatch(r"/list/(\w+)", path)
            source = aliases.get(path) or (m.group(1) if m else None)
            if source:
                return self._page(q, source)
            m = re.fullmatch(r"/epub/((?:fb)?\d+)(?:\.epub)?", path)
            if m:
                return self._epub(m.group(1))
        except Exception as e:
            log(f"ERROR {path}: {e!r}")
            return self._send(502, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path, _ = self._route()
        if not self._authed():
            return
        try:
            if path == "/progress":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(min(n, 8192)).decode() or "{}")
                except ValueError:
                    body = {}
                return self._progress(body)
        except Exception as e:
            log(f"ERROR {path}: {e!r}")
            return self._send(502, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def _page(self, q, source):
        def num(key, default):
            try:
                return max(1, int(q.get(key, [default])[0]))
            except ValueError:
                return default
        page, limit = num("page", 1), min(num("limit", 9), MAX_PAGE + 1)
        size = max(1, limit - 1)
        if source not in self.lib.sources:
            return self._send(200, {"total": 0, "items": []})
        base = self._base()
        try:
            everything = self.lib.catalog()
        except Exception as e:
            # One source down: still serve this list, but no Sync all (it would
            # silently be half a sync).
            log(f"catalog incomplete, no Sync all row: {e!r}")
            everything = [dict(i, name=file_stem(i["title"]) + f"-{i['id']}")
                          for i in self.lib.items(source)]
            sync_row = None
        else:
            ready = [i for i in everything if self.lib.cached(i["id"])]
            n = len(everything)
            sync_row = {
                "id": SYNC_ID, "base": base,
                "title": f"Sync all ({n} articles)" if len(ready) == n
                         else f"Sync all ({len(ready)} of {n} ready)",
                "subtitle": " + ".join(LIST_TITLES[s] for s in self.lib.sources),
                "description": "Downloads every article on your reading list into /Paperboy. "
                               "Afterwards, open the Library and use Refresh library.",
                "files": [f for i in ready for f in files_for(i)],
            } if ready else None
        rows = [dict(i, base=base, files=files_for(i)) for i in everything
                if self.lib.source_of(i["id"]) == source]
        if sync_row:
            rows.insert(0, sync_row)
        start = (page - 1) * size
        self._send(200, {"total": len(rows), "items": rows[start:start + limit]})

    def _file(self, token, fname):
        if not self._token_ok(token):
            return self._send(401, {"error": "unauthorized"})
        by_name = {f: i for i in self.lib.catalog() for f in files_for(i)}
        item = by_name.get(fname)
        if not item:
            return self._send(404, {"error": "not on the reading list"})
        if fname.endswith(".meta.json"):
            return self._send(200, sidecar_bytes(item["id"]))
        p = self.lib.epub_path(item["id"])
        if not p:
            return self._send(404, {"error": "not on the reading list"})
        with open(p, "rb") as f:
            self._send(200, f.read(), "application/epub+zip")

    def _epub(self, item_id):
        p = self.lib.epub_path(item_id)
        if not p:
            return self._send(404, {"error": "not on the reading list"})
        with open(p, "rb") as f:
            self._send(200, f.read(), "application/epub+zip",
                       {"Content-Disposition": f'attachment; filename="{item_id}.epub"'})

    def _progress(self, body):
        # New plugins send their sidecar id as "id"; ids from older downloads
        # arrive as "legacy_id". Unfilled templates arrive as literal "{meta...}".
        candidates = [str(body.get(k, "")).strip() for k in ("id", "legacy_id")]
        item_id = next((c for c in candidates if re.fullmatch(r"(?:fb)?\d+", c)), "")
        if not item_id:
            return self._send(200, {"ok": True, "ignored": "not a Paperboy article"})
        try:
            pct = float(str(body.get("percent", "")).strip().rstrip("%"))
        except ValueError:
            return self._send(200, {"ok": True, "ignored": "no percent"})
        if pct < float(self.lib.cfg["archive_at"]):
            return self._send(200, {"ok": True, "finished": False, "percent": pct})
        self.lib.finish(item_id, f"read {pct:g}% event={body.get('event_id', '')}")
        self._send(200, {"ok": True, "finished": True})


def main():
    try:
        cfg = config.load()
    except config.ConfigError as e:
        sys.exit(f"Paperboy: {e}")
    lib = Library(cfg)
    srv = ThreadingHTTPServer((cfg["bind"], int(cfg["port"])), Handler)
    srv.lib = lib
    log(f"Paperboy {__version__} on {cfg['bind']}:{cfg['port']}; sources: {', '.join(lib.sources)}; "
        f"finished at {cfg['archive_at']}%")
    if "--no-background" not in sys.argv:
        threading.Thread(target=lib.warm_loop, name="prebuild", daemon=True).start()
        if cfg["reader_sync"]:
            threading.Thread(target=reader_sync.loop, name="reader-sync", daemon=True,
                             args=(lib, sidecar_bytes, files_for)).start()
    srv.serve_forever()
