# Groovehound

A self-hosted companion for people who keep their own music library:

- **Wishlist**: heard something great out in the wild? Look it up on MusicBrainz, save it, and
  find it later with links to buy it (Bandcamp, Qobuz, ...).
- **Follow artists**: new releases from the artists you follow, on the home page.
- **Discover**: artist and release suggestions from ListenBrainz.

Status: early scaffolding. See [docs/roadmap.md](docs/roadmap.md) for the plan.

## A good MetaBrainz citizen

Groovehound runs on MusicBrainz and ListenBrainz data, both run by the non-profit MetaBrainz
Foundation. It keeps well inside their API rules: at most one request every 1.5 seconds to
each, a User-Agent naming your contact address, backing off when asked, no fixed-time
background jobs, and caching everything it fetches. The details are in the roadmap's
"Ground rules". If you use MusicBrainz data heavily, consider
[supporting MetaBrainz](https://metabrainz.org/donate).

## Running

```powershell
uv sync
Copy-Item config.example.toml config.toml   # then set [brainz] contact
uv run groovehound serve                    # http://127.0.0.1:8080
```

Or with Docker: see `compose.yaml`.

### On your phone

Served over HTTPS (behind a reverse proxy, with root-relative URLs throughout), Groovehound can
be installed as an app: in Chrome on Android, open it and choose *Install app* (or *Add to
Home screen*) from the menu. It then opens in its own window, without the browser bar. Chrome
only offers this over HTTPS or on `localhost`, and there is no offline mode: it needs the
server.

## Development

```powershell
uv run pytest
uv run ruff check . ; uv run ruff format .
```

Tests never touch the network: `tests/conftest.py` blocks it, and clients take a fake opener.
