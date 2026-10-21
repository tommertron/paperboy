#!/usr/bin/env python3
"""Build the Plugin Hub catalog for the Paperboy plugin.

Output: dist/crosspoint/
    catalog.json                           custom catalog for Plugin Hub
    plugins/<name>/<version>/<files...>    immutable, one folder per version

Copy dist/crosspoint/ to the website's static/crosspoint/ to publish. Only the
files plugin/manifest.json lists are copied, and the build refuses to finish if
any credential from a local data/config.json appears in the output.

Usage: tools/build_catalog.py [--base-url https://coefficiencies.com/crosspoint]
"""
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugin")
DIST = os.path.join(ROOT, "dist", "crosspoint")
BASE_URL = "https://coefficiencies.com/crosspoint"


def secrets():
    try:
        cfg = json.load(open(os.path.join(ROOT, "data", "config.json")))
    except (OSError, ValueError):
        return set()
    out = {cfg.get("token", "")}
    for section in ("instapaper", "feedbin"):
        out |= {str(v) for k, v in (cfg.get(section) or {}).items() if k != "username" and k != "email"}
    return {s for s in out if len(s) >= 8}


def main():
    base_url = sys.argv[sys.argv.index("--base-url") + 1].rstrip("/") if "--base-url" in sys.argv else BASE_URL
    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    man = json.load(open(os.path.join(PLUGIN, "manifest.json")))
    files = man["files"]
    assert "config.json" not in files, "config.json must never be published"
    dest = os.path.join(DIST, "plugins", man["name"], man["version"])
    os.makedirs(dest)
    for f in files:
        shutil.copy2(os.path.join(PLUGIN, f), os.path.join(dest, f))
    catalog = {"name": "coefficiencies", "schema_version": 1, "plugins": [{
        "name": man["name"], "title": man["title"], "description": man["description"],
        "author": man["author"], "version": man["version"],
        "base": f"{base_url}/plugins/{man['name']}/{man['version']}/", "files": files,
    }]}
    with open(os.path.join(DIST, "catalog.json"), "w") as f:
        json.dump(catalog, f, indent=2)
    bad = secrets()
    for root, _, fs in os.walk(DIST):
        for f in fs:
            data = open(os.path.join(root, f), "rb").read()
            if any(s.encode() in data for s in bad):
                shutil.rmtree(DIST)
                sys.exit(f"REFUSING: a credential from data/config.json appears in {f}")
    print(f"{man['name']} {man['version']}: {', '.join(files)}\nbuilt {DIST}")


if __name__ == "__main__":
    main()
