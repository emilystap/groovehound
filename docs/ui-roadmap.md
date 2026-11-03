# Groovehound UI roadmap

Groovehound started as "what to buy" and has become as much "what to listen to next". The UI
grew one card per feature and still reads like the first: a top bar of seven links and lists of
text rows with 64 px covers. This plan reworks it along the lines of music streaming apps
(Spotify, YouTube Music): covers first, shelves that scroll sideways, real album and artist
pages, and a bottom tab bar on a phone, which is where it's mostly used (the installed app, 1e).

Planned 2026-11-02, after 5k's backend and 2e (emilystap/groovehound#38). The visual direction
(colours, type, how close to Spotify it looks) is chosen from mockups, after this plan.

## What "streaming-like" means here

**Layout and browsing, not playback.** Groovehound doesn't play music (Non-goals), so there is
no play button, queue, now-playing bar or mini player. Where a streaming app has *Play*,
Groovehound has the next step it does offer: *Open in Navidrome* once 5g exists, else *Add to
wishlist* or *Open on MusicBrainz*. Nothing here needs a new request to MetaBrainz beyond those
named under "Album page" below, and none of it shows listening statistics (see "Decided
against" in the roadmap): listen counts appear only as a reason an album is suggested ("not
played since March").

The constraints from `CLAUDE.md` stay: logic outside `web/` (routes parse, call, pick a
template), root-relative URLs, the attribution footer on every page, external links in a new
tab, the manifest, no JS build (vendored HTMX, plus at most a few lines of inline script), and
every form working without JavaScript (the `next` field) as it does today.

## Inventory: what there is today

### Pages

| Route | Template | Shows | Actions |
|---|---|---|---|
| `/` | `home.html` | Contact notice; quick capture with what's waiting to be looked up; new releases (*Coming up*, *Out now*); *You might care about* (3b); the latest wishlist items | Save a capture, *Look it up*, remove a capture; per release: *Add to wishlist*, *Dismiss*, *Follow* (3b) |
| `/search` (and `/share`) | `search.html` | Share or capture banner; album / song form; results with cover, type, date, songs matched, *In your library* | *Search*; per result: *Follow* each artist, *Add to wishlist* (with an edition select for songs); *Not what you were looking for?* saves the query as a capture |
| `/wishlist` (`?show=bought`) | `wishlist.html` | Quick capture; tabs *Still to buy* / *Bought*; items | Per item: *Follow*, *Choose edition*, *Where to buy*, notes and "where I heard it", *Mark bought* (when found in the library), *Bought it* / *Still want it*, *Remove* |
| `/artists` | `artists.html` | Tabs *Following* / *In your library*; followed artists with since when | *Following* (unfollow), the follow's release types |
| `/artists/library` | `artists_library.html` | Suggested follows (5d); every library album artist, "you have 7 of 12" | *Follow*, *Dismiss* a suggestion |
| `/artists/<mbid>` | `artist.html` | Name, type, disambiguation; albums and EPs (have / missing); singles, live etc. and *Also in your library*, folded; similar artists | *Follow*, release types, *Open on MusicBrainz*, *Missing only*; per album *Add to wishlist*; *Find similar artists* / *Ask again*, *Follow* / *Dismiss* each |
| `/discover` | `discover.html` | *Recommended for you* (5h); one folded card per generated playlist (4a), each album with its songs | Per album: *Follow*, *Add to wishlist*, *Dismiss* |
| `/rediscover` | `rediscover.html` | Four sections of drawn library albums (5f), with listens and last played | *Shuffle*; per album *Dismiss*, *Add to wishlist* (new releases) |
| `/library` | `library.html` | Folder, counts, scan progress (polls); never played, by artist; listen history line; untagged folders, releases without a release group, unreadable files | *Rescan* |
| `/cover/<mbid>`, `/healthz` | | A cached cover; health | |

### Partials

| Partial | Used by | What it is |
|---|---|---|
| `_cover.html` | everywhere | 64 px cover (48 px on a phone), from `/cover` or the archive directly |
| `_new_release.html`, `_suggestion.html`, `_discover_release.html`, `_rediscover_pick.html`, `_wishlist_item.html` | home, discover, rediscover, wishlist | Five versions of one thing: an album row (cover, title linking to MusicBrainz, artist, type and date, badges, actions) |
| `_owned.html` | all album rows | *✓ In your library · never played* |
| `_wishlist_button.html` | all album rows | *Add to wishlist*, or *✓ On your wishlist* |
| `_follow_button.html`, `_follow_row.html` | rows, artist pages | An artist's name with *Follow* / *✓ Following*, swapped everywhere at once (out of band) |
| `_follow_types.html` | artist pages | The follow's release types, folded |
| `_editions.html`, `_buy_links.html` | wishlist item | *Choose edition* and *Where to buy*, loaded when opened |
| `_captures.html`, `_capture_search.html` | home, wishlist, search | Quick capture |
| `_similar.html` | artist page | Similar artists |
| `_library_status.html` | library | Scan status, polling while scanning |
| `_artist_tabs.html`, `_wishlist_tabs.html` | artists, wishlist | Two-way tabs with counts |

Things this shows: every album is a row, never a tile; every album title leaves the app for
MusicBrainz; the same album row exists five times with small differences; and the wishlist item
carries three folded sections and two buttons, because there's no album page to put them on.

## Information architecture

### Four destinations

A phone's bottom bar has room for four or five; Groovehound has seven links. Proposed:

| Tab | What | Today's pages |
|---|---|---|
| **Home** | Shelves of what to listen to or get next | `/`, `/discover`, `/rediscover` (as *See all*) |
| **Search** | Search MusicBrainz, quick capture | `/search` |
| **Library** | Your albums that want attention, your artists, the scan | `/library`, `/artists`, `/artists/library` |
| **Wishlist** | What you want, what you've bought, what's waiting to be looked up | `/wishlist` |

On a phone: a bottom tab bar (icons with labels, ≥ 48 px tall, above the gesture bar with
`env(safe-area-inset-bottom)`). On a wide screen: the same four in a left sidebar. Each page has
a slim app bar with its title and one or two actions (*Shuffle*, *Rescan*, a filter). URLs stay
as they are, so bookmarks and the share target keep working; the tab bar marks the destination
a page belongs to (an artist page belongs to Library, an album page to whichever tab you came
from, defaulting to Home).

### Home absorbs Discover and Rediscover

Every source is shelf-sized (recommended: 20 albums, personal fresh releases: 10, a playlist: a
dozen, Rediscover's draw: 12), so Home becomes a stack of shelves, each with *See all* opening
today's page as a grid. Proposed order, empty shelves left out:

1. **Out now** from artists you follow (2d/3a), owned and never played first, as today.
2. **Put on next**: Rediscover's draw as one shelf, each tile saying why ("Never played",
   "Not since March", "New"), with *Shuffle*. *See all* is `/rediscover` with its four sections.
3. **Coming up** (2d), soonest first, each with its date.
4. **Recommended for you** (5h).
5. **You might care about** (3b).
6. One shelf per generated playlist (4a), Weekly Exploration first.
7. **On your wishlist**, newest first.

A *To look up (2)* chip under the app bar when captures are waiting, linking to the wishlist's
list of them. The contact notice stays at the top until a contact is set. The order is for the
mockups to try out; all of it is read locally, as the home page already is, so Home makes no
requests.

### Album pages: `/albums/<release-group-mbid>`

Every album tile and row in the app links here instead of to MusicBrainz (which moves into the
page as *Open on MusicBrainz*). This is where 5k's track list goes, and where the wishlist
item's folded sections move to, so wishlist rows can be as plain as any other.

- **Header:** a big cover, title, artists (each linking to their page, with *Follow*), type and
  date, and the badges that apply (*In your library*, *Never played*, *8 of 12 tracks*, *On your
  wishlist*, *Out 14 Nov*).
- **Actions:** *Open in Navidrome* (5g, once it exists) first; *Add to wishlist* / *On your
  wishlist*; *Open on MusicBrainz*; the rest in a ⋯ menu.
- **Tracks** (5k): the edition's track list, each track marked owned (by recording) or not, the
  edition named above it ("CD, 2019, 12 tracks", with *Change*) as 5k describes.
- **Your copy** (in the library): which edition, when it was found, listens and last played,
  *8 of 12 tracks*.
- **On your wishlist** (when it is): where you heard it, notes (editable), the chosen edition,
  *Mark bought* / *Bought it*, *Remove*.
- **Where to buy** and **Editions** (1c, 1g), as today, loaded when opened.

**Requests** (decided 2026-11-02): **albums Groovehound shows you are prepared ahead of time;
albums you look up yourself load on view.** The header is built from what's already stored:
whichever of the wishlist, library, cached browses, feeds, playlists and recommendations knows
the release group (every link to the page comes from one of them). The track list, editions and
buy links come from two cached responses: 1g's releases browse (`inc=media labels url-rels`:
editions, buy links, track counts, and so the default edition) and 5k's track lookup of one
edition. The edition is the one in the library, else the wishlist item's chosen one, else 5k's
default from the browse.

- **Tracks ahead** (a trickle like 2e's, its own thread beside `covers_ahead.py`): fetches
  those responses in the background for, in this order, the wishlist; the albums on Home's
  shelves (new and upcoming releases, recommendations, personal fresh releases, the generated
  playlists, and Rediscover's whole pool, since *Shuffle* draws from all of it); then the rest
  of the library. An album with a known edition needs only the lookup; others need the browse,
  then the lookup. A random 20–60 min asleep, then at most ~10 requests; refreshed after the
  same randomized 30–60 days; nothing without a contact or while MusicBrainz is paused.
  Estimated for a library of ~600 albums: ~800 requests of backlog (2–3 days at that pace),
  then ~30–60 a day, mostly the playlists' and recommendations' turnover. A side effect: exact
  *8 of 12 tracks* for every library album, which today shows only when a browse happens to be
  cached.
- **On view**, for an album the trickle hasn't reached or never will (a search result, a
  non-followed artist's album): the browse and then the lookup, loaded after the page has shown
  (HTMX, `hx-trigger="load"`) with a placeholder track list, about 2.5–4 s behind the limiter.
  An explicit click, like opening an artist page; cached 30–60 days like the rest. A release
  group no stored data knows (a pasted URL) gets the release group lookup for its header first.

This replaces 5k's "only when *Tracks* is opened". If the trickle's volume ever seems too much,
the first things to trim are the library tier (~600 of the backlog) and Daily Jams (most of the
daily turnover).

### Artist pages

Same route. A header with the name, *Follow* and the release types (in the ⋯ menu or a sheet,
not inline); *You have 7 of 12* with *Missing only* as a toggle chip; the discography as a grid
of tiles (albums and EPs, then singles and live etc. as their own shelves, folded only when
long); *Also in your library*; similar artists as a shelf of artist tiles, still behind *Find
similar artists*. MusicBrainz and the Cover Art Archive have no artist photos, and Groovehound
fetches nothing from elsewhere, so an artist tile is a monogram or a mosaic of their cached
covers (no requests).

### Library: the albums that want attention, not a library browser

Browsing your whole library A–Z is the player's job (Navidrome, Symfonium), so the Library tab
doesn't list all 600 albums. It's what Groovehound adds to the library:

- **Albums:** shelves (each with *See all*) of *Never played*, *Partial* (5k's *8 of 12 tracks*,
  best covered first) and *Not played lately*; the never-played-by-artist list as today's
  folding list under *See all*.
- **Artists:** segments *Following* / *In your library*, with the suggested follows (5d) at the
  top of the second, as today.
- **Scan:** status, *Rescan* and the problem lists (untagged, no release group, unreadable),
  behind an icon in the app bar: maintenance, not browsing.

**Left out for now, easy to add later** (decided 2026-11-02): an *All albums* grid (A–Z, by
artist, recently added) would need no new data and no requests: a sorted, paged query over the
`library` table in `library.py`, the album tile, and HTMX `revealed` to load the next page, as
a *See all* under Library. Add it if browsing the library here turns out to matter, with the
reason, since it's close to the player's job.

### Wishlist

Segments *To buy* / *Bought* / *To look up* (the captures). Items as rows (cover, title,
artist, the badges, "heard at the café") with one inline action (*Bought it*) and the rest on
the album page. The quick capture box stays at the top of *To look up*, and moves to the Search
tab's top as well, since "I heard something and can't search now" starts there.

### Search

The form as today, results as rows (search results are seen once, so they keep the archive's
250 px covers, linked directly). *Not what you were looking for?* and the capture banner stay.

## Design language

### Tokens

All in `:root` custom properties, redefined for dark under `prefers-color-scheme`:

- **Colour:** `--bg`, `--surface`, `--surface-2` (raised: sheets, menus), `--text`, `--muted`,
  `--divider`, `--accent`, `--on-accent`, `--owned`, `--warning`, `--warning-bg`. Badge
  colours derived from these. Contrast checked to WCAG AA in both themes.
- **Type:** the system font stack, a five-step scale (12, 14, 16, 20, 28 px), two weights.
- **Space:** a 4 px scale (4, 8, 12, 16, 24, 32). **Radius:** 6 (badges, buttons), 10 (tiles),
  16 (sheets). **Covers:** row 56 px, tile ~150 px on a phone (two and a bit per screen width,
  so the next one peeks), 180 px wide, hero 240–300 px.
- **Motion:** short, and none with `prefers-reduced-motion`.

### Themes

Light and dark, following the system. Whether dark comes first (as in most streaming apps) is
for the mockups. One snag to remember: the manifest has one `theme_color`, which the installed
app's gesture bar takes (1f), and the tab bar now sits right above it, so their colours should
agree in the theme that comes first.

### Shared components

One template partial or Jinja macro each, replacing the five album rows:

- **Album tile** (shelves, grids): cover, title, artist, at most one badge, a ⋯ button.
- **Album row** (wishlist, search, track context): cover, title, artist, a meta line, badges, at
  most one inline action and ⋯.
- **Artist tile / row:** monogram or cover mosaic, name, *Follow*.
- **Shelf:** a heading, *See all*, and a row that scrolls sideways (`overflow-x: auto` with
  `scroll-snap`, no script); on a wide screen it wraps into a grid instead.
- **Grid:** auto-filling tiles for *See all* pages and discographies.
- **Badge:** *In your library*, *Never played*, *8 of 12 tracks*, *On your wishlist*, *New*,
  *Out 14 Nov*. Text, not colour alone.
- **Chip / toggle:** *Follow* / *Following*, *Missing only*, filters.
- **Segmented tabs:** Following / In your library, To buy / Bought / To look up.
- **Action menu (⋯):** the actions a row no longer shows (*Dismiss*, *Follow* each artist,
  *Open on MusicBrainz*), using the `popover` attribute with `popovertarget`, so no script;
  each item is today's form, so it still posts without JavaScript.
- **Sheet / dialog:** for release types, editions and notes on a phone (`popover` as well;
  `hx-confirm` stays for confirming).
- **App bar, tab bar / sidebar, empty state, notice, footer.** The footer's attribution stays
  on every page, at the end of the content above the tab bar.

## Covers at 500 px, as files

A tile of ~150 CSS px on a phone with 2.5–3× pixel density shows ~400 device pixels, so 250 px
covers look soft; the album page's hero needs more. Decided here, as 2e's "Size" note asked:

- **500 px** from the Cover Art Archive (`front-500`, ~100–150 KB), and covers from the library
  resized to at most 500 px. One size everywhere; rows show it scaled down (they're on the home
  network, and a second size can come later if it matters). Search results keep linking the
  archive's `front-250` directly.
- **As files** under the data folder (`covers/ab/ab12….jpg`, by release group), with the
  `covers` table keeping the rest (type, fetched, refresh after, source, plus the size made at).
  ~3,000 covers is ~350–450 MB, past the "few hundred MB" where 2e said to move them out of
  SQLite. A migration writes the existing blobs out (local, no requests).
- **Replacing the 250 px ones:** library covers are made again at 500 px when next shown (local,
  no request). Archive covers are replaced by the 30–60 day refresh as it comes round, which 2e's
  trickle already spreads out; no catch-up burst.

## Phases

Each is one or more PRs, and `main` works and looks consistent after each: new components come
in alongside the old CSS, and each PR deletes what it leaves unused. Routes don't change, and
the logic for each page stays where it is (`new_releases.py`, `rediscover.py`, …); a page that
needs something new (the album page's header) gets a function outside `web/`.

- **U0 Covers at 500 px, as files** (backend only; above). Sharper covers, nothing else changes.
- **U1 Tokens and the shell.** The new tokens and type, the bottom tab bar / sidebar with the
  four destinations, the app bar, the footer's new place. Pages keep their current content under
  the new shell; Discover, Rediscover and Artists get links from Home and Library until U3 and
  U5 place them. The shared components arrive here, used first by the shell.
- **U2 The album page, with 5k's tracks.** `/albums/<mbid>`, the track list, *8 of 12 tracks*
  badges, and every album title linking to it. The wishlist item keeps its sections for now;
  the album page shows them too. Tracks ahead (above) is its own backend PR, before or after
  the page: without it, every album loads on view.
- **U3 Home as shelves.** Home absorbs Discover and Rediscover; `/discover` and `/rediscover`
  become *See all* grids. The five album rows become the tile and row components.
- **U4 Artist pages.** Header, discography grid, *Missing only* chip, similar artists shelf.
- **U5 Library and Wishlist.** The Library tab's shelves, artists and scan; the wishlist's
  segments and plain rows, with the item's sections now only on the album page.
- **U6 Search** in the new components, and the capture box at its top. Then a last pass for
  anything still on the old CSS.

5g (*Open in Navidrome*) can land any time after U2 and fills the album page's first action.

## Decisions

Decided 2026-11-02:

1. Four destinations (Home, Search, Library, Wishlist); Artists lives under Library.
2. Home absorbs Discover and Rediscover as shelves; their pages stay as *See all*.
3. Album pages at `/albums/<mbid>`, and every album links there instead of to MusicBrainz.
4. Track lists are fetched ahead of time for the wishlist, Home's albums and the library (a
   trickle); anything else loads on view (the editions browse, then the track lookup).
5. 5k's track list goes on the album page; *8 of 12 tracks* on library albums' tiles and rows
   (Library's *Partial* shelf, the artist page, the album page), from the cached responses.
6. Covers at 500 px, stored as files under the data folder.
7. No full library browser for now: the Library tab shows what wants attention. Easy to add
   later (see Library).

Still open: the look (colours, type, dark first or not), chosen from the mockups.
