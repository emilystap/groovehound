"""Fresh releases from ListenBrainz (roadmap 3a): the sitewide feed of every release dated within
`DAYS` of today, past and future, in **one request**, filtered locally by followed artists. New
releases then show up on the home page within a day, while MusicBrainz sees only the slow
per-artist browses (`reconcile.py`).

The raw response is cached (`api_cache`, one row) for a randomized `TTL` of about a day, so the
fetch drifts around the clock instead of happening at a fixed time. The background trickle asks
for it when it's due, and only while someone follows an artist; an error pauses ListenBrainz for
everyone (`ServicePause`), leaving MusicBrainz alone. Each fetch records the followed artists'
release groups in `release_groups_seen`, so one ListenBrainz reports before the artist's next
browse is new from the day it was reported (`follows.is_new`). ListenBrainz data is CC0.

The feed lists releases (editions), not release groups, and says less about them than a browse
(no artist names apart from the credit; secondary types only if it gives them), so
`new_releases` prefers a release group's browse whenever it has one and uses the feed only for
release groups the browse hasn't caught up with yet.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from .brainz.http import NotFound, ServiceError, ServicePause
from .brainz.listenbrainz import ListenBrainzClient
from .db import Follow, Store
from .search import Hit

SERVICE = "listenbrainz"
CACHE_KIND = "fresh-releases"
CACHE_KEY = "sitewide"
# Days either side of today. A fetch a day needs only one, but a release often reaches
# MusicBrainz days after its date, and the app may have been down: two weeks covers both.
DAYS = 14
TTL = (timedelta(hours=22), timedelta(hours=26))  # fetched again at random within (about daily)


def due(store: Store, *, now: datetime | None = None) -> bool:
    entry = store.cached(SERVICE, CACHE_KIND, CACHE_KEY)
    return entry is None or entry.refresh_after <= _iso(now or datetime.now(UTC))


def fetch(
    store: Store,
    client: ListenBrainzClient,
    pause: ServicePause,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> int | None:
    """Fetch the feed if it's due and someone follows an artist; how many of the followed
    artists' release groups it reported for the first time, or None if nothing was asked.
    Raises ServiceError if ListenBrainz failed (starting the pause) or is paused."""
    now = now or datetime.now(UTC)
    followed = store.followed_artists()
    if not followed or not due(store, now=now):
        return None
    data = fetch_into_cache(
        store,
        pause,
        CACHE_KIND,
        CACHE_KEY,
        lambda: client.fresh_releases(days=DAYS, around=now.date()),
        now=now,
        rng=rng,
    )
    fetched_at = _iso(now)
    new = 0
    by_artist: dict[str, list[str]] = {}
    for release in releases(data or {}):
        for mbid in release.get("artist_mbids") or []:
            if mbid in followed and release.get("release_group_mbid"):
                by_artist.setdefault(mbid, []).append(release["release_group_mbid"])
    for mbid, groups in by_artist.items():
        before = store.release_groups_seen(mbid)
        new += len(set(groups) - set(before))
        store.see_release_groups(mbid, groups, fetched_at)
    return new


def fetch_into_cache(
    store: Store,
    pause: ServicePause,
    kind: str,
    key: str,
    call: Callable[[], dict],
    *,
    now: datetime,
    rng: random.Random | None = None,
    refresh_after: str = "",
) -> dict | None:
    """Make one ListenBrainz request (`call`) unless paused, and cache its response for a
    randomized `TTL` (or until `refresh_after`, if given). A 404 is cached as None (nothing for
    this key; no pause). Raises ServiceError if ListenBrainz failed (starting the pause) or is
    paused."""
    waiting = pause.remaining()
    if waiting:
        raise ServiceError(
            f"ListenBrainz is paused after an error; try again in {math.ceil(waiting / 60)} min."
        )
    try:
        data = call()
    except NotFound:
        data = None
    except ServiceError as exc:
        pause.start(exc.retry_after)
        raise
    if not refresh_after:
        low, high = TTL
        refresh_after = _iso(now + low + (high - low) * (rng or random).random())
    store.save_cached(SERVICE, kind, key, data, refresh_after, fetched_at=_iso(now))
    return data


def cached_hits(store: Store, follow: Follow) -> list[Hit]:
    """The cached feed's release groups credited to this artist (whatever their type), each
    dated by its earliest release in the feed. No request."""
    entry = store.cached(SERVICE, CACHE_KIND, CACHE_KEY)
    if entry is None or entry.data is None:
        return []
    found: dict[str, Hit] = {}
    for release in releases(entry.data):
        mbids = tuple(m for m in release.get("artist_mbids") or [] if isinstance(m, str))
        group = release.get("release_group_mbid")
        if follow.artist_mbid not in mbids or not group:
            continue
        date = release.get("release_date") or ""
        if group in found and (not date or found[group].first_release_date <= date):
            continue  # keep the earliest dated release of each release group
        found[group] = Hit(
            mbid=group,
            title=release.get("release_name") or "",
            artist_credit=release.get("artist_credit_name") or "",
            artist_mbids=mbids,
            # names aren't in the feed: only the followed artist's is known
            artist_names=tuple(follow.name if m == follow.artist_mbid else "" for m in mbids),
            primary_type=release.get("release_group_primary_type") or "",
            secondary_types=secondary_types(release.get("release_group_secondary_type")),
            first_release_date=date,
        )
    return list(found.values())


def releases(data: dict | list) -> list[dict]:
    """The releases in a feed response: `{"payload": {"releases": [...]}}` (or a bare list)."""
    if isinstance(data, dict):
        data = (data.get("payload") or {}).get("releases") or []
    return [r for r in data if isinstance(r, dict)]


def secondary_types(value: object) -> tuple[str, ...]:
    if isinstance(value, str) and value:
        return (value,)
    if isinstance(value, list):
        return tuple(v for v in value if isinstance(v, str) and v)
    return ()


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")
