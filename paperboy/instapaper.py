"""Instapaper Full API (api/1) client, stdlib only.

Uses xAuth (consumer key/secret + the account's username/password) to get a
long-lived access token, cached in the data directory and re-requested only
when missing or rejected.
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .config import data_path

API_BASE = "https://www.instapaper.com/api/1"


def _sign(method, url, params, consumer_secret, token_secret=""):
    """OAuth 1.0a HMAC-SHA1 signature."""
    q = lambda v: urllib.parse.quote(str(v), safe="")
    encoded = "&".join(f"{q(k)}={q(params[k])}" for k in sorted(params))
    base = f"{method}&{q(url)}&{q(encoded)}"
    key = f"{q(consumer_secret)}&{q(token_secret)}"
    return base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()


class Instapaper:
    source = "instapaper"

    def __init__(self, creds):
        self.c = creds
        self.token_path = data_path("instapaper_token.json")

    def _oauth(self, **extra):
        p = {
            "oauth_consumer_key": self.c["consumer_key"],
            "oauth_nonce": uuid.uuid4().hex,
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_version": "1.0",
        }
        p.update(extra)
        return p

    def _xauth(self):
        url = f"{API_BASE}/oauth/access_token"
        params = self._oauth(x_auth_mode="client_auth", x_auth_username=self.c["username"],
                             x_auth_password=self.c["password"])
        params["oauth_signature"] = _sign("POST", url, params, self.c["consumer_secret"])
        req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode()
        parsed = dict(pair.split("=", 1) for pair in body.split("&"))
        return parsed["oauth_token"], parsed["oauth_token_secret"]

    def _access(self, refresh=False):
        if not refresh:
            try:
                with open(self.token_path) as f:
                    d = json.load(f)
                if d.get("oauth_token") and d.get("oauth_token_secret"):
                    return d["oauth_token"], d["oauth_token_secret"]
            except (OSError, ValueError):
                pass
        tok, sec = self._xauth()
        tmp = self.token_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"oauth_token": tok, "oauth_token_secret": sec}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.token_path)
        return tok, sec

    def api(self, path, extra=None, raw=False, _retried=False):
        token, secret = self._access()
        url = API_BASE + path
        params = self._oauth(oauth_token=token)
        params.update(extra or {})
        params["oauth_signature"] = _sign("POST", url, params, self.c["consumer_secret"], secret)
        req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 401 and not _retried:   # stale token: get a new one once
                self._access(refresh=True)
                return self.api(path, extra, raw, _retried=True)
            raise
        return body if raw else json.loads(body.decode())

    # --- what the server uses ---

    def items(self):
        """Unread bookmarks, newest first, as reader-list items."""
        data = self.api("/bookmarks/list", {"folder_id": "unread", "limit": "500"})
        out = []
        for b in data if isinstance(data, list) else []:
            if b.get("type") != "bookmark":
                continue
            try:
                d = datetime.date.fromtimestamp(float(b.get("time")))
            except (TypeError, ValueError):
                d = datetime.date.today()
            url = b.get("url") or ""
            out.append({
                "id": str(b["bookmark_id"]),
                "title": b.get("title") or "",
                "url": url,
                "site": _site(url),
                "date": d.isoformat(),
                "description": b.get("description") or "",
                "author": "",
            })
        out.sort(key=lambda i: i["date"], reverse=True)
        return out

    def article_html(self, item):
        """(html, lead_image_url or None, author) for one item."""
        html = self.api("/bookmarks/get_text", {"bookmark_id": item["id"]}, raw=True)
        return html.decode("utf-8", "replace"), None, ""

    def finish(self, item_id):
        self.api("/bookmarks/archive", {"bookmark_id": item_id})


def _site(url):
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1).replace("www.", "") if m else ""
