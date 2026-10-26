# Paperboy

Your unread [Instapaper](https://www.instapaper.com) articles and starred [Feedbin](https://feedbin.com) articles on a [CrossPoint](https://crosspointreader.com) e-reader.

- Browse your reading list on the reader and download articles as EPUBs, with title covers and the articles' images.
- **Sync all** downloads, in one go, every article that isn't on the reader yet.
- Turn onto an article's last page ("The end") and it's archived in Instapaper (or unstarred in Feedbin) the next time the reader is on Wi-Fi.
- While the reader is in File Transfer mode on your home network, it's tidied to match your list: archived articles removed, new ones added.

Two parts: this **server**, which you run at home and which talks to Instapaper and Feedbin, and a **CrossPoint plugin** on the reader. Your passwords stay on your server.

Full setup guide: https://coefficiencies.com/apps/paperboy/

## Quick start

You need Docker, a CrossPoint reader on firmware 2.0 or later, and an Instapaper personal access token (and/or a Feedbin account). To get the token, create an application at [instapaper.com/developers/applications](https://www.instapaper.com/developers/applications) and generate a personal access token for it.

```sh
mkdir -p paperboy/data && cd paperboy
curl -O https://raw.githubusercontent.com/tommertron/paperboy/main/docker-compose.yml
curl -o data/config.json https://raw.githubusercontent.com/tommertron/paperboy/main/examples/config.json
# edit data/config.json, and set `user:` in docker-compose.yml to your `id -u`:`id -g`
docker compose up -d
```

No Docker? It's plain Python 3.9+ (`requests` and `Pillow`) and runs fine on a Mac; see [On a Mac, without Docker](https://coefficiencies.com/apps/paperboy/#on-a-mac-without-docker) for a launch agent setup.

On first start a `token` is added to `data/config.json`. Then on the reader: open **Plugin Hub** in its web page, add the custom catalog `https://coefficiencies.com/crosspoint/catalog.json`, install **Paperboy**, and enter your server URL and token on the **Paperboy** card under Settings.

## Configuration

`data/config.json`:

| Key | Default | |
|---|---|---|
| `instapaper` | | `access_token`: a personal access token (Instapaper API v2). Configs from before 1.1 with `consumer_key`, `consumer_secret`, `username` and `password` still work until Instapaper turns xAuth off on 2027-09-30 |
| `feedbin` | | `email`, `password` |
| `public_url` | | The address the reader uses, e.g. `https://example.com/paperboy`. Needed for Sync all. |
| `archive_at` | `95` | Percent read that counts as finished. Reaching an article's end page always counts, however short it is |
| `port` | `17300` | |
| `bind` | `0.0.0.0` | |
| `reader_sync` | `true` | Tidy the reader while it's in File Transfer mode |
| `library_author` | off | Set to a name such as `"Paperboy"` to give every article that author in its EPUB, so the reader's Library groups your articles together on the Author tab instead of mixing them in with your books. The real author is still on the cover and the article. Changing it refreshes the articles on the reader at the next home tidy-up, which resets your place in any half-read article |
| `broadcast` | `["255.255.255.255"]` | Where to look for the reader |
| `token` | generated | What the reader uses to authenticate |

At least one of `instapaper` and `feedbin` is required.

## Web page

Open the server's address in a browser (for example `http://localhost:17300/`) and sign in with the token. You get the reading list, which articles are already on the reader, the last home tidy-up, an EPUB download for each article, and a **Finished** button that archives or unstars an article without reading it on the reader.

## Server API

| Route | Auth | |
|---|---|---|
| `GET /health` | none | `ok` |
| `GET /` | none | The web page: your reading list, what's on the reader, and a Finished button. It asks for the token |
| `GET /status` | Bearer | What the web page shows |
| `POST /finish` | Bearer | `{"id"}`: archive or unstar an article now |
| `POST /refresh` | Bearer | Re-read Instapaper/Feedbin on the next request |
| `GET /lists?token=` | token in URL | The reader's list picker: one combined list, so the reader opens straight onto it |
| `GET /list/<source>?page=&limit=` | Bearer | Articles (`all`, `instapaper` or `feedbin`), paged; page 1 starts with Sync all |
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
