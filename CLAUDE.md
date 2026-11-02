# Groovehound: notes for Claude

Self-hosted wishlist, new-release tracker and recommender built on MusicBrainz and
ListenBrainz. Python backend, one Docker container. Read `README.md` and `docs/roadmap.md`
(the plan, the **Non-goals**, and the **Ground rules** for both APIs) before changing behaviour;
check new feature ideas against the Non-goals and "Decided against". Personal setup
details, if present, are in the git-ignored `CLAUDE.local.md`.

## Firm rule: respect the MetaBrainz APIs

MusicBrainz and ListenBrainz are run by a small non-profit. Every request goes through
`brainz/http.py` (`JsonClient`): one shared `RateLimiter` per service, ≥1.5 s from each
response to the next request, a User-Agent with the operator's contact (required), backoff on
503/429 honouring `Retry-After` / `X-RateLimit-Reset-In`. Never add a request path that bypasses
it, never lower `MIN_INTERVAL` for the public servers, and never schedule background work at a
fixed time of day (randomized trickles only). Prefer lookups/browses to search; search only
on an explicit user action. Cache raw responses (`api_cache`) and ask for everything a feature
needs in one `inc=`. When in doubt, make fewer requests. See the roadmap's Ground rules.

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

Phases 0 (scaffolding), 1a (search and save to the wishlist), 1b (the wishlist page), 1c
(where to buy), 1d (quick capture), 1e (installable on Android) and 1f (share to search) done.
Next: phase 1g (choose the edition on the wishlist item).

## Commands

```powershell
uv sync
uv run pytest
uv run ruff check . ; uv run ruff format .
uv run groovehound config         # settings in effect, secrets hidden
uv run groovehound serve          # web UI on http://127.0.0.1:8080
```

## Conventions

- Ruff, line length 100. `.gitattributes` forces LF line endings.
- Tests never call the real APIs (`tests/conftest.py` blocks `http._urlopen`); pass
  `FakeOpener` to clients. Record real responses as fixtures under `tests/fixtures/` sparingly.
