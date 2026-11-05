# Groovehound: notes for Claude

Self-hosted wishlist, new-release tracker and recommender built on MusicBrainz and
ListenBrainz. Python backend, one Docker container. Read `README.md` and `docs/roadmap.md`
(the plan, the **Non-goals**, and the **Ground rules** for both APIs) before changing behaviour;
check new feature ideas against the Non-goals and "Decided against". Personal setup
details, if present, are in the git-ignored `CLAUDE.local.md`.

## Firm rule: respect the MetaBrainz APIs

MusicBrainz and ListenBrainz are run by a small non-profit. Every request (the Cover Art
Archive's too) goes through `brainz/http.py` (`JsonClient`): one shared `RateLimiter` per
service, ≥1.5 s from each response to the next request, a User-Agent with the operator's contact
(required), backoff on 503/429 honouring `Retry-After` / `X-RateLimit-Reset-In`. Never add a
request path that bypasses it, never lower `MIN_INTERVAL` for the public servers, and never
schedule background work at a fixed time of day (randomized trickles only). Prefer
lookups/browses to search; search only on an explicit user action (the one exception: search
as you type against a mirror of your own that's opted in, roadmap 7b; never musicbrainz.org). Cache raw responses
(`api_cache`) and ask for everything a feature needs in one `inc=`. When in doubt, make fewer
requests. See the roadmap's Ground rules.

## Architecture decisions

- Same stack as tagdrifter (`../tagdrifter`): FastAPI + Jinja2/HTMX (vendored, no JS build),
  SQLite with append-only `MIGRATIONS` and `PRAGMA user_version`, stdlib HTTP (`urllib`).
  `brainz/http.py` and `brainz/musicbrainz.py` started as a port of tagdrifter's
  `musicbrainz.py`; keep fixes in step where they apply to both. **No shared package yet**:
  revisit once both projects have settled (see the roadmap's open questions).
- Work goes through feature branches and squash-merged PRs: `main` is protected (ruleset
  "Protect main": PR required, squash only, required checks `test` and `build`) and publishes
  `ghcr.io/emilystap/groovehound:latest`. Workflows `ci.yml` (ruff + pytest) and `image.yml`
  (build, smoke test with `--network none`, push from main and `v*` tags), both starting with
  a `changes` job (`.github/scripts/code-changed.sh`) that skips them for docs-only changes
  (`docs/**`, `CLAUDE.md`, `LICENSE`). Never use `paths-ignore` on these workflows: the
  required checks would never report.
- Actions are pinned to full commit SHAs and Docker base images to digests, with Dependabot
  (`.github/dependabot.yml`) keeping them current. Only `image.yml`'s `publish` job can write
  to GHCR.
- **Multi-user ready, single user today**: user-owned rows carry `user_id`; caches are shared.
  Routes get the user only through `web/app.py::current_user` (always `OWNER_ID` for now).
- **Config precedence**: defaults < `config.toml` < `GROOVEHOUND_*` env vars < CLI flags.
- Deployments may sit behind an HTTPS reverse proxy: templates use root-relative URLs.
- **The UI is meant to be replaceable**: logic (searching, caching, rules about requests,
  storage) lives outside `web/` (`search.py`, `db.py`, `brainz/`) and never imports from it;
  routes in `web/app.py` only parse input, call that code and pick a template. Keep it that
  way so the templates and CSS can be reworked, or a JSON API added, without touching logic.
- Every page credits MusicBrainz, ListenBrainz and the Cover Art Archive (footer). Tags,
  genres and ratings are CC BY-NC-SA.

## Status

Phases 0 (scaffolding) and 1 (the wishlist: 1a search and save, 1b the wishlist page, 1c where
to buy, 1d quick capture, 1e installable on Android, 1f share to search, 1g choose the edition)
done; phase 2a (follow / unfollow artists), 2b (release group types per follow) and 2c (slow
reconciliation: the background re-browse, `reconcile.py`), 2e (covers ahead of time,
`covers_ahead.py`) and 2d (the home page's new and upcoming
releases, `new_releases.py`) done; phase 3a (ListenBrainz's sitewide fresh releases, `fresh.py`)
and 3b (personal fresh releases, `personal.py`) done; phase 4a ("Created for you" playlists on
the Discover page, `playlists.py`) and 4b (similar artists via LB Radio on the artist page,
`similar.py`) done; phase 5a (the library scan, `library.py`), 5b ("In your library"
badges), 5e (unplayed, `listens.py`), 5f (rediscover: the listen history, `history.py`, and the
page, `rediscover.py`) and 5i (covers from the library, `covers.py` / `library.cover_art`) and 5h (recommended albums,
the CF recommendations once 4c, `recommended.py`) and 5j (library artists, with 5d's suggested
follows, `library_artists.py`) done; 5k (tracks, `tracks.py`) has its backend, its UI waits
for the UI overhaul (planned in `docs/ui-roadmap.md`): U0 (covers at 500 px as files under
`covers/` beside the database, with each cover's tint, `tint.py`) and U1 (tokens and the shell)
done; U2's album page (`/albums/<mbid>`, `album.py`) and track lists from your files (`tracks.local_track_list`) done, its tracks-ahead trickle held until the MusicBrainz mirror is running; U3 (Home as shelves, `shelves.py`) and U4 (artist pages, `artist.py`) done; U5's wishlist (segments and plain rows) done, its Library tab next. Links to other sites open in a new tab (`target="_blank"`).

## Commands

```powershell
uv sync
uv run pytest
uv run ruff check . ; uv run ruff format .
uv run groovehound config         # settings in effect, secrets hidden
uv run groovehound serve          # web UI on http://127.0.0.1:8080
```

## Conventions

- Ruff, line length 100. `.gitattributes` forces LF line endings (and `.editorconfig` tells
  editors). On Windows, write files with the Edit/Write tools, or in Python with
  `newline="\n"` (`write_text` and `open(..., "w")` otherwise write CRLF); not with
  PowerShell's `Set-Content` / `Out-File`. Git's "CRLF will be replaced by LF" warning means
  something did.
- Tests never call the real APIs (`tests/conftest.py` blocks `http._urlopen`); pass
  `FakeOpener` to clients. Record real responses as fixtures under `tests/fixtures/` sparingly.
