"""Shelves (docs/ui-roadmap.md, U3): what the home page and its *See all* pages show, as lists of
album tiles, whichever feature each album comes from. Everything is read from the database and
the cache, as those features already are, so none of this asks for anything.

A `Tile` is one album as any shelf or grid shows it: what opens it, what to call it, the reason
it's there if there is one (`badge`: "Out 14 Nov", "Not since March"), a line for grids
(`detail`), and what its ⋯ menu offers. A tile without a reason gets its badge from what the
page knows about it (`_album_tile.html`). The home page's shelves come in a fixed order, and an
empty one is left out.
"""

from __future__ import annotations

import calendar
import random
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from . import follows, new_releases, personal, playlists, recommended, rediscover
from .db import Store, WishlistItem, local_date
from .follows import Credited
from .new_releases import Feed, NewRelease
from .playlists import Playlist
from .rediscover import Draw, Pick
from .search import Hit

SHELF = 20  # most albums on a shelf; See all has the rest
WISHLIST = 10  # most recent wishlist items on the home page
SUGGESTED_ALL = 60  # "You might care about" under See all (the shelf has personal.SHOWN)


@dataclass(frozen=True)
class Tile:
    mbid: str  # release group
    title: str
    artist: str
    credits: tuple[Credited, ...] = ()  # the ⋯ menu's *Follow* buttons
    hit: Hit | None = None  # for *Add to wishlist*; None doesn't offer it
    badge: str = ""  # why it's here, if it's for a reason of its own
    detail: str = ""  # a line under it, on a grid or as a row
    dismiss: str = ""  # the URL that stops showing it, if it can be dismissed


@dataclass(frozen=True)
class Shelf:
    key: str  # unique on the page: its element id, which See all can link to
    title: str
    tiles: tuple[Tile, ...]
    see_all: str = ""  # URL
    shuffle: bool = False  # can be drawn again (Put on next)
    rows: bool = False  # as rows instead of a sideways row on a phone (the wishlist)


def out_label(date: str, today: str) -> str:
    """A release date for a badge: "Out 14 Nov", with the year if it isn't this year's, or as
    much as MusicBrainz knows: "Out Nov 2026", "Out 2027"."""
    return f"Out {short_date(date, today)}"


def short_date(date: str, today: str) -> str:
    """ "14 Nov", "14 Nov 2027", "Nov 2026" or "2027"; unchanged if it isn't an ISO date."""
    parts = date.split("-")
    if not all(p.isdigit() for p in parts) or len(parts) > 3:
        return date
    if len(parts) == 1:
        return date
    month = calendar.month_abbr[int(parts[1])] if 1 <= int(parts[1]) <= 12 else ""
    if not month:
        return date
    if len(parts) == 2:
        return f"{month} {parts[0]}"
    same_year = parts[0] == today[:4]
    return f"{int(parts[2])} {month}" + ("" if same_year else f" {parts[0]}")


def _not_since(played: str, today: str) -> str:
    """When an album was last played, for a badge: "Not since March", "Not since Sep 2025"."""
    if not played:
        return "Not played lately"
    day = local_date(played)
    month = int(day[5:7])
    if day[:4] == today[:4]:
        return f"Not since {calendar.month_name[month]}"
    return f"Not since {calendar.month_abbr[month]} {day[:4]}"


def _hit_tile(hit: Hit, *, badge: str = "", detail: str = "", artist: str = "") -> Tile:
    return Tile(
        hit.mbid,
        hit.title,
        hit.artist_credit or artist,
        tuple(follows.credited(hit.artist_mbids, hit.artist_names, hit.artist_credit)),
        hit,
        badge,
        detail,
        f"/new/{hit.mbid}/dismiss",
    )


def _release_tile(release: NewRelease, today: str, *, upcoming: bool) -> Tile:
    hit = release.hit
    return _hit_tile(
        hit,
        badge=out_label(hit.first_release_date, today) if upcoming else "",
        detail=short_date(hit.first_release_date, today) if not upcoming else "",
        artist=release.artist.name,
    )


def _suggestion_tile(hit: Hit, today: str) -> Tile:
    date = hit.first_release_date
    return _hit_tile(
        hit,
        badge=out_label(date, today) if date > today else "",
        detail=short_date(date, today) if date and date <= today else "",
    )


def _songs_tile(hit: Hit) -> Tile:
    """An album on a playlist or recommended: the songs that put it there."""
    return _hit_tile(hit, detail="“" + "”, “".join(hit.songs) + "”" if hit.songs else "")


def _pick_tile(pick: Pick, today: str, history_since: str = "") -> Tile:
    credits = (Credited(pick.artist_mbid, pick.artist or "?"),) if pick.artist_mbid else ()
    if pick.hit:
        badge = "New"
    elif not pick.listens:
        badge = "Never played"
    else:
        badge = _not_since(pick.last_played, today)
    detail = ""
    if pick.listens:
        plural = "" if pick.listens == 1 else "s"
        last = pick.last_played or history_since
        when = local_date(last) if last else ""
        since = f"last {when}" if pick.last_played else f"none since {when}" if when else ""
        detail = " · ".join(p for p in (f"{pick.listens} listen{plural}", since) if p)
    return Tile(
        pick.mbid,
        pick.title or "?",
        pick.artist or "?",
        credits,
        pick.hit,
        badge,
        detail,
        f"/rediscover/{pick.mbid}/dismiss",
    )


def _item_tile(item: WishlistItem) -> Tile:
    heard = f"Heard: {item.heard}" if item.heard else ""
    return Tile(
        item.release_group_mbid,
        item.title,
        item.artist_credit,
        tuple(follows.credited(item.artist_mbids, item.artist_names, item.artist_credit)),
        detail=heard or f"Added {local_date(item.added_at)}",
    )


def out_now(feed: Feed, today: str) -> tuple[Tile, ...]:
    return tuple(_release_tile(r, today, upcoming=False) for r in feed.new)


def coming_up(feed: Feed, today: str) -> tuple[Tile, ...]:
    return tuple(_release_tile(r, today, upcoming=True) for r in feed.upcoming)


def suggested(hits: Iterable[Hit], today: str) -> tuple[Tile, ...]:
    return tuple(_suggestion_tile(h, today) for h in hits)


def songs(hits: Iterable[Hit]) -> tuple[Tile, ...]:
    return tuple(_songs_tile(h) for h in hits)


def picks(found: Iterable[Pick], today: str, history_since: str = "") -> tuple[Tile, ...]:
    return tuple(_pick_tile(p, today, history_since) for p in found)


def rediscover_sections(drawn: Draw, today: str) -> list[tuple[str, str, tuple[Tile, ...]]]:
    """The Rediscover page's four sections: key, title and tiles."""
    return [
        (key, title, picks(found, today, drawn.history_since))
        for key, title, found in (
            ("favourites", "Never played, by artists you play most", drawn.favourites),
            ("others", "Never played, by other artists", drawn.others),
            ("not-lately", "Not played in six months", drawn.not_lately),
            ("new", "New from artists you play most", drawn.new),
        )
    ]


def playlist_key(playlist: Playlist) -> str:
    return f"playlist-{playlist.mbid[:8]}"


def put_on_next(
    store: Store, user_id: int, user_name: str, *, now: datetime | None = None, rng=None
) -> Shelf | None:
    """Rediscover's draw as one shelf, mixed; None if there's nothing to draw."""
    drawn = rediscover.draw(store, user_id, user_name, now=now, rng=rng)
    if not drawn:
        return None
    return _next_shelf(drawn, _today(now), rng or random.Random())


def _next_shelf(drawn: Draw, today: str, rng: random.Random) -> Shelf:
    mixed = rng.sample(drawn.picks, len(drawn.picks))
    return Shelf("next", "Put on next", picks(mixed, today), "/rediscover", shuffle=True)


def home(
    store: Store,
    user_id: int,
    user_name: str,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> list[Shelf]:
    """The home page's shelves, in order, leaving out the empty ones."""
    today = _today(now)
    feed = new_releases.feed(store, user_id, user_name, now=now)
    next_up = put_on_next(store, user_id, user_name, now=now, rng=rng)
    picked = recommended.recommended(store, user_id, user_name)
    found = [
        Shelf("out-now", "Out now", out_now(feed, today)[:SHELF], "/new#out-now"),
        next_up,
        Shelf("coming-up", "Coming up", coming_up(feed, today)[:SHELF], "/new#coming-up"),
        Shelf(
            "recommended",
            "Recommended for you",
            songs(picked.albums if picked else ())[:SHELF],
            "/discover#recommended",
        ),
        Shelf(
            "suggested",
            "You might care about",
            suggested(personal.suggestions(store, user_id, user_name), today),
            "/new#suggested",
        ),
        *(
            Shelf(
                playlist_key(p),
                p.title,
                songs(p.releases)[:SHELF],
                f"/discover#{playlist_key(p)}",
            )
            for p in playlists.playlists(store, user_id, user_name, now=now)
        ),
        Shelf(
            "wishlist",
            "On your wishlist",
            tuple(_item_tile(i) for i in store.wishlist(user_id, limit=WISHLIST)),
            "/wishlist",
            rows=True,
        ),
    ]
    return [s for s in found if s and s.tiles]


def new_page(
    store: Store, user_id: int, user_name: str, *, now: datetime | None = None
) -> list[Shelf]:
    """What's behind the home page's new release shelves, whole; every section, even if empty,
    so the page can say so."""
    today = _today(now)
    feed = new_releases.feed(store, user_id, user_name, now=now)
    hits = personal.suggestions(store, user_id, user_name, limit=SUGGESTED_ALL)
    return [
        Shelf("out-now", "Out now", out_now(feed, today)),
        Shelf("coming-up", "Coming up", coming_up(feed, today)),
        Shelf("suggested", "You might care about", suggested(hits, today)),
    ]


def all_tiles(shelves: Iterable[Shelf]) -> list[Tile]:
    return [t for s in shelves for t in s.tiles]


def _today(now: datetime | None) -> str:
    when = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
    return local_date(when)
