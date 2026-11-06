"""An artist's page (docs/ui-roadmap.md, "Artist pages", U4): the picture at the top of it and of
each similar artist's tile, the line under the name, and the discography as album tiles. All of
it comes from what's stored, so none of this asks for anything.

The picture, until Navidrome can give a photo (5g): four of their covers already cached, newest
first, albums before the rest, or the newest alone with fewer than four to show (a mosaic needs
all four), or their initials with none. Only cached covers, because a picture that fetched would
turn every similar artist into requests; an artist nothing of theirs has been seen of gets the
monogram, and following them fills it in (2e). The header is tinted from the newest of them.

A tile's cover comes through `/cover` when that's served and cheap: an artist you follow, an album
you have or want, or a cover already cached. Otherwise it's linked straight from the Cover Art
Archive, as you may look at this artist only once.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Protocol

from . import follows
from .db import Follow, LibraryRelease, Store
from .follows import Discography
from .library_artists import LibraryArtist
from .search import Hit
from .shelves import Tile, out_label

MOSAIC = 4  # covers in a mosaic


class Named(Protocol):
    mbid: str
    name: str


@dataclass(frozen=True)
class Picture:
    """What stands for an artist: four covers, one cover, or (no covers) their initials."""

    covers: tuple[str, ...]  # release groups
    tint: str  # the newest of them with one
    initials: str


def initials(name: str) -> str:
    """Up to two letters for a monogram: "Harbour Lights" as "HL", "Björk" as "B"."""
    words = ["".join(c for c in word if c.isalnum()) for word in name.split()]
    return "".join(word[0] for word in words if word)[:2].upper() or "?"


def release_groups(found: Discography | None, releases: Iterable[LibraryRelease] = ()) -> list[str]:
    """An artist's release groups for their picture: albums and EPs, newest first, then the
    rest of the browse, then what's in the library under them."""
    hits = (*found.main, *found.other, *(found.singles or ())) if found else ()
    owned = (r.release_group_mbid for r in releases if r.release_group_mbid)
    return list(dict.fromkeys((*(h.mbid for h in hits), *owned)))


def picture(store: Store, name: str, groups: Iterable[str]) -> Picture:
    """An artist's picture from `groups`, best first."""
    groups = list(groups)
    cached = store.cached_covers(groups)
    have = [mbid for mbid in groups if mbid in cached]
    chosen = tuple(have[:MOSAIC]) if len(have) >= MOSAIC else tuple(have[:1])
    tint = next((cached[mbid] for mbid in chosen if cached[mbid]), "")
    return Picture(chosen, tint, initials(name))


def pictures(store: Store, user_id: int, artists: Iterable[Named]) -> dict[str, Picture]:
    """The pictures of several artists (similar artists), by MBID."""
    found = {}
    for artist in artists:
        disco, _ = follows.cached_discography(store, artist.mbid)
        groups = release_groups(disco, store.library_of_artist(user_id, artist.mbid))
        found[artist.mbid] = picture(store, artist.name, groups)
    return found


def followed_picture(store: Store, user_id: int, follow: Follow) -> Picture:
    """The picture of an artist you follow, in the list of them."""
    disco, _ = follows.cached_discography(store, follow.artist_mbid)
    groups = release_groups(disco, store.library_of_artist(user_id, follow.artist_mbid))
    return picture(store, follow.name, groups)


def library_pictures(
    store: Store, user_id: int, artists: Iterable[LibraryArtist]
) -> dict[str, Picture]:
    """The pictures of the library's album artists, by MBID, from their albums in the library."""
    owned: dict[str, list[str]] = {}
    for release in store.library(user_id):
        if release.release_group_mbid:
            for mbid in release.album_artist_mbids:
                owned.setdefault(mbid, []).append(release.release_group_mbid)
    return {a.mbid: picture(store, a.name, dict.fromkeys(owned.get(a.mbid, ()))) for a in artists}


def summary(found: Discography | None) -> str:
    """The line under the name: "Group · Bristol folk · 4 albums, 2 EPs"."""
    if found is None:
        return ""
    kinds = Counter(hit.primary_type for hit in found.main)
    albums, eps = kinds["Album"], kinds["EP"]
    counts = ", ".join(
        part
        for part in (
            f"{albums} album{'' if albums == 1 else 's'}" if albums else "",
            f"{eps} EP{'' if eps == 1 else 's'}" if eps else "",
        )
        if part
    )
    return " · ".join(p for p in (found.artist.type, found.artist.disambiguation, counts) if p)


def tiles(
    hits: Iterable[Hit], artist_mbid: str, today: str, served: Collection[str] | None = None
) -> tuple[Tile, ...]:
    """Release groups as tiles on their artist's page: under the title what it is and when, not
    the artist; other artists on it have their names on a grid, and *Follow* in the ⋯ menu.
    `served`: the covers to take from `/cover` (None: all), the rest from the archive."""
    return tuple(_tile(hit, artist_mbid, today, served) for hit in hits)


def _tile(hit: Hit, artist_mbid: str, today: str, served: Collection[str] | None) -> Tile:
    date = hit.first_release_date
    return Tile(
        hit.mbid,
        hit.title,
        " · ".join(p for p in (hit.type_label or "Unknown type", date[:4]) if p),
        tuple(
            artist
            for artist in follows.credited(hit.artist_mbids, hit.artist_names, hit.artist_credit)
            if artist.mbid != artist_mbid
        ),
        hit,
        badge=out_label(date, today) if date > today else "",
        detail=hit.artist_credit if len(hit.artist_mbids) > 1 else "",
        archive=served is not None and hit.mbid not in served,
    )
