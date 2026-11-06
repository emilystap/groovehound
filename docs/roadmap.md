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
  Navidrome is only read, with one exception: 5n's playlists, opt-in, sent only when you press
  the button, and only ever the playlists Groovehound made. The other possible exception,
  keeping the wishlist in a MusicBrainz collection, would be opt-in and only run when you ask.
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

- Prefer **lookups and browses** (by MBID) over **search**. On musicbrainz.org, search is only
  ever run when the user presses *Search*, never as-you-type. The one exception is a mirror of
  your own that you've opted in (phase 7b); musicbrainz.org never gets search as you type, and
  the code refuses it, not just the UI.
- Browse with `limit=100` (the maximum) and stop after a fixed number of pages.
- Releases on new-release pages come from **one browse per followed artist**, refreshed on a slow
  randomized schedule, not by polling. Day-to-day "what's new" comes from ListenBrainz's fresh
  releases feed (one request for everything; see phase 3).
- MusicBrainz doesn't publish change notifications; its docs ask clients not to poll for changes.
  So nothing here re-checks a release or artist faster than every few days.
- An optional `api_root` lets you point at a mirror of your own (musicbrainz-docker) with a
  shorter interval. musicbrainz.org itself never gets less than 1.5 s. A mirror covers
  MusicBrainz only: the Cover Art Archive and ListenBrainz are still the public servers, with
  their limits (see phase 7).

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
- Cover art comes from the Cover Art Archive (`coverartarchive.org`), except for albums in the
  library, whose covers come from the files when they have art (5i), at 500 px (U0 in
  `docs/ui-roadmap.md`), loaded lazily. Wishlist covers go through the server's cover cache
  (`covers.py`, `/cover/<mbid>`): fetched through `brainz/http.py` like any other request,
  once per release group when first shown, refreshed (again only when shown) after a
  randomized 30–60 days. Search results link the archive directly: they're seen once, and
  would otherwise queue behind the limiter.
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
- 1d ✅ **Quick capture.** A one-line form (home page and wishlist) that saves free text ("song
  with the saxophone at the café") with the date, when you can't search right now. *Look it up*
  opens the search page with the text shown above the form; saving a result moves the text
  (and its date) into that item's notes and the capture is gone. No requests of its own.
  When a search doesn't find it, *Not what you were looking for?* below the results saves the
  query (editable, e.g. "360 by Charli xcx (song)") as a capture to come back to.
- 1e ✅ **Installable on Android** (a Progressive Web App): a web app manifest (name, icons,
  theme colour, `display: standalone`) so Chrome's *Install app* puts Groovehound on the home
  screen and opens it in its own window, without the browser bar. Chrome only offers this
  over HTTPS (or `localhost`), so it needs the reverse proxy. No offline mode: everything
  lives on the server, so no service worker either (Chrome no longer requires one). Icons
  are drawn by `scripts/make_icons.py` (stdlib only).
- 1f ✅ **Share to search** (needs 1e): a `share_target` in the manifest, so Groovehound shows up
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
  A share that doesn't parse lands in quick capture (1d) as free text, filled in but not saved
  until you press *Save*. (`/share`, a GET `share_target`; parsing in `share.py`.)
  Also here: the installed app's gesture bar takes the manifest's `theme_color` (checked on a
  Pixel, Chrome 156; `viewport-fit=cover` made no difference), and a manifest has one colour,
  so it's the dark top bar's. The page's `theme-color` still follows the theme for the status
  bar, and `user_preferences` asks for white in light mode where Chrome supports it.
- 1g ✅ **Choose the edition on the wishlist item.** An album search's response lists each release
  group's editions with only id, title and status (a search can't ask for more), so the
  *edition* select shows a row of identical titles: 15 × "BITE ME" for Reneé Rapp's album.
  Instead, *Add to wishlist* saves the album (any edition), and the item gets *Choose edition*:
  every edition with what tells them apart, as on its MusicBrainz page: release date, country,
  status, formats and discs ("2×CD", "Digital Media"), track count, label and catalogue number,
  and the disambiguation ("explicit", "deluxe"). All CC0. Barcode and packaging are left out
  (2026-11-01): they matter mostly for buying a physical copy, and the MusicBrainz page has them.
  - **Requests:** the browse 1c already makes when *Where to buy* opens, asking for
    `inc=media labels url-rels` instead of `url-rels`: one request, one cache entry, feeding
    both (`releases.py`). Responses cached before the change lack media and labels: they still
    show their links, but count as due, so they're refetched when next opened.
  - **UI:** a list, not a `<select>` (an option is one line of plain text). One row per
    edition, two short lines each: date · country · formats · tracks (· status, unless
    official) over title (if it differs) · label · catalogue number · disambiguation. Picked
    with a radio button, plus *Any edition* to go back; the item then shows the chosen
    edition's first line. Editions identical in everything shown share a row ("×3"); official
    ones first, then oldest first. Choosing one also puts its links first under *Where to buy*.
  - The search page's select goes for album searches, which have nothing to show in it. Song
    searches keep it: their results already carry date, country and format.

### 2. Follow artists

- 2a ✅ **Follow / unfollow** from search results, wishlist items, and an artist page. Following
  fetches the artist's release groups once (`/release-group?artist=<mbid>&limit=100`, at most
  a few pages; official releases only, by default) into the cache, so what's already out isn't
  reported as new.
  - **Requests:** one browse of the artist's albums and EPs (`type=album|ep`,
    `release-group-status=website-default`, `inc=artist-credits`; a page per 100, at most 3),
    cached for a randomized 7–14 days (2c's refresh interval) and shared by the artist page
    and *Follow*: open the page, follow, and it's one browse. Following someone already
    followed, or again while the browse is cached, asks nothing. The artist's name comes from
    the browse's credits; the artist lookup (cached 30–60 days) is only made for an artist
    with no albums or EPs. A 404 is cached too, so a bad link isn't asked about again soon.
  - **"Already out":** every browse records its release groups in `release_groups_seen`
    (shared, with the date first seen). A release group is new to a follower if it was first
    seen after they followed: that's what 2c and 2d read. A failed browse follows no one,
    since there'd be nothing to compare against.
  - **UI:** each credited artist on a search result or wishlist item gets their name (linking
    to their artist page, `/artists/<mbid>`) and *Follow* / *✓ Following*; a click updates
    every button for that artist on the page. Various Artists and [unknown] get none. The
    artist page lists albums and EPs newest first (live, compilations, remixes etc. folded
    away), each with *Add to wishlist*. `/artists` lists who you follow (*Artists* in the top
    bar). Wishlist items now keep the credited artists' names; items saved before 2a fall back
    to the credit (one artist) or "artist 1, 2" (several).
- 2b ✅ **Release group types.** Per follow (default: album + EP; singles optional; live,
  compilation, remix etc. off) since secondary types are noisy.
  - **Choices:** albums, EPs, singles (at least one), and also live, compilations, remixes,
    soundtracks, DJ mixes, mixtapes, demos, and "spoken word, interviews and the like" (the
    rarer secondary types, and any MusicBrainz adds later). A release group counts if its
    primary type is chosen and so is each of its secondary types. Set under *For Albums, EPs*
    on the artist page and on `/artists` (`follows.types`, migration 7). Unfollowing and
    following again starts over from the default, which is `[follows] types` in the config
    (or `GROOVEHOUND_FOLLOW_TYPES`; albums and EPs if unset). A default with singles browses
    them when following, before the follow is saved, so a failed browse follows no one.
  - **Requests:** secondary types cost nothing: the album and EP browse returns them anyway.
    Singles get a browse of their own (`type=single`, cached as `artist-singles` like the
    other), made when someone turns singles on (unless cached) and kept fresh by the artist
    page while they're on. Artists nobody follows for singles never have it made.
  - **"Already out", for singles:** `follows.singles_since` is when singles were turned on,
    after that browse. A single is new if first seen after it; albums and EPs keep comparing
    with the follow date (`follows.is_new`). So turning singles on doesn't make every old
    single new, and turning on live etc. later doesn't either (they were in the baseline).
  - **UI:** once singles are browsed, the artist page lists them (folded, like live and
    compilations); turning them on there reloads the page to show them.
- 2c ✅ **Slow reconciliation.** The background trickle re-browses each followed artist every
  randomized 7–14 days (a few artists per wake-up), catching anything phase 3's feed missed.
  Release groups not seen before become *new* (with the date first seen).
  - **Requests:** a thread started with the app (only once a contact is set) sleeps a random
    20–60 min, drawn afresh each time (the first sleep too, so a restart asks nothing at once),
    then re-browses at most 3 artists whose cached browse is due, longest due first: albums
    and EPs, then singles for artists followed for them. Up to ~100 browses a day, against a
    need of one per followed artist every 7–14 days. The same browse, cache and limiter as the
    artist page, so a page opened in between counts. Nothing due, or MusicBrainz paused after
    an error, asks nothing; an error stops the batch and starts the pause (`reconcile.py`).
  - Shown nowhere yet: what's new appears with 2d.
- 2d ✅ **Home page.** New and upcoming releases from followed artists, newest first, with
  *dismiss*, *wishlist*, and *open on MusicBrainz* (the title links there, as everywhere).
  - **Requests:** none. Read from the cached browses (albums and EPs; singles for artists
    followed for them) and `release_groups_seen`, which 2c keeps fresh (`new_releases.py`).
  - **What shows:** release groups of the follow's types that are *coming up* (released after
    today, soonest first, even if announced before you followed) or *new* (newest first,
    undated last): released in the last 90 days, whenever you followed, so following someone
    shows what they put out lately; or first seen after you followed (`follows.is_new`), in the
    last 90 days, which catches releases MusicBrainz got late. A year or month alone ("2026",
    "2026-10") counts from the day the release group was first seen, kept within that year or
    month: an album first seen by a refresh in February 2026 is new until May, not all year,
    and one announced as "2027" is new from 1 January 2027. Already out when you followed, it
    counts from then (it can't be told apart from one out in January: *dismiss* it). A refresh
    that brings the full date makes that count instead. A release group dated more than a year
    before it was first seen is a catalogue addition (an old record MusicBrainz only just got),
    not a new release, and isn't shown.
  - **Dismiss** stays dismissed, per user (`dismissed`, migration 8, with a `kind` so phase 4's
    suggestions can share it). *Add to wishlist* keeps the release listed, marked as saved.
  - **Covers** go through the cover cache (`/cover`), now also for release groups by artists
    you follow: the home page is seen often, so direct links would be slow every time. The
    artist page does the same for an artist you follow; for anyone else it links the Cover Art
    Archive directly, since you may look only once.
- 2e ✅ **Covers ahead of time.** Cached covers make a page fast from the second view on, but the
  first view of a followed artist's page waits on the Cover Art Archive's limiter, one cover per
  1.5 s or more (40 albums: about a minute). Fetch them in the background instead, so the
  cover-heavy UI planned later never waits.
  - **What:** covers of the release groups a follow is for (`follows.wanted`, the cached
    browses), newest first, that aren't cached or whose `COVER_TTL` has passed; the same
    `covers.cover`, cache and Cover Art Archive limiter as `/cover`, so a page view in between
    counts. Release groups of types the follow isn't for are left to the page view.
  - **Requests:** its own trickle, like 2c's: a random 20–60 min asleep, then at most ~10
    covers, at least 1.5 s apart. With 100 follows (~3,000 covers) that's the backlog in a few
    days, then about one request per new release group plus refreshes every 30–60 days (~50–100
    a day). Nothing while the archive is paused after an error; nothing without a contact.
    5i's covers from the library come first for release groups in the library (no request).
    Its own thread (`covers_ahead.py`), started with the app only with a contact. Nothing is
    browsed for it: only the browses 2c or a page view already cached.
  - **Size:** 250 px for now (~40 KB each; ~120 MB for 3,000); decided with the UI overhaul
    (2026-11-02: modelled on streaming apps, so likely bigger). A bigger UI may want 500 px (CAA's
    `front-500`, ~100–150 KB): change the size then, and the 30–60 day refresh replaces the old
    ones without a migration. Past a few hundred MB, keep covers as files under the data
    folder rather than blobs in SQLite, so the database stays small to back up. **Done in U0**
    (`docs/ui-roadmap.md`): 500 px, as files under `covers/` beside the database.

### 3. Fresh releases from ListenBrainz

- 3a ✅ ListenBrainz's sitewide fresh-releases feed (`/1/explore/fresh-releases/`) lists every
  release around a date in **one request**. Fetched once a day at a random time, filtered
  locally by followed artist MBIDs. This makes new releases show up within a day while
  MusicBrainz sees only phase 2's slow per-artist browses (`fresh.py`).
  - **Requests:** one, `days=14` around today (a release often reaches MusicBrainz days after
    its date, and the app may have been down), about 1,000 releases. Cached raw for a
    randomized 22–26 hours and fetched by 2c's background trickle on the first wake-up after
    that, so the time drifts around the clock. Nobody following anyone asks nothing. An error
    pauses ListenBrainz only; the MusicBrainz batch goes on.
  - **"Already out":** each fetch records the followed artists' release groups in
    `release_groups_seen`, so one reported before the artist's next browse is new from the day
    ListenBrainz reported it (`follows.is_new`), and keeps that date once a browse has it.
  - **Home page:** the feed lists releases, not release groups, so a release group is dated by
    its earliest release in the feed, and the feed has no artist names besides the credit's.
    Whenever an artist's browse has the release group, the browse's view wins (first release
    date, all types): a reissue in the feed of an album from 2024 isn't new. The feed only
    fills the gap until the next browse. Its responses (checked 2026-11-02) carry no secondary
    type, so a live album found this way shows as an album until the browse catches up.
- 3b ✅ With a ListenBrainz username configured: the personal fresh-releases feed
  (`/1/user/<name>/fresh_releases`) as a "you might care about" section (artists you listen to
  but don't follow), with *Follow* (`personal.py`).
  - **Requests:** one, `days=14` (past and future), cached raw for a randomized 22–26 hours and
    fetched by the same wake-up as 3a's feed, right after it. No username asks nothing. A 404
    (no such user, or no data yet) is cached like a response, so it's asked again only the next
    day; any other error pauses ListenBrainz. The username comes from config and applies to
    user 1 until per-user settings are editable.
  - **What shows:** each release group once, dated by its earliest release in the feed, if
    none of its credited artists is followed (followed ones are 2d's and 3a's) and it isn't
    dismissed. Various Artists only: not shown. Ranked by the feed's `confidence` (how much you
    listen to the artists), then newest; the best 10.
  - **UI:** a home page section under *New releases*, each with *Follow* for every credited
    artist, *Add to wishlist* and *Dismiss* (shared with 2d's). The feed has only the credit
    for names, so several artists' names are read by splitting it at " & ", ", ", " feat. "
    etc., when that gives one name per artist; otherwise they show as "artist 2". Covers go
    through the cover cache (`/cover` allows release groups in the cached feed).

### 4. Discovery

- 4a ✅ "Created for you" playlists: the ones ListenBrainz generates for the configured user
  (Weekly Exploration, Weekly Jams, Daily Jams) on a *Discover* page, as the albums their
  recordings are on, with *Add to wishlist*, *Follow* for each artist, and *Dismiss* (shared
  with the home page's) (`playlists.py`).
  - **Requests:** the list (`/1/user/<name>/playlists/createdfor`) is cached raw for a
    randomized 22–26 hours and fetched by 2c's trickle, after 3b's feed. Shown: the newest of
    each recurring kind that hasn't expired. Each is fetched once (`/1/playlist/<mbid>`): a
    generated playlist doesn't change, and the next one has a new MBID. Its tracks name the
    album only (and a release for its cover), so one more request
    (`/1/metadata/recording/?recording_mbids=…&inc=release`, the whole playlist at once) gives
    each recording's release group, which is what the wishlist saves. Both are cached until the
    playlist expires, then dropped. About three requests a day, none to MusicBrainz. Yearly
    playlists (Top Discoveries of 2025 etc.) have no expiry and aren't regenerated: left out.
    Errors and 404s as in 3b. (Response shapes checked 2026-11-02.)
  - **What shows:** per playlist, its albums in playlist order (one row per release group, not
    per release: that's what the wishlist keeps), each with the playlist's songs on it and the
    artists of those songs. The metadata has no type or release group date, so neither is
    shown. Weekly Exploration (new to you) comes first and open; the others are folded.
    Recordings without a known release group, and dismissed albums, are counted, not shown.
- 4b ✅ Similar artists on an artist's page, via LB Radio's artist endpoint
  (`/1/lb-radio/artist/<mbid>`), only on request, cached for weeks (`similar.py`).
  - **Requests:** one per artist, only when you press *Find similar artists*, with the closest
    artists only (`mode=easy`), 20 of them and one recording each (the fewest it allows: only
    the names are used). Cached raw (shared) for a randomized 28–42 days; pressing again asks
    nothing until then, after which the cached list still shows with *Ask again*. A 404 is
    cached like a response; any other error pauses ListenBrainz. None to MusicBrainz.
  - **What shows:** the similar artists, without the one asked about, each with *Follow* and
    *Dismiss* (per user, `dismissed` kind `artist`: not suggested as similar to anyone again).
    The response is keyed by MBID and not ranked (checked 2026-11-02), so they're listed by
    name, a leading "The" aside. Offered on every artist page, followed or not: it's one
    request on an explicit click, like a search.
- 4c CF recording recommendations: moved to 5h, after the library (checked 2026-11-02).
- 4d **Playlists to the wishlist** (needs 5k's recording ids for "already in your library"):
  paste a link to a public ListenBrainz playlist, pick the songs you want, and their albums go on
  the wishlist. The same path as 4a's playlists, on request instead of daily.
  - **Requests:** one for the playlist (`/1/playlist/<mbid>`) and one metadata request for its
    recordings (`/1/metadata/recording/?inc=release artist`, at most 50 at a time, as in 5h),
    only when you press *Load*. Cached raw (shared) for a randomized 1–3 days, so loading it
    again to pick more songs asks nothing. None to MusicBrainz. Errors and 404s as in 3b.
  - **Other services go through ListenBrainz.** ListenBrainz imports Spotify playlists (and, it
    says, Apple Music and SoundCloud) into your account itself, matched to MusicBrainz ids: import
    there, then paste the ListenBrainz link. Groovehound holds no Spotify credentials (see
    "Decided against").
  - **Pasted track lists** (later, optional): a box for "Artist - Title" lines copied from
    anywhere, each matched by ListenBrainz's own matcher (`/1/metadata/lookup/`, the one it uses
    for listens) rather than a MusicBrainz search per line. Unmatched lines are listed, not
    guessed. **Needs the user's token:** the docs (checked 2026-11-02) say the lookup "now
    requires an auth token" (abuse by AI scrapers), GET and POST alike, so without one the box
    isn't offered; the rest of 4d needs none. **Check first:** the POST form takes a list of
    recordings, up to `MAX_MAPPING_QUERY_LENGTH` (the docs don't give its value), so find the
    limit; a line per request is fine for a dozen songs, not for a hundred.
  - **Wishlist items stay albums.** Each song picked adds the release group it's on (the one the
    metadata names; a song is often on several, and 1g picks the edition), with the songs wanted
    kept on the item ("for: *Song A*, *Song B*") and the playlist as its source. Several songs on
    one album make one item; an album already on the wishlist gets the songs added.
  - **UI:** a dialog listing the playlist's songs in order, each with its album, *In your library*
    (by recording, so a song owned on another album counts: 5k) or *On your wishlist*, and a
    checkbox; owned songs are unticked. The wishlist can be filtered by source playlist.
- Dismissed suggestions stay dismissed (per user).

### 5. Library awareness

Know what's already in your music library, so Groovehound can say "you have this" instead of
suggesting it again, and point you at what you own but haven't played. **No MusicBrainz
requests** (except the artist page browses 5j's links open, as today, and 5k's track lists, on
request); ListenBrainz only for
listening stats (5e), your listen history (5f) and recommendations (5h). 5a and 5b need
only phase 1 and could move earlier; 5d needs phase 2; 5f works best with phases 2–3.

- 5a ✅ **Library scan.** Mount the music folder into the container read-only (e.g.
  `/music:ro`, `library.path` in config or `GROOVEHOUND_LIBRARY`) and read the MusicBrainz tags
  Picard writes (via `mutagen`: FLAC/Ogg/Opus Vorbis comments, ID3 `TXXX` in MP3/AIFF/WAV, MP4
  freeform atoms, APEv2, ASF, which all name them alike): *MusicBrainz Release Group Id*,
  *Album Id* (the release), *Album Artist Id* (else *Artist Id*), plus the album title and album
  artist name. One row per release in a per-user `library` table (release group, release,
  album artist MBIDs, title, artist, folder, file count, first found, last seen), rebuilt after
  each scan from a per-file index (`library_files`: mtime, size and tags), so rescans only
  re-read changed files (`library.py`).
  - **Files without an album id** are counted and listed by folder on a *Library* page, not
    guessed at by name; unreadable files are listed with why. A release with an album id but no
    release group id is listed too and will match wishlist editions only; resolving it would
    cost a lookup per release, so it's left alone.
  - **When:** on demand (*Rescan* on the Scan page, a full scan in the background, the page
    showing progress; `groovehound scan`) and on a trickle of its own (it runs without a
    contact, unlike 2c's): a random 1-3 hours asleep, then a walk of the folder (a `stat` per
    file) reading at most 2,000 changed files. A missing folder, or one with no audio files
    where the index had some (an unmounted share), stops the scan and changes nothing. Hidden
    files and folders are skipped; symlinked folders aren't followed.
- 5b ✅ **"In your library".** Search results, wishlist items, new releases, suggestions, the
  artist page and Discover get an *In your library* badge when their release group is in the
  library (`Store.in_library`, matching a release MBID too, for files without a release group
  id); owned ones sort after the rest of the new-release and suggestion lists, rather than hiding.
  Except (decided 2026-11-02): in the home page's *Out now*, owned ones never played (5e) come
  first, since that list is also for what to listen to next; owned and played ones stay last.
  A wishlist item that appears in the library is offered as *bought* ("Found in your library on
  2026-…", *Mark bought*), never changed for you; an item for one edition, with another in the
  library, says "Another edition" (`library.found`).
- 5c **Navidrome instead of a folder** (optional; deferred 2026-11-02 until a deployment needs
  it): the same table filled from Navidrome's Subsonic API (read-only; it exposes MusicBrainz
  ids), for when Groovehound can't see the files. With the folder mounted it adds nothing: the
  tags already give the release group, the one id Subsonic may not. Its connection is built with
  5g instead; this would only add filling the `library` table from it. **Check first** that it gives the *release group* MBID: the Subsonic API reliably has the
  release (album) and artist MBIDs, and Navidrome stores the release group's, but it may be in
  its internal API only, which can change without notice. With release MBIDs alone, matching
  would need a MusicBrainz lookup per album, which the ground rules rule out; then match
  wishlist editions only, or skip 5c.
- 5d ✅ **Suggest follows** (needs phase 2; built with 5j, `library_artists.py`): album artists
  with 3+ release groups in the library that you don't follow, ranked by how many you own (the
  top 10, at the top of 5j's tab), with *Follow* and *dismiss* (stays dismissed: `dismissed`
  kind `suggested-follow`, apart from 4b's). Later, combine with ListenBrainz listen counts
  (phase 4).
  - **Owned albums aren't hidden from new releases** (decided 2026-11-02, instead of seeding the
    "already out" list from the library on follow): the home page's list is also for what to
    listen to next, not only what to buy, so an album you own that MusicBrainz lists after you
    followed shows as new like any other, marked *In your library* (5b). Following already
    records what the browse returns as seen.
- 5e ✅ **Unplayed.** With a ListenBrainz username: your all-time listen counts per release
  group (`/1/stats/user/<name>/release-groups`, `range=all_time`; public, no token), kept in a
  per-user `listened` table (`listens.py`). Library minus played = unplayed: *never played* next
  to *In your library* wherever an album shows, and in *All albums* how many albums were
  never played, by album artist (most first, each folding out its titles).
  - **Requests:** fetched by 2c's trickle after 4a, cached raw for a randomized 22–26 hours; a
    page holds 1,000 release groups (`count=1000`), longer lists are paged with `offset`, up to
    10 pages. So one request a day for most libraries, none to MusicBrainz. A 204 (not
    calculated yet) keeps what's known and marks nothing new; a 404 is cached like a response.
    The artist stats aren't fetched: what's played per artist follows from these (5f too).
  - **Checked 2026-11-02:** the lists aren't cut to a top N. All 238 release groups of a real
    user came back in one request, down to those played once, so no export is needed. 6 of the
    238 had no release group (listens ListenBrainz couldn't link), and *Never played* says
    those, and songs linked to another album (a compilation, a single), don't count.
  - **Played also counts 5f's listen history** (decided 2026-11-02): in a real snapshot, 38
    library albums with 279 listens in the history were missing from the stats, which had
    linked their songs to other release groups (singles, compilations). The history keeps the
    album the scrobbler sent (Navidrome sends your tags' ids), so an album is played if either
    has a listen of it, as Rediscover does.
  - Matched by release group MBID only: library releases without one get no badge, and
    nothing is marked before the first stats arrive.
- 5f ✅ **Rediscover.** One page of library albums to put on next, picked at random in fixed
  proportions: unplayed ones by artists you play most, unplayed ones by artists you rarely play,
  albums you played but not lately, plus new releases (phases 2–3) by your most-played artists
  that you haven't played yet. *Shuffle* draws again (locally, no requests); *dismiss* stays
  dismissed. Fixed proportions on purpose: no tunable weights or saved mixes
  (`rediscover.py`).
  - **The proportions:** 12 albums: 4 never played by the artists you play most, 3 never played
    by the others, 3 not played in six months, 2 new releases. A section with fewer albums
    shows what it has; the others don't make up the difference. That's the home page's
    shelf (U3); its *See all* opens the same draw with up to 12 albums a section, so it holds
    what the shelf showed, and a reload or coming back keeps it (`?draw=`).
  - **Artists you play most:** an album artist's plays are the all-time listens (5e) of their
    release groups in the library; the top quarter of the artists with any (at least one),
    Various Artists and [unknown] aside. The rest, including artists never played, are "the
    others". New releases count for a followed artist among them, from the home page's feed
    (2d), in the library or not (*Add to wishlist* if not).
  - **Played** means the stats or the history have a listen of the album. "Not lately" needs the
    history's dates, so it waits for the first fill; an album the stats count but the history
    doesn't was played before its oldest listen, so it's not lately.
  - **Dismiss** has its own kind (`rediscover`): an album dismissed here still shows elsewhere.
    Nothing shows before the first stats arrive, so nothing is called unplayed on a guess.
  - **Listen history, not just stats:** 5e's all-time counts have no dates, so they can't tell
    "played two years ago" from "played yesterday". 5f keeps your listens
    (`/1/user/<name>/listens`, public, no token) in a per-user `listens` table: when, the
    recording, and the album (release group), one row per listen, keyed by ListenBrainz's own
    listen identity (`listened_at` and `recording_msid`), so a listen fetched twice counts once
    (`history.py`). The album is the one the scrobbler sent (`additional_info`; Navidrome sends
    your tags' ids, so it's the album you played, as in the library), else ListenBrainz's match
    (`mbid_mapping`). Listens with neither are counted, not matched, as in 5e. The latest
    response is kept in `api_cache` (it schedules the next request); the history is an input to
    Rediscover only: no stats page (see "Decided against").
  - **Requests:** up to 1,000 listens per request (`count=1000`). The first fill pages back with
    `max_ts`, at most 3 requests per trickle wake-up and **10 in all**: ListenBrainz's docs
    recommend the listens endpoint for "roughly ten pages or fewer" and their export for more,
    and say not to page through a whole history otherwise. Past 10,000 listens only the newest
    are kept; albums played before them still count as played through 5e's stats. (Checked
    2026-11-02: 2.3k listens since June, about 16 a day, so 10 pages is about 20 months.) After
    that, one a day with `min_ts` from 2c's trickle, a week before the newest listen kept, so
    listens a player submits late (offline) are caught. With `min_ts` the API returns the
    listens just after it (checked 2026-11-02), so a full page is followed by the next one. No
    periodic re-fill: listens deleted or backdated on ListenBrainz may stay out of step.
    None to MusicBrainz.
  - **Later, if a history outgrows 10 pages:** fill from ListenBrainz's export instead
    (`/1/export/`: needs the user's token; queue, poll, download a zip). Checked 2026-11-02 on a
    real export: its `mbid_mapping` has no `release_group_mbid` (the API's does), so the album
    would come from `additional_info` only (87% of listens for a Navidrome user), and
    `listened_at` is a float.
  - **Tracks** (after 5k's scan, its own PR): songs you played before but not lately, on albums
    in the library, matched by recording, so a song owned on a compilation counts too. Each
    links to its album (5g is per album).
  - The CF recommendations' `latest_listened_at` (5h) only covers recommended recordings, so the
    history is the source; decided 2026-11-02.
- 5g **Open in your player.** *Open in Navidrome* on library items, linking to the album in
  Navidrome's web UI. That needs Navidrome's own album id, so 5g brings the read-only Subsonic
  connection (URL, user and token or API key in config, the secret hidden by `groovehound
  config`): it lists albums with their MBIDs (your own server, no MetaBrainz requests) and maps
  each Navidrome album id to a `library` row by release MBID, which the tags already give, so
  the release-group question that holds up 5c doesn't arise. The file scan stays the library
  source. The same connection gives artist pages a photo and biography for library artists
  (`getArtistInfo2`; see `docs/ui-roadmap.md`, "Artist pages"). *Open in Symfonium* can't be a
  plain link: as of 2026-11 Symfonium has no links a web page can open (deep links are an open
  feature request on its forum, and its broadcast-intent API is for other Android apps). 5m's
  bridge app gets around that; drop the bridge if Symfonium adds links.
- 5h ✅ **Recommended albums** (was 4c; needs 5a/5b): ListenBrainz's collaborative-filtering
  recording recommendations (`/1/cf/recommendation/user/<name>/recording`, experimental), as the
  albums those recordings are on, on the *Discover* page with *Add to wishlist*, *Follow* and
  *Dismiss* (`recommended.py`). They don't depend on the generated playlists: checked
  2026-11-02, the recommendations came from a model of 2026-10-22 while the last Weekly
  Exploration was for the week of 2026-09-14. They come first on the page, open.
  - **Requests:** the recommendations, the best 100 (`count=100`; the user has 1,000), and one
    metadata request for the unheard ones (`/1/metadata/recording/?inc=release artist`), at most
    50 recordings at a time: 100 at once got a 502 once, 59 worked. Both cached raw for a
    randomized 22–26 hours, fetched by 2c's trickle after the rest of ListenBrainz's (and on
    their own: their errors don't hold the others up). The metadata asks `inc=artist release`
    (the artists' MBIDs, for *Follow*), only for recordings not fetched for the same
    `last_updated`, so none while it hasn't changed; recordings it doesn't know are remembered as
    such, not asked about again. About two requests a day, none to MusicBrainz. A 204 (no
    recommendations) is cached like a response; a 502 waits for the next day rather than
    pausing ListenBrainz, keeping what's cached (and, for the metadata, what the batches before
    it found).
  - **What shows:** each entry has `recording_mbid`, `score` and `latest_listened_at`; 41 of the
    best 100 had been played, so only those with `latest_listened_at` null count. Their albums,
    ranked by the best score of their songs, each with those songs; left out if the album is in
    the library (5b), played (5e/5f: songs of it you did play), on the wishlist or dismissed
    (shared with the home page); the best 20 are shown. Artists are **not** filtered out, followed
    or in the library: an older album you haven't heard by an artist you know is worth seeing.
    In the check, the 59 unheard songs were on 45 albums by 30 artists, all with a release
    group, and mostly well-known pop (Sabrina Carpenter, Chappell Roan, Dua Lipa, ABBA): watch
    whether that's useful once the library filter is in.
- 5i ✅ **Covers from the library** (needs 5a/5b; before 5f, whose page is all library albums):
  for a release group in the library, the cover cache (`covers.py`) takes the cover from its
  folder first (`cover.*`, `folder.*`, `front.*`, `albumart.*`, `album.*` as JPEG, PNG, WebP or
  GIF, in the edition's folder, then its files' own, e.g. a disc's), then the art embedded in
  its files (FLAC pictures, Vorbis `METADATA_BLOCK_PICTURE`, ID3 `APIC`, MP4 `covr`, APEv2
  *Cover Art (Front)*; the front cover if marked; the first two files of each edition), and
  asks the Cover Art Archive only if neither has one (`library.cover_art`). Made lazily, when
  first shown, resized to at most 250 px as a JPEG (Pillow) and cached in `covers` with the
  file it came from and that file's mtime (`source`, migration 10).
  - **Changes:** each view of a library cover checks its file (one `stat`) instead of waiting
    for a rescan: changed, it's made again; gone, the library is looked at again, then the
    archive. Art added to the library replaces a cover from the archive on its next view. A
    better file added beside one already used (a `cover.jpg` where the embedded art was taken)
    shows after the randomized 30–60 day refresh.
  - `/cover/<mbid>` also allows release groups in the library, and search results and other
    artists' pages use it for those, so they need no contact and no request. Library covers
    work without a contact configured.
- 5j ✅ **Library artists** (needs 5a/5b; `library_artists.py`): every album artist in the library, followed or not, so
  any of them can be opened to see what you're missing. *Artists* gets two tabs: *Following*
  (today's `/artists`) and *In your library* (`/artists/library`), built from the `library`
  table's album artist MBIDs, sorted by sort name, each with how many of their release groups
  you have, a *Follow* button and a link to their artist page. Files with no album artist id
  aren't listed (they're already on the Scan page).
  - **Missing releases:** the artist page already lists every album and EP with *In your
    library* badges (5b); for an artist in the library it adds a *Missing only* toggle (local,
    no requests) and a count, "You have 7 of 12".
  - **Which types count:** for a followed artist, the follow's own types; for one you don't
    follow, the default for new follows (`[follows] types`, `GROOVEHOUND_FOLLOW_TYPES`). Only
    release groups of those types count as missing (`follows.wanted`), so with the default a
    compilation you don't have isn't "missing". What you have always shows, whatever its type:
    an owned compilation or single is listed and counted in "7 of 12" even when its type isn't
    followed. If the types include singles and they haven't been browsed, opening the page
    browses them too (one more request, on that page view only, cached like a follow's).
  - **Owned but not in the browse:** a release group in the library under this album artist
    that the browse didn't return (promotional or bootleg only, a Broadcast or Other primary
    type, past the 300 cap, or credited to the artist differently) is listed anyway, under
    "Also in your library", from the `library` table's title (no lookup), linking to
    MusicBrainz. Compilations filed under Various Artists aren't the artist's, so they show
    under neither.
  - **Requests:** the list itself makes none. Opening an artist page is the browse it costs
    today (one, cached 7-14 days and shared with following), plus the singles browse above if
    the types include singles. Nothing browses library artists in
    the background: a library can have hundreds, and the trickle (2c) is for follows. So the
    list's "missing" counts appear only for artists whose browse is cached ("not checked yet"
    otherwise), never fetched to fill the column.
  - **With 5d:** the suggested follows (library artists with N+ release groups you don't
    follow) can be a section at the top of this tab rather than a page of their own.
- 5k **Tracks** (needs 5a/5b; `tracks.py`): see which songs of an album you have, mostly for
  albums you own only part of. **Backend done** (scan, partial counts, editions, cached lookup);
  the track list and badges wait for the UI overhaul (decided 2026-11-02), which gives albums a
  page of their own where the track list belongs, with track lists fetched ahead of time for the
  albums Groovehound shows (see `docs/ui-roadmap.md`, "Album pages").
  - **Scan:** also read *MusicBrainz Release Track Id* and *MusicBrainz Track Id* (the
    recording) into `library_files`; a migration, and every file read once more after it (local
    only, no requests). Matching by recording means a song you own on a compilation or a single
    counts as owned anywhere else it appears, which 4d and 5f's tracks need. ID3 keeps the
    recording in a `UFID:http://musicbrainz.org` frame, not `TXXX` (migration 13).
  - **Partial albums, no requests:** the library row's file count against the edition's track
    count from 1g's cached browse (`inc=media`) gives *8 of 12 tracks* on library items and the
    artist page. Only when that browse is cached; never fetched to fill the badge.
  - **Track list:** *Tracks* on a library item, a wishlist item or an album on the artist page
    opens the edition's track list, each track marked *In your library* (by recording) or not.
    The edition is the one in the library, else the wishlist item's chosen one, else a default
    from 1g's browse (the earliest official edition with the most common track count), named
    above the list with a link to choose another.
  - **Requests:** one release lookup (`/release/<mbid>?inc=recordings artist-credits`) per
    edition, only when *Tracks* is opened; cached (shared) for a randomized 30–60 days, refreshed
    only when opened again. Not 1g's browse with `inc=recordings` (checked 2026-11-02): the API
    docs say a release browse is cut so a page holds at most 500 tracks, which would cost
    *Choose edition* most of its editions for a big release group. A lookup returns every
    track (Picard reads box sets this way).
- 5l **Now playing** (needs 5g's Subsonic connection; planned 2026-11-04): a card at the top of
  the home page with what your player is playing right now, when something is, and the next
  steps Groovehound offers for it: *In your library* (or not), the album page, *Follow* for
  each artist, *Add to wishlist* for the album or the song. Reads only (see Non-goals): no
  controls, not a player bar (see `docs/ui-roadmap.md`, on what "streaming-like" means).
  - **Source: Navidrome's `getNowPlaying`**, over 5g's connection: your own server, so no
    MetaBrainz requests. It lists what every Navidrome user is playing, so keep the entries
    whose `username` is the configured user. The album maps to a `library` row through 5g's
    album ids; OpenSubsonic's `musicBrainzId` on the song, where Navidrome has one, gives the
    recording. **Check first** that Symfonium reports what it plays to Navidrome (a
    `scrobble` with `submission=false`), including songs from its offline cache, and what
    Navidrome returns for them.
  - **Fallback: ListenBrainz's playing now** (`/1/user/<name>/playing-now`), with only a
    ListenBrainz username configured or when Navidrome has nothing for you: one request through
    `JsonClient`, cached raw for a minute so reloads don't ask again. Check what it carries: a
    playing-now listen may have only the names the player sent, no MBIDs.
  - **Requests:** only when the home page is opened, never polled and never part of a
    trickle. The card loads after the page (HTMX `hx-trigger="load"`), so a wait behind the
    ListenBrainz limiter doesn't hold up the page; *Refresh* asks again. When the song has no
    MBIDs, nothing is looked up: the card offers a song search pre-filled with title and artist,
    as the share target does (1f), so it's one search when you press *Search*.
- 5m **Open in Symfonium, through a bridge app** (needs 5g; planned 2026-11-04). A web page
  can't send Symfonium's broadcast intents, so a small Android app of its own does it: no UI,
  one activity that takes a link from Groovehound, sends Symfonium the matching broadcast and
  closes. The PWA stays the interface; this is not a native Groovehound app (see "Decided
  against").
  - **Check first** what Symfonium's broadcast API can do: whether it can open or play a given
    album (and by which id: Navidrome's album id, which 5g already maps, or something of
    Symfonium's own), and whether it needs Symfonium to be running. If it can't do either,
    stop here and record why under "Decided against".
  - **The link:** an `intent://` URL naming the bridge's package, with
    `S.browser_fallback_url` set to 5g's *Open in Navidrome* link, so without the bridge
    installed the button still goes somewhere useful. Chrome opens these from a tap in the PWA.
    Shown only with `[symfonium] bridge = true` in config, since a page can't tell whether the
    app is installed.
  - **Scope:** just this. Anything more on the phone (widgets, reading the media session,
    catching Pixel's Now Playing notifications) is a new decision, checked against the
    Non-goals; 5l covers now playing from the server.
  - **Open:** where the code lives (an `android/` folder here, which would need
    `code-changed.sh` to skip the Python and image workflows for it, or a repo of its own), and
    how the APK is built and installed (a release asset from a workflow, sideloaded; signed with
    a key kept as a repository secret).
- 5n **ListenBrainz playlists in Navidrome** (needs 5g, 5k and 4d, whose dialog it reuses; planned 2026-11-04):
  for a "Created for you" playlist (4a) or a pasted one (4d), *Send to Navidrome* makes a
  Navidrome playlist of the songs you have, and *Add the missing to the wishlist* puts the
  albums of the rest on the wishlist. Not a playlist editor (see "Decided against"): the
  playlist is ListenBrainz's, Groovehound only copies it.
  - **Which songs you have:** by recording, from 5k's scan, so a song owned on another album
    counts. Each playlist shows *12 of 50 songs in your library* before you press anything;
    Weekly Exploration, mostly new to you, will often have few, Weekly and Daily Jams many.
  - **Navidrome's song ids:** 5g's connection already lists albums by release MBID; this adds
    the songs on each (`getAlbum` per album, your own server, no MetaBrainz requests), kept
    beside 5g's album ids and refreshed after a library scan. A recording owned more than once
    takes the copy on the album the playlist names, else the first.
  - **Writing, opt-in:** off unless `[navidrome] write_playlists = true`. `createPlaylist` with
    the songs in playlist order, named after the ListenBrainz playlist with "(Groovehound)"
    after it. Groovehound keeps the Navidrome id of each playlist it made and only ever
    replaces or deletes those, never one of yours. Pressing again replaces the songs
    (`updatePlaylist`) with a snapshot of what you have now; nothing is kept in sync on its
    own. A recurring kind (Weekly Jams) keeps one Navidrome playlist, replaced by the newest
    week's when you send it. If you deleted it in Navidrome, the next press makes a new one.
  - **Add the missing to the wishlist:** opens 4d's dialog for this playlist with the songs you
    don't have and that aren't on the wishlist ticked, so you can untick some before *Add*.
    Items as in 4d (the album, with the songs wanted and the playlist as its source). For a 4a
    playlist it uses the metadata 4a already has; no new requests.
  - **Requests:** only when you press a button; none to MetaBrainz beyond what 4a and 4d
    already make, and none in a trickle.
  - Rediscover stays a page of albums with *Open in Navidrome* (5g), not a playlist.

### 6. Notifications and polish

- 6a Gotify (like tagdrifter) when a followed artist has a new release; a weekly digest option.
- 6b Login (tagdrifter's scrypt + signed cookie, CSRF guard).
- 6c Release calendar: upcoming releases (future release dates) on their own page.
- Later: ntfy / webhooks, an RSS/Atom feed of new releases, export/import of follows and the
  wishlist (JSON / CSV), MusicBrainz collections sync (needs OAuth; would let the wishlist live
  in a MusicBrainz collection too).

### 7. A MusicBrainz mirror of your own

Optional, for anyone running musicbrainz-docker beside Groovehound (planned 2026-11-03). The
mirror is the operator's to set up and keep running; Groovehound only talks to its web service
(`/ws/2`), like any MusicBrainz server, and never to its database. Without a mirror nothing here
changes.

- 7a **Setting it up** (`docs/mirror.md`, written from a real build): what musicbrainz-docker
  needs (with indexed search its README recommends 16 threads, 16 GB of RAM and 350 GB of disk;
  4 GB and 100 GB without), the pre-built search indexes (about 60 GB to download) rather than
  a reindex, replication with a MetaBrainz access token, live indexing (marked experimental
  upstream, checked 2026-11-03), and the two settings (`api_root`, `interval`), with notes for
  running it in an LXC on TrueNAS. Also what happens
  when the mirror is down: a failed run pauses MusicBrainz for an hour, as now, and the page
  says the mirror couldn't be reached rather than musicbrainz.org.
  **No automatic fallback** to musicbrainz.org: work paced for a mirror at interval 0 would land
  on the public server, so going back to it is a settings change the operator makes.
- 7b **Search as you type, from the mirror** (needs U6's one bar, `docs/ui-roadmap.md`). As
  well as the local matches, the bar asks the mirror while you type: albums (release groups)
  and artists, the small search indexes, so they can stay in the mirror's memory; songs stay
  on Enter (the recording index is many times larger). They show as an *On MusicBrainz* group
  under the local matches, re-ranked as U6's results are.
  - **Needs the mirror's search containers** (Solr, and the indexer that keeps it current), not
    just its database and web service, which don't answer `?query=` searches. That's most of
    the mirror's cost; the Groovehound side is small.
  - **Only with a mirror you've opted in:** a separate setting (`[musicbrainz]
    search_as_you_type = true`), refused at startup unless `api_root` is set to a server that
    isn't musicbrainz.org (`musicbrainz.is_public_server`). Setting `api_root` alone isn't
    enough: it could be someone else's server, which deserves the public rules.
  - **Prefixes:** MusicBrainz search (Lucene) matches whole words, so the word being typed gets
    a `*` ("demon da*"). `*` doesn't work inside a quoted phrase, so these queries can't use
    `search_query`; each word is escaped on its own instead, so punctuation can't change the
    query. Wildcards ignored case and matched accents when tried (`ró*` found Sigur Rós).
  - **Ranking albums** (tried on a real mirror, 2026-11-05): a plain wildcard on the title ranks
    poorly, since every match scores about the same: `dark AND side AND of*` left Pink Floyd out
    of the top 5, and `nevermi*` left out Nirvana's album. Re-ranking only reorders what comes
    back, so the query has to do some of the work:
    - **Edition count stands in for popularity:** `releases` is searchable, and each result
      returns it as `count`. `(Q AND primarytype:album) OR (Q AND primarytype:album AND
      releases:[5 TO *]^50)` kept all 62 `nevermi*` matches with Nirvana's *Nevermind* (100
      editions) in the top 2; filtering on `releases` also found *The Dark Side of the Moon*
      (152) and *The Loneliest Time* (30). An album with few editions, a new one especially,
      stays in the results but ranks lower; followed artists should help there. As written this
      leaves out EPs, so try the album type as a boost rather than a filter.
    - **Each word in the title or the artist:** `(releasegroup:nirvana OR artist:nirvana) AND
      (releasegroup:nevermi* OR artist:nevermi*)` came down to 16 matches, for someone typing
      "nirvana nevermind". Tried on its own, not yet with the edition boost.
    - **Then re-rank locally:** titles and names starting with what was typed first, then by
      `count`. A name containing the word can otherwise win (`beyon*` put Above & Beyond ahead
      of Beyoncé, though artists ranked well in general). Exact-phrase boosts didn't help.
  - **Speed** (2026-11-05, 95 keystrokes replayed on the mirror's own machine, nothing else
    running, `limit=10`, plain wildcards): p95 35 ms for artists, 91 ms for albums and 195 ms for
    recordings, slowest 288 ms, on a 12 GiB LXC with a DRAM-less NVMe. Fast enough for the pause
    below. Still to measure: the boosted query, and search during replication and indexing.
  - **Requests:** after a short pause in typing (HTMX `delay:300ms`), from 3 characters, a small
    `limit` (10), through `JsonClient` as always; a newer keystroke replaces a request still
    waiting (`hx-sync="this:replace"`). Best with `interval = 0`; at 1.5 s it works but lags.
  - **Not cached:** every prefix of every query would fill `api_cache`. Enter's search is
    cached as today.
  - **Covers:** the rows show a cover only when Groovehound already has it (library, wishlist,
    the cover cache); otherwise a blank tile. Loading the archive's thumbnails for every
    keystroke's results would put the load on the Cover Art Archive instead. Enter's results
    link the archive as today.
  - The mirror's shorter `interval` could also speed up the trickles; that's a separate
    decision.

## Decided against

See "Non-goals". Each entry gives the date and the reason.

- **Creating playlists in Navidrome** from Rediscover (2026-11-02): needs write access to
  Navidrome and keeping the playlist in sync, which is a player's job. Link to the album instead
  (5g). Revisited 2026-11-04 for ListenBrainz's playlists (5n): opt-in, on a button press, a
  snapshot rather than a sync, and only the playlists Groovehound made. Rediscover stays out:
  it's albums, not songs.
- **A playlist editor** (building or editing playlists by hand, 2026-11-04): Navidrome and
  Symfonium already do that. 5n only copies a ListenBrainz playlist as it is.
- **Reading Spotify playlists directly** for 4d (2026-11-02): needs registered app credentials
  (another secret), and Spotify keeps narrowing what new apps may read. Its ISRCs would cost a
  MusicBrainz lookup per song (75 s+ for 50 songs behind the limiter), and many aren't in
  MusicBrainz. ListenBrainz imports Spotify playlists itself, matched to MusicBrainz ids: use that.
- **YouTube and YouTube Music playlists** (2026-11-02): YouTube Music has no official API (the
  usual library is unofficial, close to scraping), and YouTube video titles ("Artist - Song
  (Official Video) [4K]") would need guessing. 4d's pasted track list covers the odd case.
- **Listening statistics** (top artists, listens over time, a year in review) from 5f's listen
  history (2026-11-02): ListenBrainz's own stats pages do that, with more data and its own
  matching. The history only feeds what to play next (Rediscover).
- **A native Android app** (2026-11-04): the PWA (1e, 1f) already installs, opens in its own
  window and takes shares. A native app would be a second UI in a second language, with a JSON
  API, a build and signing to keep up, for little gain: now playing comes from the server (5l),
  and the one thing a web page can't do, sending Symfonium's broadcasts, takes a small bridge
  app (5m), not a whole one.

## Open questions

- Share code with tagdrifter as a package? Decided (2026-11-01): **not yet**. The overlap
  today is about 200 lines (rate limiter, User-Agent, backoff); a package would need its own
  repo, releases and version pins in both projects. Revisit when both are mature, or when more
  is duplicated (login/CSRF, Gotify notifications, the MusicBrainz cache). Candidates then:
  `brainz/http.py` (the limiter and `JsonClient`), tagdrifter's `web/auth.py`, `notify.py`.
- Wishlist granularity: release group (the album) by default, with an optional specific release
  (edition, format). In use, picking the edition from search results doesn't work for albums;
  1g moves the choice to the wishlist item.
- Thumbnails: link CAA directly from the browser (no server work, but every view hits CAA),
  or proxy and cache under the data dir. Started direct; decided (2026-11-02): **cache
  wishlist covers** in the database. Direct links redirect twice (to archive.org, then a
  mirror), neither redirect is cacheable, and the mirror varies, so every page load took
  1–3 s per cover; cached, a reload costs nothing. Search results stay direct.
