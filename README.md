# Paperboy

Your unread [Instapaper](https://www.instapaper.com) articles and starred [Feedbin](https://feedbin.com) articles on a [CrossPoint](https://crosspointreader.com) e-reader.

- Browse your reading list on the reader and download articles as EPUBs, with title covers and the articles' images.
- **Sync all** downloads, in one go, every article that isn't on the reader yet.
- Finish an article and it's archived in Instapaper (or unstarred in Feedbin) the next time the reader is on Wi-Fi.
- While the reader is in File Transfer mode on your home network, it's tidied to match your list: archived articles removed, new ones added.

Two parts: this **server**, which you run at home and which talks to Instapaper and Feedbin, and a **CrossPoint plugin** on the reader. Your passwords stay on your server.

Full setup guide: https://coefficiencies.com/apps/paperboy/

## Quick start

You need Docker, a CrossPoint reader on firmware 2.0 or later, and an [Instapaper API key](https://www.instapaper.com/main/request_oauth_consumer_token) (and/or a Feedbin account).

```sh
mkdir -p paperboy/data && cd paperboy
curl -O https://raw.githubusercontent.com/tommertron/paperboy/main/docker-compose.yml
curl -o data/config.json https://raw.githubusercontent.com/tommertron/paperboy/main/examples/config.json
# edit data/config.json, and set `user:` in docker-compose.yml to your `id -u`:`id -g`
docker compose up -d
```

On first start a `token` is added to `data/config.json`. Then on the reader: open **Plugin Hub** in its web page, add the custom catalog `https://coefficiencies.com/crosspoint/catalog.json`, install **Paperboy**, and enter your server URL and token on the **Paperboy** card under Settings.

## Configuration

`data/config.json`:

| Key | Default | |
|---|---|---|
| `instapaper` | | `consumer_key`, `consumer_secret`, `username`, `password` |
| `feedbin` | | `email`, `password` |
| `public_url` | | The address the reader uses, e.g. `https://example.com/paperboy`. Needed for Sync all. |
| `archive_at` | `95` | Percent read that counts as finished |
| `port` | `17300` | |
| `bind` | `0.0.0.0` | |
| `reader_sync` | `true` | Tidy the reader while it's in File Transfer mode |
| `broadcast` | `["255.255.255.255"]` | Where to look for the reader |
| `token` | generated | What the reader uses to authenticate |

At least one of `instapaper` and `feedbin` is required.

## Server API

| Route | Auth | |
|---|---|---|
| `GET /health` | none | `ok` |
| `GET /lists?token=` | token in URL | The reader's list picker |
| `GET /list/<source>?page=&limit=` | Bearer | Articles, paged; page 1 starts with Sync all |
| `GET /f/<token>/<file>` | token in path | Bundle files: `<Name>.epub` and `<Name>.epub.meta.json` |
| `GET /epub/<id>` | Bearer | One article's EPUB |
| `POST /progress` | Bearer | `{"id", "percent", "event_id"}` from the reader |

The firmware can't send headers on bundle downloads or the list index, so those carry the token in the URL; the server's log redacts it.

## Development

```sh
python3 -m unittest discover -s tests -v      # offline tests, fake sources and a fake reader
python3 tools/build_catalog.py                # build the plugin catalog into dist/crosspoint/
PAPERBOY_DATA=./data python3 -m paperboy      # run without Docker
```

The plugin lives in `plugin/`. To release it: bump `version` in `plugin/manifest.json`, run `tools/build_catalog.py`, and publish `dist/crosspoint/`. Tagging `vX.Y.Z` builds the Docker image for amd64 and arm64.

## License

MIT
