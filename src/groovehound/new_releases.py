"""New and upcoming releases from followed artists (roadmap 2d), for the home page.

Read entirely from what's cached, so the home page asks nothing: each followed artist's browse
of albums and EPs (and of singles, for artists followed for them), ListenBrainz's fresh
releases (`fresh.py`, roadmap 3a) for release groups those browses don't have yet, and when
each release group was first seen (`release_groups_seen`). The background trickle
(`reconcile.py`) keeps both fresh.

A release group shows if the follow is for its types (`follows.wanted`) and it is either
- **upcoming**: first released after today (whenever it was first seen, so an album announced
  before you followed still shows), or
- **new**: released within the last `NEW_FOR` (whenever you followed, so following someone
  shows what they put out lately; a year or month alone counts from when it was first seen,
  within that year or month: see `_new_from`), or first seen after you followed
  (`follows.is_new`) within the last `NEW_FOR`, unless it's a catalogue addition: something
  released more than `CATALOGUE` before it was first seen is an old record MusicBrainz only
  just got.

Dismissed ones stay dismissed (per user), and ones already in the library (roadmap 5b) come
after the rest of their list. All of it is CC0.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import follows, fresh, library
from .db import Follow, Store, local_date
from .search import Hit, release_group_hits

NEW_FOR = timedelta(days=90)  # how long a release group stays new after release / first seen
CATALOGUE = timedelta(days=365)  # released this long before first seen: not a new release
DISMISSED_KIND = "release-group"  # in the dismissed table


@dataclass(frozen=True)
class NewRelease:
    hit: Hit
    artist: Follow  # the followed artist it's from (the first, if several are credited)
    first_seen: str


@dataclass(frozen=True)
class Feed:
    upcoming: tuple[NewRelease, ...]  # soonest first
    new: tuple[NewRelease, ...]  # newest first, undated last

    def __bool__(self) -> bool:
        return bool(self.upcoming or self.new)


def feed(store: Store, user_id: int, *, now: datetime | None = None) -> Feed:
    """What's new and upcoming from the artists this user follows, from the cache only."""
    now = now or datetime.now(UTC)
    today = local_date(_iso(now))
    dismissed = store.dismissed(user_id, DISMISSED_KIND)
    upcoming: dict[str, NewRelease] = {}
    new: dict[str, NewRelease] = {}
    for follow in store.follows(user_id):
        seen = store.release_groups_seen(follow.artist_mbid)
        for hit in _cached_hits(store, follow):
            first_seen = seen.get(hit.mbid, "")
            if hit.mbid in dismissed or hit.mbid in upcoming or hit.mbid in new:
                continue
            if not follows.wanted(follow.types, hit):
                continue
            found = NewRelease(hit, follow, first_seen)
            if hit.first_release_date > today:
                upcoming[hit.mbid] = found
            elif _is_new(follow, hit, first_seen, now):
                new[hit.mbid] = found
    # newest release date first (then most recently seen), undated last; ISO dates sort as text
    newest = sorted(new.values(), key=lambda r: (r.hit.first_release_date, r.first_seen))
    owned = store.in_library(user_id, [*upcoming, *new])
    return Feed(
        upcoming=_owned_last(
            sorted(upcoming.values(), key=lambda r: r.hit.first_release_date), owned
        ),
        new=_owned_last(
            (
                *(r for r in reversed(newest) if r.hit.first_release_date),
                *(r for r in reversed(newest) if not r.hit.first_release_date),
            ),
            owned,
        ),
    )


def _owned_last(releases: Iterable[NewRelease], owned: Iterable[str]) -> tuple[NewRelease, ...]:
    return tuple(library.owned_last(releases, owned, lambda r: r.hit.mbid))


def _cached_hits(store: Store, follow: Follow) -> list[Hit]:
    """The artist's browses, then ListenBrainz's fresh releases for release groups the browses
    don't have yet (a browse knows a release group's first release date and every type; the
    feed only the releases near today)."""
    kinds = [follows.CACHE_KIND]
    if follow.singles_since:
        kinds.append(follows.SINGLES_KIND)
    hits = []
    for kind in kinds:
        entry = store.cached("musicbrainz", kind, follow.artist_mbid)
        if entry is not None:
            hits.extend(release_group_hits(entry.data or {}))
    browsed = {h.mbid for h in hits}
    return hits + [h for h in fresh.cached_hits(store, follow) if h.mbid not in browsed]


def _is_new(follow: Follow, hit: Hit, first_seen: str, now: datetime) -> bool:
    date = hit.first_release_date
    if date and _new_from(date, first_seen) >= local_date(_iso(now - NEW_FOR)):
        return True  # released lately, even if it was out before following
    if not first_seen or not follows.is_new(follow, hit, first_seen):
        return False
    if first_seen < _iso(now - NEW_FOR):
        return False
    first_seen_at = datetime.fromisoformat(first_seen)
    return not hit.first_release_date or _latest(hit.first_release_date) >= local_date(
        _iso(first_seen_at - CATALOGUE)
    )


def _new_from(date: str, first_seen: str) -> str:
    """The day a release group's `NEW_FOR` starts: its release date, or for a partial one
    (YYYY or YYYY-MM), the day it was first seen, kept within the year or month it names (an
    album announced as "2027" isn't new before 2027; one first seen after 2026 was out by its
    end). A refresh that brings the full date makes that the start instead."""
    if date.count("-") == 2:
        return date
    earliest = date + "-01" * (2 - date.count("-"))
    if not first_seen:
        return _latest(date)
    return min(max(earliest, local_date(first_seen)), _latest(date))


def _latest(date: str) -> str:
    """The latest day a partial date (YYYY or YYYY-MM) can mean, for comparing as text:
    "2025" could be December, so it isn't older than "2025-01-05"."""
    return date + "-99" * (2 - date.count("-"))


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")
