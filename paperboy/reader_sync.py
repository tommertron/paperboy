"""Home sync: whenever the CrossPoint reader is in File Transfer mode on the same
network, make /Paperboy on its card match the reading list.

The reader's on-device plugin can download but never delete, so this is the
half that tidies up. Every minute it sends CrossPoint's UDP discovery ("hello"
to port 8134; only a reader in File Transfer mode answers), then over the
reader's own web API:

  - uploads every article on the reading list that the card lacks,
  - deletes articles that were archived (Instapaper) or unstarred (Feedbin),
  - deletes extra copies of an article that's already on the card.

It only ever touches files whose "<book>.meta.json" sidecar carries a Paperboy
id; everything else on the card is invisible to it. If any source can't be
read the pass is skipped (an outage must never look like an empty reading
list), and an empty reading list never deletes anything.
"""
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .config import data_path
from .log import log

FOLDER = "/Paperboy"
LEGACY_FOLDERS = ("/Instapaper",)   # pre-1.0 plugin downloads: recognised, never added to
SIDECAR_KEYS = ("paperboy_id", "instapaper_id")
DISCOVERY_PORT = 8134
POLL = 60
HTTP_TIMEOUT = 20

_sidecar_cache = {}  # (path, size) -> (id or "", epub design version or "")


def discover(broadcasts, last_ip=None, timeout=2.0):
    """IP of a reader in File Transfer mode, or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(timeout)
    try:
        for addr in ([last_ip] if last_ip else []) + list(broadcasts):
            try:
                s.sendto(b"hello", (addr, DISCOVERY_PORT))
            except OSError:
                pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, (ip, _) = s.recvfrom(512)
            except socket.timeout:
                return None
            if data.startswith(b"crosspoint"):
                return ip
        return None
    finally:
        s.close()


class Reader:
    """The reader's File Transfer web API (no auth)."""

    def __init__(self, host):
        self.base = f"http://{host}"

    def _req(self, method, path, data=None, headers=None):
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.read()

    def status(self):
        return json.loads(self._req("GET", "/api/status"))

    def list(self, path):
        return json.loads(self._req("GET", "/api/files?" + urllib.parse.urlencode({"path": path})))

    def read(self, path):
        return self._req("GET", "/download?" + urllib.parse.urlencode({"path": path}))

    def mkdir(self, parent, name):
        self._req("POST", "/mkdir", urllib.parse.urlencode({"name": name, "path": parent}).encode(),
                  {"Content-Type": "application/x-www-form-urlencoded"})

    def upload(self, folder, name, data, ctype):
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode() + data + \
               f"\r\n--{boundary}--\r\n".encode()
        self._req("POST", "/upload?" + urllib.parse.urlencode({"path": folder}), body,
                  {"Content-Type": f"multipart/form-data; boundary={boundary}"})

    def delete(self, paths):
        self._req("POST", "/delete", urllib.parse.urlencode({"paths": json.dumps(paths)}).encode(),
                  {"Content-Type": "application/x-www-form-urlencoded"})


def card_state(reader):
    """({id: [epub paths]}, [orphan sidecar paths], main_folder_exists, {epub path: version})."""
    have, orphans, versions = {}, [], {}
    # Listing a folder that doesn't exist answers [] rather than 404 on
    # CrossPoint 2.0.0rc, so check the root for the folder itself.
    exists = any(e.get("isDirectory") and e.get("name") == FOLDER.strip("/")
                 for e in reader.list("/"))
    for folder in (FOLDER,) + LEGACY_FOLDERS:
        try:
            entries = reader.list(folder)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            continue
        if not isinstance(entries, list):
            continue
        files = {e["name"]: e.get("size", 0) for e in entries if not e.get("isDirectory")}
        for name, size in files.items():
            if not name.endswith(".epub.meta.json"):
                continue
            path = f"{folder}/{name}"
            if (path, size) not in _sidecar_cache:
                try:
                    meta = json.loads(reader.read(path).decode("utf-8", "replace"))
                    _sidecar_cache[(path, size)] = (next(
                        (str(meta[k]) for k in SIDECAR_KEYS if meta.get(k)), ""), str(meta.get("epub", "")))
                except (OSError, ValueError):
                    _sidecar_cache[(path, size)] = ("", "")
            item_id, version = _sidecar_cache[(path, size)]
            if not item_id:
                continue  # another plugin's sidecar
            epub = name[:-len(".meta.json")]
            if epub in files:
                have.setdefault(item_id, []).append(f"{folder}/{epub}")
                versions[f"{folder}/{epub}"] = version
            else:
                orphans.append(path)
    return have, orphans, exists, versions


def sync_pass(lib, host, sidecar_bytes, files_for, dry_run=False):
    reader = Reader(host)
    st = reader.status()
    if st.get("firmwareFamily") != "crosspoint" or st.get("mode") != "STA":
        return None
    items = lib.catalog()   # raises on any source failure -> no pass at all
    want = {i["id"]: i for i in items}
    have, orphans, exists, versions = card_state(reader)

    delete, removed, outdated = list(orphans), [], []
    for item_id, epubs in have.items():
        if item_id not in want:
            if items:   # never purge against an empty reading list
                delete += [p for e in epubs for p in (e, e + ".meta.json")]
                removed.append(item_id)
            continue
        canonical = f"{FOLDER}/{want[item_id]['name']}.epub"
        keep = canonical if canonical in epubs else sorted(epubs)[0]
        delete += [p for e in epubs if e != keep for p in (e, e + ".meta.json")]
        # Built to an older EPUB design (e.g. before the end page): swap it for
        # a fresh copy. Reading position goes with it, as the reader keeps it
        # in the book's cache, which an upload clears.
        current = json.loads(sidecar_bytes(item_id)).get("epub", "")
        if versions.get(keep) != current:
            delete += [keep, keep + ".meta.json"]
            outdated.append(item_id)

    missing = [i for i in items if i["id"] not in have or i["id"] in outdated]
    if dry_run:
        return {"would_delete": delete, "would_add": [i["name"] for i in missing]}
    # Ground truth for what Sync all can skip, recorded before anything below
    # can fail; uploads are added as they land.
    on_card = set(have) - set(removed) - set(outdated)
    lib.mark_on_card(on_card, replace=True)

    if delete:
        reader.delete(delete)
        gone = set(delete)
        for k in [k for k in _sidecar_cache if k[0] in gone]:
            del _sidecar_cache[k]
    added = []
    if missing and not exists:
        reader.mkdir("/", FOLDER.strip("/"))
    for i in missing:
        p = lib.epub_path(i["id"])
        if not p:
            continue
        epub_name, meta_name = files_for(i)
        with open(p, "rb") as f:
            reader.upload(FOLDER, epub_name, f.read(), "application/epub+zip")
        # Sidecar second: a half-finished pass leaves an EPUB without one
        # (retried next pass), never a sidecar pointing at nothing.
        reader.upload(FOLDER, meta_name, sidecar_bytes(i["id"]), "application/json")
        added.append(i["name"])
        lib.mark_on_card([i["id"]])
    return {"deleted": delete, "removed_ids": removed, "added": added}


def loop(lib, sidecar_bytes, files_for):
    state_path = data_path("reader_sync.json")
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    while True:
        try:
            ip = discover(lib.cfg["broadcast"], state.get("last_ip"))
            if ip:
                res = sync_pass(lib, ip, sidecar_bytes, files_for)
                if ip != state.get("last_ip") or (res and (res["deleted"] or res["added"])):
                    if res and (res["deleted"] or res["added"]):
                        log(f"reader {ip}: +{len(res['added'])} added, {len(res['removed_ids'])} "
                            f"finished elsewhere removed, {len(res['deleted'])} files deleted")
                        state.update(last_sync=time.strftime("%Y-%m-%d %H:%M:%S"), last_result=res)
                    state["last_ip"] = ip
                    with open(state_path, "w") as f:
                        json.dump(state, f, indent=2)
        except Exception as e:
            log(f"reader sync: pass failed ({e!r})")
        time.sleep(POLL)
