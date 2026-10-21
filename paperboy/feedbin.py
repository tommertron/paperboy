"""Feedbin API (v2) client for starred entries, stdlib only. Item ids are "fb<entry id>"."""
import base64
import json
import re
import urllib.error
import urllib.request

from .instapaper import _site

API_BASE = "https://api.feedbin.com/v2"
MIN_BODY_CHARS = 1500  # shorter feed content is a summary; use Feedbin's full-page extract


class Feedbin:
    source = "feedbin"

    def __init__(self, creds):
        auth = base64.b64encode(f"{creds['email']}:{creds['password']}".encode()).decode()
        self.headers = {"Authorization": "Basic " + auth,
                        "Content-Type": "application/json; charset=utf-8"}
        self._entries = {}

    def _req(self, method, path, body=None):
        url = path if path.startswith("http") else API_BASE + path
        req = urllib.request.Request(url, method=method, headers=self.headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        return json.loads(raw.decode()) if raw.strip() else None

    def items(self):
        ids = self._req("GET", "/starred_entries.json") or []
        entries = []
        for i in range(0, len(ids), 100):          # entries.json takes at most 100 ids
            chunk = ",".join(str(x) for x in ids[i:i + 100])
            entries += self._req("GET", f"/entries.json?ids={chunk}&mode=extended") or []
        entries.sort(key=lambda e: e.get("published") or e.get("created_at") or "", reverse=True)
        out = []
        for e in entries:
            url = e.get("url") or ""
            iid = f"fb{e['id']}"
            self._entries[iid] = e
            out.append({
                "id": iid,
                "title": e.get("title") or "",
                "url": url,
                "site": _site(url),
                "date": (e.get("published") or e.get("created_at") or "")[:10],
                "description": e.get("summary") or "",
                "author": e.get("author") or "",
            })
        return out

    def article_html(self, item):
        e = self._entries.get(item["id"])
        if e is None:
            self.items()
            e = self._entries.get(item["id"], {})
        content, lead = e.get("content") or "", None
        if len(re.sub(r"<[^>]+>", "", content)) < MIN_BODY_CHARS and e.get("extracted_content_url"):
            try:
                x = self._req("GET", e["extracted_content_url"]) or {}
                if len(x.get("content") or "") > len(content):
                    content, lead = x["content"], x.get("lead_image_url")
            except (urllib.error.URLError, ValueError):
                pass
        return content, lead, e.get("author") or ""

    def finish(self, item_id):
        self._req("DELETE", "/starred_entries.json", {"starred_entries": [int(item_id[2:])]})
