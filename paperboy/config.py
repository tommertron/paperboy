"""Settings: one JSON file in the data directory (PAPERBOY_DATA, default /data).

{
  "instapaper": {"access_token"},          optional; a personal access token (API v2).
                                           Or, until xAuth ends on 2027-09-30, the old
                                           {"consumer_key", "consumer_secret", "username", "password"}
  "feedbin":    {"email", "password"},                                        optional
  "public_url": "https://host/paperboy",  the address the reader uses (for bundle downloads)
  "archive_at": 95,                        percent read that counts as finished
  "port": 17300,
  "bind": "0.0.0.0",
  "reader_sync": true,                     tidy the reader when it's in File Transfer mode
  "broadcast": ["255.255.255.255"],        where to look for the reader
  "token": "..."                           added on first start
}

At least one of instapaper / feedbin is required.
"""
import json
import os
import secrets

DATA_DIR = os.environ.get("PAPERBOY_DATA", "/data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

DEFAULTS = {
    "archive_at": 95,
    "port": 17300,
    "bind": "0.0.0.0",
    "reader_sync": True,
    "broadcast": ["255.255.255.255"],
    "public_url": "",
}


class ConfigError(Exception):
    pass


def data_path(*parts):
    return os.path.join(DATA_DIR, *parts)


def load():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"no config file at {CONFIG_PATH} (see the README for an example)")
    except ValueError as e:
        raise ConfigError(f"{CONFIG_PATH} is not valid JSON: {e}")

    ip, fb = cfg.get("instapaper"), cfg.get("feedbin")
    if not ip and not fb:
        raise ConfigError("config needs an \"instapaper\" section, a \"feedbin\" section, or both")
    if ip and not ip.get("access_token"):
        missing = [k for k in ("consumer_key", "consumer_secret", "username", "password") if not ip.get(k)]
        if len(missing) == 4:
            raise ConfigError("instapaper section needs an \"access_token\" (a personal access token from "
                              "instapaper.com/developers/applications)")
        if missing:
            raise ConfigError("instapaper section is missing: " + ", ".join(missing))
    if fb:
        missing = [k for k in ("email", "password") if not fb.get(k)]
        if missing:
            raise ConfigError("feedbin section is missing: " + ", ".join(missing))

    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(24)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_PATH)
        print(f"Paperboy: added a new token to {CONFIG_PATH}; enter it on the reader.", flush=True)

    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    cfg["public_url"] = (cfg.get("public_url") or "").rstrip("/")
    return cfg
