# Groovehound UI roadmap

Groovehound started as "what to buy" and has become as much "what to listen to next". The UI
grew one card per feature and still reads like the first: a top bar of seven links and lists of
text rows with 64 px covers. This plan reworks it along the lines of music streaming apps
(Spotify, YouTube Music): covers first, shelves that scroll sideways, real album and artist
pages, and a bottom tab bar on a phone, which is where it's mostly used (the installed app, 1e).

Planned 2026-11-02, after 5k's backend and 2e (emilystap/groovehound#38). The visual direction
was chosen from mockups of three: A, close to the streaming apps and dark first (see
"Decisions").

## Start here

For a session starting on this work:

1. **Read** this doc, the roadmap's Non-goals and Ground rules (`docs/roadmap.md`), and, if you
   can, the mockups' *Direction A (the design)* page (see "Direction A, as mocked up"). Where
   the doc and the mockups disagree, the doc wins.
2. **Take the next phase not marked ✅** under "Phases", starting with U0. Each phase is its own
   branch from `main` and its own squash-merged PR (or several); mark it ✅ here, and in
   `CLAUDE.md`'s status, when it merges.
3. **Check "Still to check"** at the end of this doc for anything the phase depends on, and
   settle it first.
4. **Preview** with a copy of a real database, never the original, and test as "Testing" says.

## What "streaming-like" means here

**Layout and browsing, not playback.** Groovehound doesn't play music (Non-goals), so there is
no play button, queue, now-playing bar or mini player. Where a streaming app has *Play*,
Groovehound has the next step it does offer: *Open in Navidrome* once 5g exists (*Open in
Symfonium* with 5m's bridge app), else *Add to wishlist* or *Open on MusicBrainz*. The roadmap's
5l *Now playing* card is no exception: it shows what your player is playing, with those same
next steps, and has no controls. Nothing here needs a new request to MetaBrainz beyond those
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
| **Library** | Your albums that want attention, then all of them; your artists, the scan | `/library`, `/artists`, `/artists/library` |
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
buy links come from two cached responses: 1g's releases browse (`inc=media labels url-rels`, plus `release-groups artist-credits`:
editions, buy links, track counts, and so the default edition) and 5k's track lookup of one
edition. The edition is the one in the library, else the wishlist item's chosen one, else 5k's
default from the browse.

- **Track lists from your files**: an edition in the library with a file for every track needs
  neither request. Picard writes its tags from MusicBrainz, so the scan reads them too (title,
  track artist, track and disc numbers with their totals, length, medium format, disc title)
  and keeps each library release's track count and whether every track is there
  (`library.coverage`); `tracks.local_track_list` builds the list from them. A cached lookup
  still comes first; an edition you have only part of still needs the lookup, to show what's
  missing. The same counts give *8 of 12 tracks* for any library album whose tags say how
  many, with no browse cached.
- **Tracks ahead** (a trickle like 2e's, its own thread beside `covers_ahead.py`): fetches
  those responses in the background for, in this order, the wishlist; the albums on Home's
  shelves (new and upcoming releases, recommendations, personal fresh releases, the generated
  playlists, and Rediscover's whole pool, since *Shuffle* draws from all of it); then the rest
  of the library. An album with a known edition needs only the lookup; others need the browse,
  then the lookup. A random 20–60 min asleep, then at most ~10 requests; refreshed after the
  same randomized 30–60 days; nothing without a contact or while MusicBrainz is paused.
  Estimated for a library of ~600 albums: ~800 requests of backlog (2–3 days at that pace),
  then ~30–60 a day, mostly the playlists' and recommendations' turnover. The library tier
  skips editions listed from their files, so most of its ~600 go: only partial albums, and
  files whose tags don't give the counts, are left.
- **On view**, for an album the trickle hasn't reached or never will (a search result, a
  non-followed artist's album): the browse and then the lookup, loaded after the page has shown
  (HTMX, `hx-trigger="load"`) with a placeholder track list, about 2.5–4 s behind the limiter.
  An explicit click, like opening an artist page; cached 30–60 days like the rest. A release
  group no stored data knows (a pasted URL) gets the editions browse first, which names it
  (`inc=release-groups artist-credits`) for the header.

This replaces 5k's "only when *Tracks* is opened". If the trickle's volume ever seems too much,
the first things to trim are what's left of the library tier and Daily Jams (most of the daily
turnover).

### Artist pages

Same route. A header with the name, *Follow*, *MusicBrainz* (opens the artist there) and ⋯,
with the release types under it as one line ("New releases: albums and EPs · *Change*", which
opens a sheet); *You have 7 of 12* with *Missing only* as a toggle chip; the discography as a
grid of tiles (albums and EPs, then singles and live etc. as rows that open their lists);
*Also in your library*; similar artists as a shelf of artist tiles, still behind *Find similar
artists*.

**The header's picture** (decided 2026-11-02): MusicBrainz and the Cover Art Archive have no
artist photos, and Groovehound fetches nothing from other services itself, so:

1. **A photo and a short biography from Navidrome**, for artists in the library, once 5g's
   Subsonic connection exists: `getArtistInfo2` gives `biography` and small, medium and large
   image URLs. Navidrome gets them itself (Deezer by default, no key; Last.fm with a key;
   `artist.*` files in the music folder first), so Groovehound only asks your own server. The
   photo goes through Groovehound's cover cache like a cover; the biography is shown with a
   credit ("From Navidrome (Last.fm)"). **Check first:** whether the image URLs point at
   Navidrome or straight at Deezer, how an artist is found by MusicBrainz id, and the
   biographies' licence (Last.fm's are CC BY-SA, as far as known).
2. **Otherwise a mosaic of four of their cached covers**, in a header tinted from the newest:
   every artist with albums, and the only header until 5g.
3. **A monogram** (initials on a colour from their newest cover, or a neutral one) for an
   artist with no covers at all.

Artist tiles (similar artists, the Library's artist list) follow the same order, in a circle.

**On desktop** (not mocked up; decided 2026-11-02): the desktop album page's layout, with the
picture square on the left of the header band and the name to its right, never stretched
into a wide banner (the photos Navidrome passes on are mostly square, and a wide crop cuts
them or blurs them); the discography as a grid and similar artists as a shelf.

### Library: the albums that want attention, then all of them

The Library tab leads with what Groovehound adds to the library, not an A–Z list of 600 albums:

- **Albums:** shelves (each with *See all*) of *Never played*, *Partial* (5k's *8 of 12 tracks*,
  best covered first) and *Not played lately*; the never-played-by-artist list as today's
  folding list under *See all*. Below them, *All albums* (next section).
- **Artists:** segments *Following* / *In your library*, with the suggested follows (5d) at the
  top of the second, as today.
- **Scan:** status, *Rescan* and the problem lists (untagged, no release group, unreadable),
  behind an icon in the app bar: maintenance, not browsing.

### All albums: the library browser

Left out on 2026-11-02 as close to the player's job; added on 2026-11-03 because search already
shows *In your library* and every album gets a page (U2), so the app plainly knows your albums
and having no way to see them all reads as a gap. It stays a view of the library *from
Groovehound's side*, not a second player:

- **One grid, the shelves' filters on top.** *All albums* is a grid of album tiles under the
  shelves, with chips for what only Groovehound knows: *Never played*, *Partial*, *Not played
  lately*, *On your wishlist*. Each shelf's *See all* opens this grid with its chip on, so the
  shelves and the browser are one thing, not two lists of the same albums.
- **Sorts that serve "what next?":** by artist (A–Z), by title, recently added, last played.
  No genre or year trees, no folders, no playlists.
- **Nothing to play.** Tiles open the album page; where a player would have *Play*, the album
  page has *Open in Navidrome* (5g).
- **No new data, no requests:** a sorted, filtered, paged query over the `library` table (with
  the listen history and wishlist for the chips) in a function outside `web/`, the album tile,
  and HTMX `revealed` to load the next page. Filter and sort live in the URL
  (`/library/albums?filter=partial&sort=artist`), so it works without JavaScript and can be
  bookmarked.

Ideas that turn it into a player (queues, editing, genre browsing, statistics) stay out under
the Non-goals and "Decided against".

### Wishlist

Segments *To buy* / *Bought* / *To look up* (the captures). Items as rows (cover, title,
artist, the badges, "heard at the café") with one inline action (*Bought it*) and the rest on
the album page. The quick capture box stays at the top of *To look up*, and moves to the Search
tab's top as well, since "I heard something and can't search now" starts there.

### Search: one bar (decided 2026-11-02)

One search box instead of today's album / song choice and separate title and artist boxes,
submitted with Enter (`enterkeyhint="search"`; no *Search* button in the bar). Under it, chips
for *Albums* / *Songs* / *Artists* say what MusicBrainz is asked for, and *Artist box* shows a
second box. Two kinds of results:

- **As you type, locally:** the library (albums and album artists) and the wishlist, matched by
  title and artist name in the database (no requests), so an album you already have shows
  before you'd look it up again. A short debounce, HTMX `hx-trigger="input changed delay:300ms"`.
- **As you type, from your own mirror** (added 2026-11-03; phase 7b in `docs/roadmap.md`): only
  with a MusicBrainz mirror opted in (`search_as_you_type`), never musicbrainz.org. Albums and
  artists from the mirror, under the local matches and without repeating them; covers only
  where Groovehound already has them. Without a mirror this row isn't there.
- **On Enter, MusicBrainz:** one search per press, as today, cached as today. What's sent
  depends on what was typed:
  1. **"title by artist"**, split at the last " by " as the share target does (`share.py`):
     title and artist as separate fields, which ranks best. The page says how it read it, with
     *Search the words instead*.
  2. **With the artist box open:** the two boxes as the two fields, for a title with "by" in it
     or a song.
  3. **Anything else:** the words as free text (no fields), which MusicBrainz matches against
     title and artist together.

**Checked 2026-11-02** (8 searches against musicbrainz.org; `dismax=true` made no difference):
free text finds an album from title and artist words in any order and without accents
("bite me renee rapp" → *BITE ME* by Reneé Rapp, first), but ranks loosely: "demon days
gorillaz" put a 2021 remix single first and the album third, tied with a single of the same
name. A bare title is hopeless either way ("brat": 4,190 matches, Charli xcx's not in the top
five), and **songs as free text don't work**: "feel good inc gorillaz" returned covers and
mashups whose titles contain "Gorillaz", and not the original. So: re-rank album results
locally (albums before EPs before singles, live, remix and compilation types after the rest,
within the scores MusicBrainz gives), and for *Songs* without " by " or the artist box, say
under the results that adding the artist works much better.

**On desktop** (not mocked up; decided 2026-11-02): the search bar sits at the top of every
page's content, and typing in it opens the Search page (focus and text carried over), where
the results show as you type; no drop-down of results over the current page, so there's one
results layout and one place where Enter asks MusicBrainz. The page itself is the phone's,
wider.

Search results are seen once, so they keep the archive's 250 px covers, linked directly.
*Not what you were looking for?* (save as a capture) and the capture banner stay. This
changes 1a's form, not its requests: still one search per press, never as you type (on
musicbrainz.org; a mirror of your own may, see above).

## Design language

### Tokens

All in `:root` custom properties, redefined for dark under `prefers-color-scheme`:

- **Colour:** `--bg`, `--surface`, `--surface-2` (raised: sheets, menus), `--text`, `--muted`,
  `--divider`, `--accent`, `--on-accent`, `--owned`, `--warning`, `--warning-bg`. Badge
  colours derived from these. Contrast checked to WCAG AA in both themes.
- **Type:** the system font stack (decided 2026-11-02: `system-ui, -apple-system, "Segoe UI",
  Roboto, sans-serif`, so Roboto on Android): nothing downloaded, nothing loaded from another
  site (the mockups' Google Fonts link is the mockups' only), and native on the phone. Sizes as
  under "Direction A, as mocked up", two weights.
- **Breakpoint:** one, at 768 px. Narrower: the bottom tab bar, phone layouts. 768 px and
  wider: the sidebar, desktop layouts. A tablet held upright gets the phone layout.
- **Space:** a 4 px scale (4, 8, 12, 16, 24, 32). **Radius:** 6 (badges, buttons), 10 (tiles),
  16 (sheets). **Covers:** row 56 px, tile ~150 px on a phone (two and a bit per screen width,
  so the next one peeks), 180 px wide, hero 240–300 px.
- **Motion:** short, and none with `prefers-reduced-motion`.

### Themes

Light and dark, following the system; dark comes first (direction A, decided 2026-11-02), so
the manifest's one `theme_color`, which the installed app's gesture bar takes (1f), becomes the
dark tab bar's colour, and the tab bar right above the gesture bar matches it.

### Direction A, as mocked up

The mockups (2026-11-02, made-up data) are a Claude design canvas, private to the owner:
<https://claude.ai/artifact/TyTqjUk2K4XVDPG3VC48Vi>. Its page *Direction A (the design)* is the
design; *Explored (not chosen)* keeps directions B and C and the other search layouts, as the
record of what was compared. A session that can read it should; the
values below are the ones it uses, so the design survives without it. They're a starting
point for U1's tokens, to be checked for contrast in place.

| Token | Dark | Light |
|---|---|---|
| `--bg` | `#121212` | `#ffffff` |
| `--surface` (cards, inputs) | `#1f1f1f` | `#f3f3f3` |
| `--text` | `#f4f4f4` | `#111111` |
| `--muted` | `#ababab` | `#5e5e5e` |
| `--divider` | `#2a2a2a` | `#e3e3e3` |
| `--accent` (one primary button per page, ticks) | `#ff7a59` | `#c8401f` |
| `--on-accent` | `#1a0d08` | `#ffffff` |
| `--badge-bg` / `--badge-fg` | `#2a2a2a` / `#ededed` | `#efefef` / `#222222` |
| tab bar / sidebar frame | `#0b0b0b` / `#000000` | `#ffffff` / `#e9e9e9` |

- **Type:** the system sans (the mockups show Inter, which is close), 700 for headings with
  -0.02em tracking: page titles
  30 px on a phone, 34 px on desktop; album titles 30 / 64 px; shelf headings 21 / 23 px; body
  14–15 px; badges 11–12 px, 600.
- **Shapes:** covers and tiles 6 px radius (4 px at row size), badges 4 px, buttons and the
  search bar fully rounded; tiles 150 px on a phone, 184 px in a desktop shelf.
- **Album and artist headers:** centred on a phone (cover or picture, title, artist, meta line,
  badges, buttons, all on one axis), on a band tinted from the cover (dark `#22313b`, light
  `#dfe9f0` for a blue cover: a darkened or lightened tone of the cover's main colour). On
  desktop, the cover sits left of a left-aligned title. A photo header puts the name over the
  photo's bottom edge, centred, with a fade into `--bg` behind it.
- **The tint:** worked out once, when a cover is cached, not per page view: the cover's main
  colour (Pillow, reduced to eight colours, the most common one that isn't near black or white
  and covers at least 5% of it), stored with the cover in the `covers` table (`tint.py`, called
  from `covers.py` and the migration; outside `web/`). `tint.band` turns it into the dark band
  (its hue at the mockup's lightness, 18%, saturation at most 40%, darkened further if
  `--text` on it doesn't pass WCAG AA) or the light one (91%, at most 50%, until `#111111`
  passes). No cover, or no usable colour: `--surface`. Added with U0's migration, which also
  works out the tints of the covers already cached.

### Shared components

One template partial or Jinja macro each, replacing the five album rows:

- **Album tile** (shelves, grids): cover, title, artist, at most one badge, a ⋯ button.
- **Album row** (wishlist, search, track context): cover, title, artist, a meta line, badges, at
  most one inline action and ⋯.
- **Artist tile / row:** monogram or cover mosaic, name, *Follow*.
- **Shelf:** a heading, *See all*, and one row that scrolls sideways (`overflow-x: auto` with
  `scroll-snap`), on a phone and a wide screen alike (decided 2026-11-02: Home stays a list of
  shelves, several to a screen, however long each is). On a wide screen, ‹ › buttons beside
  *See all* scroll the row by about its width (a few lines of inline script; without it the row
  still scrolls with a trackpad or Shift+wheel). *See all* opens the whole list as a grid.
- **Grid:** auto-filling tiles for *See all* pages and discographies.
- **Badge:** *In your library*, *Never played*, *8 of 12 tracks*, *On your wishlist*, *New*,
  *Out 14 Nov*. Text, not colour alone.
- **Chip / toggle:** *Follow* / *Following*, *Missing only*, filters.
- **Segmented tabs:** Following / In your library, To buy / Bought / To look up.
- **Action menu (⋯):** the actions a row no longer shows (*Dismiss*, *Follow* each artist,
  *Open on MusicBrainz*), using the `popover` attribute with `popovertarget`, so no script;
  each item is today's form, so it still posts without JavaScript.
- **Links to MusicBrainz** (decided 2026-11-02): everything shown has a MusicBrainz id, and
  MusicBrainz is where to read more or fix the data, so each album and artist page has a visible
  *MusicBrainz* link (album and artist), every ⋯ menu has *Open on MusicBrainz*, and an
  edition's line links to that release. They open in a new tab, as every external link does.
- **Sheet / dialog:** for release types, editions and notes on a phone (`popover` as well;
  `hx-confirm` stays for confirming).
- **App bar, tab bar / sidebar, empty state, notice, footer.** The footer's attribution stays
  on every page, at the end of the content above the tab bar.
- **Icons:** a few simple line icons (home, search, library, bookmark, ⋯, back, tick, plus,
  open externally, chevrons), drawn as inline SVG from one macro file (`_icons.html`,
  `icon("home")`), using `currentColor`, `aria-hidden` beside a text label or with an
  `aria-label` when alone. No icon font, nothing from another site.

### Navigation

Plain page loads between pages: no HTMX `hx-boost` (which swaps pages in place) in U1, so the
back button, scroll position and the installed app behave as browsers do. HTMX stays for the
parts of a page that change (follow, dismiss, the lazily loaded sections). Try `hx-boost` later
only if switching tabs feels slow.

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

- **U0 ✅ Covers at 500 px, as files** (backend only; above), with each cover's tint. Sharper
  covers, nothing else changes. Files under `covers/` beside the database (`Store.covers_dir`,
  `covers/ab/ab12….jpg`); migrations 14 and 15 add `file`, `size` and `tint` and write the
  blobs out (then `VACUUM`). The band itself (`tint.band`) waits for U2's header.
- **U1 ✅ Tokens and the shell.** The new tokens and type, the bottom tab bar / sidebar with the
  four destinations, the app bar, the footer's new place. Pages keep their current content under
  the new shell; Discover, Rediscover and Artists get links from Home and Library until U3 and
  U5 place them (as app bar buttons). Of the shared components, those the shell uses arrive
  here (icons, app bar, tab bar / sidebar, footer, and today's cards, buttons, chips and tabs
  on the new tokens); the album tile, row, shelf, grid, ⋯ menu and sheet come with their
  first users in U2 and U3. On desktop the footer sits at the bottom of the sidebar, as
  mocked up; on a phone it ends the content. The desktop search bar waits for U6. Decided in
  place: buttons are quiet pills on `--button-bg` (a step past `--surface`, as they sit on
  cards), `.primary` takes the accent, and light `--owned` is `#237a36` (AA on `--surface`).
- **U2 The album page, with 5k's tracks.** `/albums/<mbid>`, the track list, *8 of 12 tracks*
  badges, and every album title linking to it. The wishlist item keeps its sections for now;
  the album page shows them too. Tracks ahead (above) is its own backend PR, before or after
  the page: without it, every album loads on view.
  - ✅ **The page** (`album.py`): the header from what's stored, the tinted band, *Tracks*
    (loaded after the page shows when not cached; `?edition=` shows another edition's), *Your
    copy*, the wishlist item (its forms post plainly and come back), *Editions* and *Where to
    buy*; album titles everywhere link here, and library rows say *8 of 12 tracks*. Decided in
    place: the editions browse also asks for `release-groups artist-credits`, so an album
    nothing stored knows gets its header from the browse the track list needs anyway (one
    request, not a release group lookup and then the browse; checked against musicbrainz.org
    2026-11-03). The ⋯ menu waits for U3's tile, its first other user.
  - ✅ **Track lists from your files** (above): decided after the page landed, to take most of
    the library tier out of tracks ahead before it starts. Migration 16 reads every file once
    more (local only).
  - **Tracks ahead**: the trickle. Held on 2026-11-04 until the MusicBrainz mirror is running:
    on-view loading from a mirror may be fast enough to shrink it or drop it, and its ~800
    request backlog shouldn't go to musicbrainz.org a day or two before that's known.
- **U3 ✅ Home as shelves.** Home absorbs Discover and Rediscover; `/discover` and `/rediscover`
  become *See all* grids. `shelves.py` turns every source into `Tile`s and `Shelf`s; one
  partial (`_album_tile.html`) replaces the four album rows of those pages, with `_shelf.html`,
  `_grid.html` and the ⋯ menu (a sheet on a phone, a small dialog from 768 px; the popover isn't
  anchored to its button, so it names the album). Wishlist items and search results keep their
  rows until U5 and U6. Decided in place: **`/new`** is the *See all* for *Out now*, *Coming up*
  and *You might care about*, which had no page of their own; shelves show at most 20.
  *On your wishlist* is rows on a phone and a shelf from 768 px, as mocked up, by CSS on the same
  tile (`.rows`). *Quick capture* takes the place of the *To look up* chip with nothing waiting,
  and the box itself lives on the wishlist page, so it stays one tap from Home until U6 puts it
  on Search; a shared text that isn't "song by artist" still opens Home with the form filled
  in. *Shuffle* swaps its shelf with HTMX (`/shelves/next`; a plain request goes back to
  `/#next`). Grids show what a shelf leaves out: the songs on a recommended album, listens and
  last played. A tile's artists are in its ⋯ menu, each with *Follow*. The album page's own ⋯
  menu has nothing to hold yet.
- **U4 Artist pages.** Header, discography grid, *Missing only* chip, similar artists shelf.
- **U5 Library and Wishlist.** The Library tab's shelves, *All albums*, artists and scan; the
  wishlist's segments and plain rows, with the item's sections now only on the album page. The
  Library tab has no mockup yet: mock it up (phone and desktop, on the Direction A page) when U5
  starts, from the tile, shelf and grid as built by then.
- **U6 Search** as one bar: local matches as you type (a query over the library and wishlist,
  outside `web/`), the three ways of reading what's typed, the local re-ranking, and the
  capture box at its top. Then a last pass for anything still on the old CSS.
- **Later, only with a mirror of your own: MusicBrainz as you type** (phase 7b in
  `docs/roadmap.md`, which has the details): an *On MusicBrainz* group under U6's local matches,
  on the same bar and debounce, re-ranked as U6's results are. Without a mirror, nothing
  changes.

5g (*Open in Navidrome*) can land any time after U2 and fills the album page's first action;
the artist photos and biographies (above) come with it or just after, on the same connection.

### Testing

- `tests/test_app.py` checks the pages' HTML: each phase updates it for what it changes, and
  keeps checking what the old tests promised (the attribution footer, `target="_blank"` on
  external links, root-relative URLs).
- **Every action still works without JavaScript:** each form posts normally and comes back
  (`next`). A test per new form or menu item posts it without HTMX headers and follows the
  redirect.
- New logic outside `web/` (the album page's header, tracks ahead, the tint, local search
  matching, re-ranking) gets its own tests, with recorded responses only; never the network.
- **Previews:** run the app on a copy of a real database (never the original), with no contact
  set so nothing reaches MetaBrainz, and check each page at phone width and at desktop width, in
  dark and light.

## Decisions

Decided 2026-11-02:

1. Four destinations (Home, Search, Library, Wishlist); Artists lives under Library.
2. Home absorbs Discover and Rediscover as shelves; their pages stay as *See all*.
3. Album pages at `/albums/<mbid>`, and every album links there instead of to MusicBrainz.
4. Track lists are fetched ahead of time for the wishlist, Home's albums and the library (a
   trickle); anything else loads on view (the editions browse, then the track lookup).
5. 5k's track list goes on the album page; *8 of 12 tracks* on library albums' tiles and rows
   (Library's *Partial* shelf, the artist page, the album page), from the cached responses or
   the files' tags. An edition you have whole is listed from its files, with no request.
6. Covers at 500 px, stored as files under the data folder.
7. No full library browser for now: the Library tab shows what wants attention. Easy to add
   later (see Library). *Changed 2026-11-03:* *All albums* added (see All albums), as a grid
   with the shelves' filters, in U5.
8. The look: direction A, "streaming, dark first" (near-black, big covers, one bright accent,
   the album page's header tinted from its cover), with a light theme of the same design.
   Chosen from mockups of three directions.
9. Search is one bar: local matches as you type, MusicBrainz on Enter, as "title by artist",
   with the artist box, or as free text (see Search).
10. Shelves are one sideways row on every screen size, with ‹ › buttons on wide screens.
11. Artist headers: a photo and biography from Navidrome (5g) for library artists, else a
    mosaic of their covers, else a monogram (see Artist pages).
12. A visible *MusicBrainz* link on every album and artist page, and in every ⋯ menu.
13. System fonts, one breakpoint at 768 px, inline SVG icons from one macro file, plain page
    loads (no `hx-boost`), and the header tint computed when a cover is cached (see Design
    language).

## Still to check

Gathered from the "Check first" notes above; settle each before the phase that needs it.

- ✅ **Before U0:** that a cover's main colour gives a pleasant band for typical covers.
  Checked 2026-11-02 on the 135 covers in a copy of a real database: 133 got a tint (two
  near-monochrome ones didn't), and the bands came out muted and close to the mockup's (greys
  for black-and-white covers, browns for skin tones, a dark teal for a pale blue one).
- **Before tracks ahead (with U2):** the trickle's real volume against the estimate (~800
  requests of backlog, then ~30–60 a day), from its first days' logs.
- **Before U6:** how the local re-ranking treats real album searches (the "demon days gorillaz"
  case: the album should come first), and whether free-text song searches need more than the
  hint to add the artist.
- **Before the artist photos (with 5g):** whether `getArtistInfo2`'s image URLs point at
  Navidrome or straight at Deezer; how an artist is found by MusicBrainz id; the biographies'
  licence (Last.fm's are CC BY-SA, as far as known) and the credit to show.
- **With U2:** that 5k's default edition (the earliest official one with the most common track
  count), used for albums not in the library or on the wishlist, gives sensible track lists in
  practice.
