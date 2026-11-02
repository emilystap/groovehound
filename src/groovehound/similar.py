"""Similar artists (roadmap 4b): artists like one on an artist page, from ListenBrainz's LB Radio
artist endpoint (`/1/lb-radio/artist/<mbid>`), each with *Follow* and *Dismiss*.

**Requests:** only when you ask (*Find similar artists* on the artist page), one per artist, and
none while the answer is cached: for a randomized `TTL` of a month or so (who's similar changes
slowly). After that the cached list still shows, and asking again is offered. A 404 is cached like
a response (ListenBrainz knows nothing similar); any other error pauses ListenBrainz
(`fresh.fetch_into_cache`). The cache is shared; dismissing is per user. ListenBrainz data is CC0.

The response is keyed by artist MBID, the asked-about artist among them, and isn't ranked (the
keys come back sorted), so artists are shown by name, a leading "The" aside. Checked against
the live API 2026-11-02.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import fresh
from .brainz.http import ServicePause
from .brainz.listenbrainz import ListenBrainzClient
from .brainz.musicbrainz import is_mbid
from .db import Store

CACHE_KIND = "similar-artists"
DISMISSED_KIND = "artist"  # in the dismissed table
COUNT = 20  # similar artists asked for
TTL = (timedelta(days=28), timedelta(days=42))  # asked again (on request) only after this


@dataclass(frozen=True)
class SimilarArtist:
    mbid: str
    name: str


@dataclass(frozen=True)
class Similar:
    artists: tuple[SimilarArtist, ...]  # by name, leaving out those dismissed
    fetched_at: str
    due: bool  # old enough to ask again
    hidden: int = 0  # dismissed


def cached(store: Store, user_id: int, mbid: str, *, now: datetime | None = None) -> Similar | None:
    """Artists similar to this one, if ListenBrainz has been asked (None if not). No request."""
    entry = store.cached(fresh.SERVICE, CACHE_KIND, mbid)
    if entry is None:
        return None
    found = _artists(entry.data, mbid)
    dismissed = store.dismissed(user_id, DISMISSED_KIND)
    kept = tuple(a for a in found if a.mbid not in dismissed)
    return Similar(
        artists=kept,
        fetched_at=entry.fetched_at,
        due=entry.refresh_after <= _iso(now or datetime.now(UTC)),
        hidden=len(found) - len(kept),
    )


def fetch(
    store: Store,
    client: ListenBrainzClient,
    pause: ServicePause,
    mbid: str,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> bool:
    """Ask ListenBrainz for artists similar to this one, unless the cached answer is still
    fresh (so repeated clicks ask nothing). Whether a request was made. Raises ServiceError if
    ListenBrainz failed (starting the pause) or is paused."""
    now = now or datetime.now(UTC)
    entry = store.cached(fresh.SERVICE, CACHE_KIND, mbid)
    if entry is not None and entry.refresh_after > _iso(now):
        return False
    low, high = TTL
    fresh.fetch_into_cache(
        store,
        pause,
        CACHE_KIND,
        mbid,
        lambda: client.similar_artists(mbid, count=COUNT),
        now=now,
        refresh_after=_iso(now + low + (high - low) * (rng or random).random()),
    )
    return True


def _artists(data: object, seed: str) -> list[SimilarArtist]:
    """The similar artists in a response, by name ("The" aside): each key with a named
    recording, not the asked-about artist."""
    if not isinstance(data, dict):
        return []
    found = []
    for mbid, recordings in data.items():
        if not is_mbid(mbid) or mbid.lower() == seed or not isinstance(recordings, list):
            continue
        name = next(
            (
                r["similar_artist_name"]
                for r in recordings
                if isinstance(r, dict) and isinstance(r.get("similar_artist_name"), str)
            ),
            "",
        )
        if name.strip():
            found.append(SimilarArtist(mbid.lower(), name.strip()))
    return sorted(found, key=lambda a: (_sort_name(a.name), a.mbid))


def _sort_name(name: str) -> str:
    """Roughly MusicBrainz's sort name (which the response doesn't give): "The Clash" as
    "clash"."""
    name = name.casefold()
    return name[4:] if name.startswith("the ") else name


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")
