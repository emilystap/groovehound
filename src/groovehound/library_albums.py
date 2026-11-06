"""The library's albums (docs/ui-roadmap.md, "Library" and "All albums"): one album per release
group, with what only Groovehound knows about it, so the Library tab's shelves and the *All
albums* grid are one list seen through a filter. It's a view from Groovehound's side, not a second
player: the filters and sorts are the ones that serve "what next?". Rediscover draws from the same
albums (`albums`).

**No requests**: everything comes from tables and the cache.

- **Never played** waits for the first stats, so nothing is called unplayed on a guess.
- **Not played lately** also waits for the listen history's first fill, which has the dates. An
  album the stats count but the history doesn't was played before its oldest listen, so it's
  not lately (as in Rediscover).
- **Partial**: 5k's *8 of 12 tracks*, from the cached browses and the files' tags.
- **Mixed** spreads each artist's albums through the list, so a long discography, or a scan that
  found everything at once, doesn't put one artist's albums side by side. It's a fixed order for
  the day, so the pages of the grid join up and a shelf's *See all* starts where the shelf did.
- A filter that can't be told yet, or asks for something unknown, is no filter.
"""

from __future__ import annotations

import random
from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from . import history, listens, tracks
from .db import Store
from .follows import NOT_FOLLOWABLE
from .library_artists import sort_name
from .tracks import Partial

LATELY = timedelta(days=182)  # played within this: not "not played lately"
PAGE = 48  # albums a page of the grid: divides by 2, 3, 4 and 6 columns, so rows end full

NEVER_PLAYED = "never-played"
PARTIAL = "partial"
NOT_LATELY = "not-lately"
WISHLIST = "wishlist"
FILTERS = {  # key: label, in the order the chips show them
    NEVER_PLAYED: "Never played",
    PARTIAL: "Partial",
    NOT_LATELY: "Not played lately",
    WISHLIST: "On your wishlist",
}

MIXED = "mixed"
ARTIST = "artist"
TITLE = "title"
ADDED = "added"
PLAYED = "played"
COVERED = "covered"  # only with PARTIAL
SORTS = {
    MIXED: "Mixed",
    ARTIST: "By artist",
    TITLE: "By title",
    ADDED: "Recently added",
    PLAYED: "Longest since played",
}
COVERED_LABEL = "Best covered"
DEFAULT_SORTS = {NEVER_PLAYED: MIXED, PARTIAL: COVERED, NOT_LATELY: PLAYED}  # else ARTIST


@dataclass(frozen=True)
class Album:
    """An album in the library: one release group, named as its first edition is."""

    mbid: str  # release group
    title: str
    artist: str
    artist_mbid: str  # to link to their page ("" if none or not an artist)
    artist_key: tuple[str, ...]  # who it's by, for grouping by album artist
    added_at: str = ""  # when its first edition was found
    listens: int = 0  # all-time, from the stats (or the history, if it knows more)
    last_played: str = ""  # the latest listen in the history, UTC ISO ("" if none kept)


@dataclass(frozen=True)
class Library:
    """Every album in a user's library, and which of them each filter holds."""

    albums: tuple[Album, ...]  # by artist, then title
    never_played: frozenset[str]
    partial: dict[str, Partial]
    not_lately: frozenset[str]
    wishlist: frozenset[str]
    stats_ready: bool = False  # the first stats are here, so Never played means something
    history_ready: bool = False  # the history's first fill is done, so "lately" is known
    seed: str = ""  # the order Mixed is in: the day's

    def holds(self, key: str) -> Collection[str]:
        return {
            NEVER_PLAYED: self.never_played,
            PARTIAL: self.partial,
            NOT_LATELY: self.not_lately,
            WISHLIST: self.wishlist,
        }[key]


@dataclass(frozen=True)
class Choice:
    """A filter chip or a sort, as a page offers it."""

    key: str
    label: str
    count: int = 0


@dataclass(frozen=True)
class Link:
    """A filter chip or a sort as a link to the page it opens: every choice is in the URL."""

    label: str
    url: str
    on: bool = False


@dataclass(frozen=True)
class Page:
    """One page of the *All albums* grid."""

    albums: tuple[Album, ...]
    total: int  # albums in the filter
    of: int  # albums in the library
    filter_key: str
    sort: Link  # the one in use
    chips: tuple[Link, ...]
    sorts: tuple[Link, ...]
    url: str  # this page: the grid, with no page number
    next_url: str = ""  # the next page, if there is one


def albums(store: Store, user_id: int) -> list[Album]:
    """The user's albums, by artist then title, with their listens. Editions of one album are
    one; releases whose files name no release group aren't albums here, as elsewhere."""
    found: dict[str, Album] = {}
    for release in store.library(user_id):
        mbid = release.release_group_mbid
        if not mbid:
            continue
        known = found.get(mbid)
        if known is None:
            first = release.album_artist_mbids[0] if release.album_artist_mbids else ""
            found[mbid] = Album(
                mbid=mbid,
                title=release.title,
                artist=release.artist,
                artist_mbid=first if first not in NOT_FOLLOWABLE else "",
                artist_key=release.album_artist_mbids or (release.artist.casefold(),),
                added_at=release.added_at,
            )
        elif release.added_at < known.added_at:
            found[mbid] = replace(known, added_at=release.added_at)

    counts = store.listened(user_id, found)
    plays = store.album_plays(user_id)
    for mbid, album in found.items():
        heard = plays.get(mbid)
        found[mbid] = replace(
            album,
            listens=max(counts.get(mbid, 0), heard.listens if heard else 0),
            last_played=_iso(datetime.fromtimestamp(heard.last_listened_at, UTC)) if heard else "",
        )
    return list(found.values())


def library(store: Store, user_id: int, user_name: str, *, now: datetime | None = None) -> Library:
    """Everything the Library tab needs to know about the user's albums. No request."""
    now = now or datetime.now(UTC)
    found = albums(store, user_id)
    mbids = [a.mbid for a in found]
    stats_ready = listens.stats(store, user_name) is not None
    status = history.status(store, user_id, user_name)
    history_ready = stats_ready and status is not None and not status.filling
    lately = _iso(now - LATELY)
    return Library(
        albums=tuple(found),
        never_played=frozenset(a.mbid for a in found if stats_ready and not a.listens),
        partial=tracks.partial_albums(store, user_id, mbids),
        not_lately=frozenset(
            a.mbid for a in found if history_ready and a.listens and a.last_played < lately
        ),
        wishlist=frozenset(store.wishlisted(user_id, mbids)),
        stats_ready=stats_ready,
        history_ready=history_ready,
        seed=now.date().isoformat(),
    )


def filters(found: Library) -> list[Choice]:
    """The filter chips: those with albums to show."""
    chips = (Choice(key, label, len(found.holds(key))) for key, label in FILTERS.items())
    return [chip for chip in chips if chip.count]


def known_filter(found: Library, key: str) -> str:
    """`key` if it's a filter that can be told, else no filter. One with nothing in it stays,
    so a bookmark says so instead of quietly showing everything."""
    if key == NEVER_PLAYED and not found.stats_ready:
        return ""
    if key == NOT_LATELY and not found.history_ready:
        return ""
    return key if key in FILTERS else ""


def sorts(filter_key: str) -> list[Choice]:
    """The sorts offered with a filter."""
    offered = [Choice(key, label) for key, label in SORTS.items()]
    return [Choice(COVERED, COVERED_LABEL), *offered] if filter_key == PARTIAL else offered


def known_sort(filter_key: str, key: str) -> str:
    """`key` if it's offered with this filter, else the filter's default."""
    if any(choice.key == key for choice in sorts(filter_key)):
        return key
    return DEFAULT_SORTS.get(filter_key, ARTIST)


def chosen(found: Library, filter_key: str = "", sort: str = "") -> list[Album]:
    """The albums of a filter (`""`: all of them), in the order of a sort (`""`: the filter's
    default)."""
    shown = [a for a in found.albums if not filter_key or a.mbid in found.holds(filter_key)]
    return _sorted(shown, known_sort(filter_key, sort), found)


def url(filter_key: str = "", sort: str = "", page: int = 1) -> str:
    """The grid's URL; what a filter would do anyway is left out, so it stays short."""
    params = {
        "filter": filter_key,
        "sort": "" if sort == known_sort(filter_key, "") else sort,
        "page": str(page) if page > 1 else "",
    }
    query = urlencode({k: v for k, v in params.items() if v})
    return f"/library/albums?{query}" if query else "/library/albums"


def page(found: Library, filter_key: str = "", sort: str = "", number: int = 1) -> Page:
    """A page of the grid, from what's in the URL; what doesn't make sense there is left out."""
    filter_key = known_filter(found, filter_key)
    sort = known_sort(filter_key, sort)
    shown = chosen(found, filter_key, sort)
    number = max(number, 1)
    start = (number - 1) * PAGE
    offered = {chip.key: chip for chip in filters(found)}
    if filter_key:
        offered.setdefault(filter_key, Choice(filter_key, FILTERS[filter_key]))
    chips = tuple(
        Link(
            f"{offered[key].label} · {offered[key].count}",
            url("" if key == filter_key else key),
            key == filter_key,
        )
        for key in FILTERS
        if key in offered
    )
    links = tuple(Link(s.label, url(filter_key, s.key), s.key == sort) for s in sorts(filter_key))
    return Page(
        albums=tuple(shown[start : start + PAGE]),
        total=len(shown),
        of=len(found.albums),
        filter_key=filter_key,
        sort=next(link for link in links if link.on),
        chips=chips,
        sorts=links,
        url=url(filter_key, sort),
        next_url=url(filter_key, sort, number + 1) if start + PAGE < len(shown) else "",
    )


def _sorted(shown: list[Album], sort: str, found: Library) -> list[Album]:
    by_artist = sorted(
        shown, key=lambda a: (sort_name(a.artist).casefold(), a.title.casefold(), a.mbid)
    )
    if sort == TITLE:
        return sorted(shown, key=lambda a: (a.title.casefold(), a.mbid))
    if sort == ADDED:
        return sorted(by_artist, key=lambda a: a.added_at, reverse=True)
    if sort == MIXED:
        return mixed(by_artist, found.seed)
    if sort == PLAYED:  # before the history began, then the longest ago, then never played
        return sorted(
            mixed(by_artist, found.seed),
            key=lambda a: (not a.listens, bool(a.last_played), a.last_played),
        )
    if sort == COVERED:

        def covered(album: Album) -> float:
            part = found.partial.get(album.mbid)
            return -part.have / part.of if part else 0.0

        return sorted(mixed(by_artist, found.seed), key=covered)
    return by_artist


def mixed(shown: list[Album], seed: str) -> list[Album]:
    """`shown` in an order that spreads each artist's albums evenly through it: an artist with
    four albums gets one in each quarter, from a starting point of their own."""
    rng = random.Random(seed)
    by_artist: dict[tuple[str, ...], list[Album]] = {}
    for album in shown:
        by_artist.setdefault(album.artist_key, []).append(album)
    places: dict[str, float] = {}
    for group in by_artist.values():
        rng.shuffle(group)
        start = rng.random()
        for index, album in enumerate(group):
            places[album.mbid] = (index + start) / len(group)
    return sorted(shown, key=lambda a: places[a.mbid])


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")
