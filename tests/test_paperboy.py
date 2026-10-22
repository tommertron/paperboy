"""Offline tests: fake sources, the real HTTP server, and a fake CrossPoint reader.

Run: python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DATA = tempfile.mkdtemp(prefix="paperboy-test-")
os.environ["PAPERBOY_DATA"] = DATA

from paperboy import config, epub, reader_sync, server  # noqa: E402

TOKEN = "test-token-1234567890"


class FakeSource:
    def __init__(self, source, items):
        self.source = source
        self._items = items
        self.finished = []

    def items(self):
        return [dict(i) for i in self._items]

    def article_html(self, item):
        return f"<p>Body of {item['title']}.</p><img src='data:image/png;base64,xx' alt='alt text'>", None, "A. Writer"

    def finish(self, item_id):
        self.finished.append(item_id)


def make_item(i, title, prefix=""):
    return {"id": f"{prefix}{i}", "title": title, "url": f"https://example.com/{i}", "site": "example.com",
            "date": "2026-10-21", "description": "", "author": ""}


def make_library():
    shutil.rmtree(DATA)
    os.makedirs(DATA)
    cfg = dict(config.DEFAULTS, token=TOKEN, public_url="http://pub.example/paperboy")
    lib = server.Library(cfg)
    lib.sources = {
        "instapaper": FakeSource("instapaper", [make_item(101, "First Article"), make_item(102, "Second: Article!")]),
        "feedbin": FakeSource("feedbin", [make_item(201, "Starred One", "fb"), make_item(202, "First Article", "fb")]),
    }
    return lib


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.lib = make_library()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.srv.lib = self.lib
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()

    def get(self, path, auth=True, method="GET", body=None):
        headers = {"Authorization": f"Bearer {TOKEN}"} if auth else {}
        req = urllib.request.Request(self.base + path, headers=headers, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_health_and_auth(self):
        self.assertEqual(self.get("/health", auth=False), (200, b"ok"))
        self.assertEqual(self.get("/list/instapaper", auth=False)[0], 401)
        self.assertEqual(self.get("/lists?token=wrong", auth=False)[0], 401)

    def test_one_combined_list(self):
        st, b = self.get(f"/lists?token={TOKEN}", auth=False)
        lists = json.loads(b)["lists"]
        self.assertEqual(len(lists), 1)                 # one list: the reader skips its picker
        self.assertIn("/list/all?", lists[0]["url"])
        for i in self.lib.catalog():
            self.lib.epub_path(i["id"])
        rows = json.loads(self.get("/list/all?page=1&limit=17")[1])["items"]
        self.assertEqual(rows[0]["title"], "Sync all (4 articles)")
        self.assertEqual(sorted(r["id"] for r in rows[1:]), ["101", "102", "fb201", "fb202"])
        del self.lib.sources["feedbin"]
        rows = json.loads(self.get("/list/all?page=1&limit=17")[1])["items"]
        self.assertEqual(sorted(r["id"] for r in rows[1:]), ["101", "102"])

    def test_list_rows_names_and_sync_row(self):
        for i in self.lib.catalog():
            self.lib.epub_path(i["id"])          # pre-build everything
        st, b = self.get("/list/instapaper?page=1&limit=9")
        rows = json.loads(b)["items"]
        self.assertEqual(rows[0]["id"], "sync-all")
        self.assertEqual(rows[0]["title"], "Sync all (4 articles)")
        self.assertEqual(len(rows[0]["files"]), 8)
        self.assertTrue(rows[0]["base"].startswith("http://pub.example/paperboy/f/"))
        names = [r["files"][0] for r in rows[1:]]
        self.assertEqual(names, ["First-Article.epub", "Second-Article.epub"])
        st, b = self.get("/starred")                    # alias, and a duplicate title gets its id
        fb = [r["files"][0] for r in json.loads(b)["items"][1:]]
        self.assertIn("First-Article-fb202.epub", fb)

    def test_sync_all_offers_only_whats_not_on_the_card(self):
        for i in self.lib.catalog():
            self.lib.epub_path(i["id"])
        sync_row = lambda: json.loads(self.get("/list/instapaper?page=1&limit=9")[1])["items"][0]

        def fetch(f):
            # The server notes a delivery just after sending, so let it finish.
            st = self.get(f"/f/{TOKEN}/{f}", auth=False)[0]
            time.sleep(0.05)
            return st

        row = sync_row()
        self.assertEqual(row["files"][:2], ["First-Article.epub.meta.json", "First-Article.epub"])
        for f in row["files"][:3]:                     # cancelled part-way: the reader keeps nothing
            fetch(f)
        self.assertEqual(self.lib.on_card(), set())
        for f in ["Second-Article.epub", "Second-Article.epub.meta.json"]:   # one article's own row
            fetch(f)
        self.assertEqual(self.lib.on_card(), {"102"})
        row = sync_row()
        self.assertEqual(row["title"], "Sync all (3 new)")
        self.assertEqual(len(row["files"]), 6)
        for f in row["files"]:                         # a whole sync
            self.assertEqual(fetch(f), 200)
        self.assertEqual(self.lib.on_card(), {"101", "102", "fb201", "fb202"})
        self.assertNotEqual(sync_row()["id"], "sync-all")   # nothing new: no Sync all row

        self.lib.mark_on_card(["101"], replace=True)   # a home tidy-up saw only one
        self.assertEqual(sync_row()["title"], "Sync all (3 new)")
        self.get("/progress", method="POST", body={"id": "fb201", "percent": "10"})
        self.assertEqual(sync_row()["title"], "Sync all (2 new)")

    def test_new_articles_are_built_on_first_ask(self):
        st, b = self.get("/list/instapaper?page=1&limit=9")
        self.assertNotEqual(json.loads(b)["items"][0]["id"], "sync-all")   # nothing built yet
        for _ in range(100):                          # asking kicked off the builds
            if all(self.lib.cached(i["id"]) for i in self.lib.catalog()):
                break
            time.sleep(0.05)
        st, b = self.get("/list/instapaper?page=1&limit=9")
        self.assertEqual(json.loads(b)["items"][0]["title"], "Sync all (4 articles)")

    def test_bundle_files_and_epub(self):
        st, b = self.get(f"/f/{TOKEN}/First-Article.epub", auth=False)
        self.assertEqual(st, 200)
        z = zipfile.ZipFile(io.BytesIO(b))
        self.assertIsNone(z.testzip())
        art = z.read("OEBPS/article.xhtml").decode()
        self.assertIn("Body of First Article", art)
        self.assertNotIn("alt text", art)                 # images replace alt text; failed ones vanish
        self.assertIn("OEBPS/cover.jpg", z.namelist())
        st, b = self.get(f"/f/{TOKEN}/First-Article.epub.meta.json", auth=False)
        self.assertEqual(json.loads(b), {"paperboy_id": "101", "epub": server.EPUB_VERSION})
        self.assertEqual(self.get(f"/f/nope/First-Article.epub", auth=False)[0], 401)
        self.assertEqual(self.get(f"/f/{TOKEN}/Nope.epub", auth=False)[0], 404)

    def test_end_page_counts_as_finished(self):
        p = self.lib.epub_path("101")
        with zipfile.ZipFile(p) as z:
            self.assertIn("OEBPS/end.xhtml", z.namelist())
        end = self.lib.finish_at("101")
        self.assertTrue(0 < end < 95)                     # a short piece: the end page starts early
        post = lambda pct: json.loads(self.get("/progress", method="POST", body={"id": "101", "percent": pct})[1])
        self.assertFalse(post(str(end - 1))["finished"])
        self.assertTrue(post(str(end))["finished"])

    def test_progress(self):
        ip, fb = self.lib.sources["instapaper"], self.lib.sources["feedbin"]
        post = lambda body: json.loads(self.get("/progress", method="POST", body=body)[1])
        self.assertIn("ignored", post({"id": "{meta.paperboy_id}", "legacy_id": "{meta.instapaper_id}", "percent": "100"}))
        self.assertFalse(post({"id": "101", "percent": "50"})["finished"])
        self.assertTrue(post({"id": "{meta.paperboy_id}", "legacy_id": "101", "percent": "100"})["finished"])
        self.assertTrue(post({"id": "fb201", "percent": "97"})["finished"])
        post({"id": "101", "percent": "100"})               # replayed event: no second archive
        self.assertEqual(ip.finished, ["101"])
        self.assertEqual(fb.finished, ["fb201"])


class FakeReader(BaseHTTPRequestHandler):
    card = {}
    calls = []

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/api/status":
            return self._send(200, {"firmwareFamily": "crosspoint", "mode": "STA"})
        if u.path == "/api/files":
            # Like CrossPoint 2.0.0rc: a missing folder lists as [], not 404.
            d = q["path"][0].rstrip("/")
            paths = [p for p in self.card if p != "__dirs__"] + self.card.get("__dirs__", [])
            kids = {p[len(d) + 1:].split("/")[0] for p in paths if p.startswith(d + "/")}
            return self._send(200, [{"name": k, "size": len(self.card.get(d + "/" + k, "")),
                                     "isDirectory": d + "/" + k not in self.card}
                                    for k in sorted(kids)])
        if u.path == "/download":
            return self._send(200, self.card[q["path"][0]].encode())

    def do_POST(self):
        import email.parser, email.policy
        u = urllib.parse.urlsplit(self.path)
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        if u.path == "/upload":
            msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + raw)
            part = next(msg.iter_parts())
            folder = urllib.parse.parse_qs(u.query)["path"][0]
            data = part.get_payload(decode=True)
            self.card[f"{folder}/{part.get_filename()}"] = data.decode() if part.get_filename().endswith(".json") else "<epub>"
            self.calls.append("upload")
        elif u.path == "/delete":
            for p in json.loads(urllib.parse.parse_qs(raw.decode())["paths"][0]):
                self.card.pop(p, None)
        elif u.path == "/mkdir":
            f = urllib.parse.parse_qs(raw.decode())
            self.card.setdefault("__dirs__", []).append(f["path"][0].rstrip("/") + "/" + f["name"][0])
        self._send(200, b"ok")


class ReaderSyncTest(unittest.TestCase):
    def test_sync_and_purge(self):
        lib = make_library()
        meta = lambda i, k="paperboy_id": json.dumps({k: i, "epub": server.EPUB_VERSION})
        FakeReader.card = {
            "/Paperboy/First-Article.epub": "<epub>",
            "/Paperboy/First-Article.epub.meta.json": meta("101"),
            "/Paperboy/Old-Copy.epub": "<dup>",                       # duplicate of 101
            "/Paperboy/Old-Copy.epub.meta.json": meta("101"),
            "/Instapaper/Done.epub": "<finished elsewhere>",          # legacy folder + legacy key
            "/Instapaper/Done.epub.meta.json": meta("999", "instapaper_id"),
            "/Instapaper/Second.epub": "<legacy copy of 102>",
            "/Instapaper/Second.epub.meta.json": meta("102", "instapaper_id"),
            "/Paperboy/Gone.epub.meta.json": meta("998"),             # orphan sidecar
            "/Paperboy/Novel.epub": "<not ours>",
            "/Paperboy/Other.epub": "<other plugin>",
            "/Paperboy/Other.epub.meta.json": json.dumps({"bookfusion_id": "5"}),
        }
        srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeReader)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        host = f"127.0.0.1:{srv.server_address[1]}"
        try:
            res = reader_sync.sync_pass(lib, host, server.sidecar_bytes, server.files_for)
            card = FakeReader.card
            for kept in ["/Paperboy/First-Article.epub", "/Instapaper/Second.epub", "/Paperboy/Novel.epub",
                         "/Paperboy/Other.epub", "/Paperboy/Other.epub.meta.json"]:
                self.assertIn(kept, card)
            for gone in ["/Paperboy/Old-Copy.epub", "/Instapaper/Done.epub", "/Paperboy/Gone.epub.meta.json"]:
                self.assertNotIn(gone, card)
            self.assertEqual(sorted(res["added"]), ["First-Article-fb202", "Starred-One"])
            self.assertEqual(json.loads(card["/Paperboy/Starred-One.epub.meta.json"]),
                             {"paperboy_id": "fb201", "epub": server.EPUB_VERSION})
            self.assertEqual(lib.on_card(), {"101", "102", "fb201", "fb202"})
            again = reader_sync.sync_pass(lib, host, server.sidecar_bytes, server.files_for, dry_run=True)
            self.assertEqual(again, {"would_delete": [], "would_add": []})
            # An empty reading list must never purge.
            for s in lib.sources.values():
                s._items = []
            lib.forget_lists()
            self.assertEqual(reader_sync.sync_pass(lib, host, server.sidecar_bytes, server.files_for,
                                                   dry_run=True)["would_delete"], [])
        finally:
            srv.shutdown()

    def test_replaces_copies_built_to_an_older_design(self):
        lib = make_library()
        FakeReader.card = {
            "/Paperboy/First-Article.epub": "<old design>",
            "/Paperboy/First-Article.epub.meta.json": json.dumps({"paperboy_id": "101"}),   # pre-version
        }
        srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeReader)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        host = f"127.0.0.1:{srv.server_address[1]}"
        try:
            res = reader_sync.sync_pass(lib, host, server.sidecar_bytes, server.files_for)
            self.assertIn("First-Article", res["added"])
            self.assertEqual(json.loads(FakeReader.card["/Paperboy/First-Article.epub.meta.json"])["epub"],
                             server.EPUB_VERSION)
            self.assertEqual(reader_sync.sync_pass(lib, host, server.sidecar_bytes, server.files_for,
                                                   dry_run=True), {"would_delete": [], "would_add": []})
        finally:
            srv.shutdown()

    def test_creates_a_deleted_folder(self):
        lib = make_library()
        lib.mark_on_card(["101", "102", "fb201", "fb202"])   # then the user deleted /Paperboy
        FakeReader.card = {"/Books/Novel.epub": "<not ours>"}
        srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeReader)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            res = reader_sync.sync_pass(lib, f"127.0.0.1:{srv.server_address[1]}",
                                        server.sidecar_bytes, server.files_for)
            self.assertIn("/Paperboy", FakeReader.card["__dirs__"])
            self.assertEqual(len(res["added"]), 4)
            self.assertEqual(lib.on_card(), {"101", "102", "fb201", "fb202"})
        finally:
            srv.shutdown()

    def test_source_outage_skips_pass(self):
        lib = make_library()
        lib.sources["feedbin"].items = lambda: (_ for _ in ()).throw(urllib.error.URLError("down"))
        with self.assertRaises(urllib.error.URLError):
            lib.catalog()


class EpubTest(unittest.TestCase):
    def test_file_stem(self):
        self.assertEqual(server.file_stem("Don’t Panic: A Guide (2026)"), "Dont-Panic-A-Guide-2026")
        self.assertEqual(server.file_stem("???"), "Article")


if __name__ == "__main__":
    unittest.main()
