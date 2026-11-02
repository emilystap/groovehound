# Groovehound roadmap

Groovehound is a self-hosted companion to a personal music library. It does three things:

1. **Wishlist**: look up a release you heard "in the wild" on MusicBrainz and save it for
   later, with links to buy it (Bandcamp, Qobuz, ...).
2. **Follow artists**: the home page shows new releases from the artists you follow.
3. **Discover**: suggest artists and releases from ListenBrainz.

Single user today; the schema and code are ready for more (see "Users").

## Non-goals

All three answer one question: *what should I listen to, or go and get, next?* A feature
belongs only if it serves that, uses data already at hand (or a few bounded requests), and
leaves jobs that other tools already do well to those tools. So Groovehound does **not**:

- **Play music.** Navidrome, Symfonium and the rest do that; Groovehound links out to them.
- **Write to your library or to other services.** The music folder is mounted read-only and
  Navidrome is only ever read. The one possible exception, keeping the wishlist in a
  MusicBrainz collection, would be opt-in and only run when you ask.
- **Manage files.** No tagging, renaming, moving or deleting: that's Picard (or beets).
- **Fetch music automatically.** No downloaders or Lidarr-style automation. Groovehound shows
  where to buy something; buying it is up to you.
- **Scrape stores.** Purchase links are built locally or come from MusicBrainz; no price or
  availability checks.
- **Record listens or be social.** ListenBrainz records and shares listens; Groovehound only
  reads them.

Ideas turned down for these reasons are listed under "Decided against", with the reason, so
they don't come back every few months.

## Ground rules: being a good MetaBrainz citizen

These are project rules, not tuning knobs. MusicBrainz and ListenBrainz are run by a small
non-profit (MetaBrainz), and every feature below is designed around these limits.

**Both APIs**

- One request at a time per service, spaced **at least 1.5 s from each response to the next
  request** (the documented limit is 1/s; counting from the response means network jitter can't
  bunch requests up). One process-wide limiter per service, shared by every thread.
- A User-Agent of `Groovehound/<version> ( <contact> )`. The operator's contact (an email or
  URL) is **required**: any outbound feature refuses to start without it.
- Errors stop the run. MusicBrainz 503: back off 15 s then 60 s (or `Retry-After`, if longer),
  then give up; ListenBrainz 429: wait `X-RateLimit-Reset-In`. A failed run pauses that service
  for an hour, so repeated clicks during an outage don't keep poking it.
- Background jobs **never wake at a fixed time of day**. They trickle: wake at a random interval,
  do a small batch, sleep again. Refresh intervals are randomized so work doesn't bunch up.
- Cache raw responses (compressed) so new features read from the cache instead of refetching.
- Tests never touch the network; `tests/conftest.py` blocks it, and recorded fixtures stand in.

**MusicBrainz specifics**

- Prefer **lookups and browses** (by MBID) over **search**. Search is only ever run when the user
  presses *Search*, never as-you-type.
- Browse with `limit=100` (the maximum) and stop after a fixed number of pages.
- Releases on new-release pages come from **one browse per followed artist**, refreshed on a slow
  randomized schedule, not by polling. Day-to-day "what's new" comes from ListenBrainz's fresh
  releases feed (one request for everything; see phase 3).
- MusicBrainz doesn't publish change notifications; its docs ask clients not to poll for changes.
  So nothing here re-checks a release or artist faster than every few days.
- An optional `api_root` lets you point at a mirror of your own (musicbrainz-docker) with a
  shorter interval. musicbrainz.org itself never gets less than 1.5 s.

**ListenBrainz specifics**

- Honour `X-RateLimit-Remaining` / `X-RateLimit-Reset-In` on every response, on top of the
  spacing above.
- Never page through a user's whole listening history with `/listens` (the docs ask clients not
  to). Use bounded queries only, or the export API.
- Recommendations and generated playlists change daily or weekly, so fetch them **at most once a
  day**. Endpoints marked experimental (e.g. CF recording recommendations) are isolated so
  breakage doesn't spread. `labs.api.listenbrainz.org` has no published limits or stability
  promise, so it is **not used**.
- The user token is a secret: kept in config/env only, never logged, only sent to ListenBrainz.

**Data licences and attribution**

- MusicBrainz core data (artists, releases, release groups, relationships) is CC0.
  Tags, genres, ratings and annotations are **CC BY-NC-SA 3.0**: fine for personal,
  non-commercial use, but the UI credits MusicBrainz wherever they're shown. ListenBrainz data
  is CC0. The footer always credits both, with links.
- Cover art comes from the Cover Art Archive (`coverartarchive.org`) only, as 250 px
  thumbnails, loaded lazily.
- MetaBrainz asks for financial support from commercial users. This project is for personal
  use; if that ever changes, revisit this.

## Users

Single-user now, multi-user ready:

- A `users` table from day one. Every user-owned row (wishlist, follows, dismissed suggestions)
  carries a `user_id`. Migration 1 creates user 1 (the owner).
- Shared caches (MusicBrainz responses, CAA thumbnails) have no `user_id`: one artist followed
  by two users is fetched once.
- Per-user settings (ListenBrainz username and token, notification targets) go in the `users`
  table, not global config, once they're editable in the UI. Until then they come from config
  and apply to user 1.
- Login starts as tagdrifter's single password (scrypt + signed cookie). Adding users later
  means a username on the login form and a session that carries a `user_id`. No route reads
  "the user" except through one dependency (`current_user`), so the switch is in one place.

## Phases

Each phase is one or more PRs. ✅ = done.

### 0. Scaffolding ✅

Repo, tooling (uv, ruff, pytest, CI), config loading, SQLite with append-only migrations,
FastAPI + Jinja + HTMX shell (`/`, `/healthz`), Dockerfile + compose. Shared HTTP layer for both
APIs (`brainz/http.py`: rate limiter, User-Agent, backoff, gzip, network blocked in tests);
MusicBrainz and ListenBrainz clients with the first few calls; no features yet.

### 1. Wishlist

The core loop: hear something → find it → save it → buy it later.

- 1a ✅ **Search and save.** A search box (release group by title + artist; recording by title +
  artist for "I heard this song", which lists the release groups it appears on). One search
  request per *Search* press. Results show title, artist credit, type, first release date, CAA
  thumbnail. *Add to wishlist* saves the release group (and optionally a specific release).
- 1b ✅ **Wishlist page.** Sorted by date added; notes; "where I heard it" (free text); mark as
  *bought* (kept, hidden by default) or remove. Mobile-friendly: this is used from a phone.
- 1c ✅ **Where to buy.** Per item: search links built locally, no requests (Bandcamp, Qobuz
  store search with artist + title; the Qobuz store is `[stores] qobuz`), plus links MusicBrainz
  already has. One browse of the release group's releases with `inc=url-rels` gives
  "purchase for download", "purchase for mail-order" and "streaming" URLs for every edition,
  often pointing straight at the Bandcamp or Qobuz page (the saved edition's first, one per
  store). It's made only when *Where to buy* is opened, not when the item is added, so items
  nobody looks at cost nothing and older items need no backfill; cached, and refreshed (again
  only when opened) after a randomized 30–60 days.
- 1d **Quick capture.** A minimal form that saves free text ("song with the saxophone at the
  café, 2026-11-01") when you can't search right now; turn it into a real wishlist item later.
- 1e **Installable on Android** (a Progressive Web App): a web app manifest (name, icons,
  theme colour, `display: standalone`) so Chrome's *Install app* puts Groovehound on the home
  screen and opens it in its own window, without the browser bar. Chrome only offers this
  over HTTPS (or `localhost`), so it needs the reverse proxy. No offline mode: everything
  lives on the server.
- 1f **Share to search** (needs 1e): a `share_target` in the manifest, so Groovehound shows up
  in Android's share sheet, aimed at Pixel's Now Playing (*Share* on a song in its history;
  the live recognition screen has no share). Its share is two lines, recorded in
  `tests/fixtures/now_playing_shares.txt`:

      DARE by Gorillaz
      https://www.google.com/search?q=DARE+by+Gorillaz

  Split the first line at the **last** " by " (a title can contain one: "Stand by Me by
  Ben E. King"); if the text is missing, read the same words from the link's `q` parameter,
  locally (links are never fetched). The result opens a *song* search pre-filled with title and
  artist; you still press *Search*, so it's one request as usual and you can fix a bad guess
  first. The share names only the main artist, not featured artists, which is fine: the
  artist field matches part of a credit ("Gwen Stefani" finds "Gwen Stefani feat. Eve").
  A share that doesn't parse lands in quick capture (1d) as free text.

### 2. Follow artists

- 2a **Follow / unfollow** from search results, wishlist items, and an artist page. Following
  fetches the artist's release groups once (`/release-group?artist=<mbid>&limit=100`, at most
  a few pages; official releases only, by default) into the cache, so what's already out isn't
  reported as new.
- 2b **Release group types.** Per follow (default: album + EP; singles optional; live,
  compilation, remix etc. off) since secondary types are noisy.
- 2c **Slow reconciliation.** The background trickle re-browses each followed artist every
  randomized 7–14 days (a few artists per wake-up), catching anything phase 3's feed missed.
  Release groups not seen before become *new* (with the date first seen).
- 2d **Home page.** New and upcoming releases from followed artists, newest first, with
  *dismiss*, *wishlist*, and *open on MusicBrainz*.

### 3. Fresh releases from ListenBrainz

- 3a ListenBrainz's sitewide fresh-releases feed (`/1/explore/fresh-releases/`) lists every
  release around a date in **one request**. Fetched once a day at a random time, filtered
  locally by followed artist MBIDs. This makes new releases show up within a day while
  MusicBrainz sees only phase 2's slow per-artist browses.
- 3b With a ListenBrainz username configured: the personal fresh-releases feed
  (`/1/user/<name>/fresh_releases`) as a "you might care about" section (artists you listen to
  but don't follow), with *Follow*.

### 4. Discovery

- 4a "Created for you" playlists (Weekly Exploration etc., via
  `/1/user/<name>/playlists/createdfor` then the playlist): their recordings grouped by release,
  with *Wishlist* and *Follow artist*. Daily at most.
- 4b Similar artists for an artist you follow, via LB Radio's artist endpoint
  (`/1/lb-radio/artist/<mbid>`), only on request, cached for weeks.
- 4c CF recording recommendations (experimental endpoint), if they turn out useful.
- Dismissed suggestions stay dismissed (per user).

### 5. Library awareness

Know what's already in your music library, so Groovehound can say "you have this" instead of
suggesting it again, and point you at what you own but haven't played. **No MusicBrainz
requests**; ListenBrainz only for listening stats (5e). 5a and 5b need only phase 1 and could
move earlier; 5d needs phase 2; 5f works best with phases 2–3.

- 5a **Library scan.** Mount the music folder into the container read-only (e.g.
  `/music:ro`, `library.path` in config) and read the MusicBrainz tags Picard writes (via
  `mutagen`: FLAC/Ogg/Opus Vorbis comments, MP3 `TXXX`/`UFID`, MP4 freeform atoms):
  *MusicBrainz Release Group Id*, *Album Id* (the release), *Album Artist Id* and *Artist Id*.
  One row per release in a per-user `library` table (release group, release, album artist
  MBIDs, folder, file count, last seen), plus a per-file mtime/size index so rescans only
  re-read changed files. Files without MBID tags are counted and listed, not guessed at by name.
  Scans run on demand (*Rescan* button, `groovehound scan`) and on the same randomized trickle
  as other background work; it's local disk, but a big library still shouldn't be re-read all
  at once. A release with an album id but no release group id is matched against wishlist
  editions only; resolving it would cost a lookup per release, so it's left alone.
- 5b **"In your library".** Search results, wishlist items and new releases get a badge when
  their release group is in the library; owned ones sort after (or hide from) new-release lists. A
  wishlist item that appears in the library is offered as *bought* ("found in your library on
  2026-…"), not silently changed.
- 5c **Navidrome instead of a folder** (optional): the same table filled from Navidrome's
  Subsonic API (read-only; it exposes MusicBrainz ids), for when Groovehound can't see the files.
  **Check first** that it gives the *release group* MBID: the Subsonic API reliably has the
  release (album) and artist MBIDs, and Navidrome stores the release group's, but it may be in
  its internal API only, which can change without notice. With release MBIDs alone, matching
  would need a MusicBrainz lookup per album, which the ground rules rule out; then match
  wishlist editions only, or skip 5c.
- 5d **Suggest follows** (needs phase 2): album artists with N+ release groups in the library
  that you don't follow, ranked by how many you own, with *Follow* and *dismiss* (stays
  dismissed). Following one also seeds the "already out" list from the library, so albums you
  own are never reported as new. Later, combine with ListenBrainz listen counts (phase 4).
- 5e **Unplayed.** With a ListenBrainz username: your all-time stats per release group and per
  artist (`/1/stats/user/<name>/release-groups` and `/artists`, `range=all_time`, paged with
  `count`; public, no token), fetched at most once a day. Library minus played = unplayed: a
  *never played* badge on library items and an unplayed count per artist. Listens are matched by
  the MBIDs ListenBrainz mapped them to, so a listen it couldn't map counts as unplayed; say so
  in the UI. **Check first** whether the stats lists stop at a top N: if they do, records
  played only once or twice drop off, and a one-off ListenBrainz export (not paging `/listens`)
  fills the gap.
- 5f **Rediscover.** One page of unplayed releases from the library, picked at random in fixed
  proportions: some by artists you play most, some by artists you rarely play, plus new releases
  (phases 2–3) by your most-played artists that you haven't played yet. *Shuffle* draws again
  (locally, no requests); *dismiss* stays dismissed. Fixed proportions on purpose: no tunable
  weights or saved mixes.
- 5g **Open in your player.** *Open in Navidrome* on library items, linking to the album in
  Navidrome's web UI. That needs Navidrome's own album id, so it uses 5c's read-only connection
  to list albums with their MBIDs (your own server, no MetaBrainz requests), even when the file
  scan is the library source. *Open in Symfonium* isn't possible yet: as of 2026-11 Symfonium
  has no links a web page can open (deep links are an open feature request on its forum, and
  its broadcast-intent API is for other Android apps). Revisit if it adds them.

### 6. Notifications and polish

- 6a Gotify (like tagdrifter) when a followed artist has a new release; a weekly digest option.
- 6b Login (tagdrifter's scrypt + signed cookie, CSRF guard).
- 6c Release calendar: upcoming releases (future release dates) on their own page.
- Later: ntfy / webhooks, an RSS/Atom feed of new releases, export/import of follows and the
  wishlist (JSON / CSV), MusicBrainz collections sync (needs OAuth; would let the wishlist live
  in a MusicBrainz collection too).

## Decided against

See "Non-goals". Each entry gives the date and the reason.

- **Creating playlists in Navidrome** from Rediscover (2026-11-02): needs write access to
  Navidrome and keeping the playlist in sync, which is a player's job. Link to the album instead
  (5g).

## Open questions

- Share code with tagdrifter as a package? Decided (2026-11-01): **not yet**. The overlap
  today is about 200 lines (rate limiter, User-Agent, backoff); a package would need its own
  repo, releases and version pins in both projects. Revisit when both are mature, or when more
  is duplicated (login/CSRF, Gotify notifications, the MusicBrainz cache). Candidates then:
  `brainz/http.py` (the limiter and `JsonClient`), tagdrifter's `web/auth.py`, `notify.py`.
- Wishlist granularity: release group (the album) by default, with an optional specific release
  (edition, format). Confirm once 1a is in use.
- Thumbnails: link CAA directly from the browser (no server work, but every view hits CAA),
  or proxy and cache under the data dir. Start direct; revisit if it feels slow.
