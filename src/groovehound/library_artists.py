"""Library artists (roadmap 5j) and suggested follows (5d): every album artist in your library,
followed or not, with how many of their release groups you have and, once their browse is
cached, how many you're missing.

The list makes no requests: it's built from the `library` table (the files' album artist ids)
and what's already cached, so an artist whose browse isn't cached shows as not checked yet
rather than being browsed to fill the column. Nothing browses library artists in the background
either: a library can have hundreds, and the trickle (2c) is for follows. An artist's page makes
the browse it always made (plus their singles, if the types counted include singles).

What counts as missing: release groups of the types followed (the follow's own, or for an
artist you don't follow, the default for new follows), as `follows.wanted` has it. What you
have always counts, whatever its type, so "You have 7 of 12" includes an owned compilation even
when compilations aren't followed. Release groups in the library under an artist that their
browse didn't return (promotional or bootleg only, past the browse's cap, credited differently)
are owned all the same, listed apart (`Gaps.also`). Various Artists and the other special
purpose artists aren't anyone's, so they're left out.

Suggested follows are the library artists you don't follow with `SUGGEST_AT` or more release
groups in the library, most first, until dismissed (per user).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace

from . import follows
from .db import Follow, LibraryRelease, Store
from .follows import Discography

SUGGEST_AT = 3  # release groups in the library before an artist you don't follow is suggested
SUGGESTIONS = 10  # suggested follows shown at most
DISMISSED_KIND = "suggested-follow"  # in the dismissed table


@dataclass(frozen=True)
class Gaps:
    """What you have of an artist's release groups and what you're missing, from their browse."""

    owned: frozenset[str]  # release groups in the library (any edition), of any type
    missing: frozenset[str]  # of the types counted, not in the library
    also: tuple[LibraryRelease, ...]  # in the library under them but not in the browse

    @property
    def have(self) -> int:
        return len(self.owned)

    @property
    def total(self) -> int:
        return len(self.owned) + len(self.missing)


@dataclass(frozen=True)
class LibraryArtist:
    mbid: str
    name: str
    sort_name: str
    owned: int  # release groups in the library with them as album artist
    following: bool
    gaps: Gaps | None  # None: their browse isn't cached (or their singles, if counted)


def count(store: Store, user_id: int) -> int:
    """How many album artists the user's library has (the special purpose ones aside)."""
    return len(store.library_artists(user_id) - follows.NOT_FOLLOWABLE)


def library_artists(
    store: Store, user_id: int, default_types: tuple[str, ...]
) -> list[LibraryArtist]:
    """Every album artist in the user's library, by sort name. `default_types`: what a new
    follow is for, counted for the artists not followed."""
    by_artist: dict[str, list[LibraryRelease]] = {}
    for release in store.library(user_id):
        for mbid in dict.fromkeys(release.album_artist_mbids):
            if mbid not in follows.NOT_FOLLOWABLE:
                by_artist.setdefault(mbid, []).append(release)
    followed = {f.artist_mbid: f for f in store.follows(user_id)}
    found = []
    for mbid, releases in by_artist.items():
        follow = followed.get(mbid)
        disco, _ = follows.cached_discography(store, mbid)
        name, sort_name = names(releases, follow, disco)
        types = follow.types if follow else default_types
        found.append(
            LibraryArtist(
                mbid=mbid,
                name=name,
                sort_name=sort_name,
                owned=len({_group(r) for r in releases}),
                following=follow is not None,
                gaps=_gaps(store, user_id, releases, disco, types),
            )
        )
    return sorted(found, key=lambda a: (a.sort_name.casefold(), a.mbid))


def suggestions(store: Store, user_id: int, artists: list[LibraryArtist]) -> list[LibraryArtist]:
    """The suggested follows among `library_artists`' output: not followed, at least
    `SUGGEST_AT` release groups in the library, not dismissed; most owned first."""
    dismissed = store.dismissed(user_id, DISMISSED_KIND)
    picks = [
        a for a in artists if not a.following and a.owned >= SUGGEST_AT and a.mbid not in dismissed
    ]
    return sorted(picks, key=lambda a: -a.owned)[:SUGGESTIONS]  # stable: by sort name within


def gaps(
    store: Store, user_id: int, mbid: str, found: Discography, types: tuple[str, ...]
) -> Gaps | None:
    """What an artist's page shows of what you have and what's missing, from their browse
    (`found`); None if nothing of theirs is in the library, or for Various Artists and the
    like. Counting singles needs them browsed: without, they're counted as owned only."""
    if mbid in follows.NOT_FOLLOWABLE:
        return None
    releases = store.library_of_artist(user_id, mbid)
    result = _gaps(store, user_id, releases, found, types, need_singles=False)
    return result if result and result.owned else None


def missing_only(found: Discography, gaps: Gaps) -> Discography:
    """The browse with only the missing release groups left (singles: None if none are)."""
    keep = gaps.missing
    singles = tuple(h for h in found.singles or () if h.mbid in keep)
    return replace(
        found,
        release_groups=tuple(h for h in found.release_groups if h.mbid in keep),
        singles=singles or None,
    )


def _gaps(
    store: Store,
    user_id: int,
    releases: list[LibraryRelease],
    found: Discography | None,
    types: tuple[str, ...],
    *,
    need_singles: bool = True,
) -> Gaps | None:
    if found is None or (need_singles and "single" in types and found.singles is None):
        return None
    hits = (*found.release_groups, *(found.singles or ()))
    browsed = {h.mbid for h in hits}
    owned = set(store.in_library(user_id, browsed)) | {_group(r) for r in releases}
    also: dict[str, LibraryRelease] = {}
    for release in releases:  # one per release group, the one with the most files
        group = _group(release)
        if group not in browsed and (group not in also or release.files > also[group].files):
            also[group] = release
    return Gaps(
        owned=frozenset(owned),
        missing=frozenset(h.mbid for h in hits if follows.wanted(types, h)) - owned,
        also=tuple(sorted(also.values(), key=lambda r: (r.title.casefold(), r.release_mbid))),
    )


def names(
    releases: list[LibraryRelease], follow: Follow | None, found: Discography | None
) -> tuple[str, str]:
    """An artist's name and sort name: as followed, else from their cached browse, else as the
    files name them (a release with them alone as album artist, if there is one)."""
    if follow is not None:
        return follow.name, follow.sort_name or _sort_name(follow.name)
    if found is not None:
        return found.artist.name, found.artist.sort_name or _sort_name(found.artist.name)
    alone = Counter(r.artist for r in releases if len(r.album_artist_mbids) == 1 and r.artist)
    name = alone.most_common(1)[0][0] if alone else releases[0].artist or "Unknown artist"
    return name, _sort_name(name)


def _sort_name(name: str) -> str:
    """Roughly MusicBrainz's sort name, when it isn't known: "The Clash" as "Clash"."""
    return name[4:] if name.casefold().startswith("the ") else name


def _group(release: LibraryRelease) -> str:
    """A release's release group, or the release itself when its files don't name one."""
    return release.release_group_mbid or release.release_mbid
