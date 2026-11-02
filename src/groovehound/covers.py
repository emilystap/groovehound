"""Cover thumbnails, cached (roadmap: "Thumbnails" under open questions).

Linked straight from the browser, a Cover Art Archive thumbnail redirects twice (to archive.org,
then to a mirror) and neither redirect can be cached, so every page load waited for both. Covers
of wishlist items are instead fetched by the server the first time they're shown, kept in the
database and served from there; the browser can then keep them too.

Like *Where to buy*, nothing is fetched in the background: a cover is asked for when a page
shows it and isn't cached, or its randomized `COVER_TTL` has passed (cover art gets added and
changed, so "no cover" is remembered for the same time). A failed fetch starts
`coverart.PAUSE`, during which the cached cover, if any, is shown as it is.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

from .brainz.coverart import CoverArtClient
from .brainz.http import NotFound, ServiceError, ServicePause
from .db import CachedCover, Store

COVER_TTL = (timedelta(days=30), timedelta(days=60))  # refreshed (when shown) at random within
MAX_BYTES = 2_000_000  # a 250 px thumbnail is about 40 KB
# Served from our own origin, so only image types a browser won't run as a page
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


def cover(
    store: Store,
    client: CoverArtClient | None,
    pause: ServicePause,
    release_group_mbid: str,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> CachedCover:
    """The release group's thumbnail: cached while fresh, otherwise one request (none without a
    `client`, i.e. no contact configured, or while paused: then the stale copy is kept). An
    `image` of None means it has no cover art. Raises ServiceError when the fetch failed (or
    can't be made) and nothing is cached."""
    now = now or datetime.now(UTC)
    cached = store.cover(release_group_mbid)
    if cached is not None and cached.refresh_after > _iso(now):
        return cached
    try:
        image, content_type = _fetch(client, pause, release_group_mbid)
    except ServiceError:
        if cached is not None:
            return cached  # stale beats nothing
        raise
    low, high = COVER_TTL
    refresh_after = _iso(now + low + (high - low) * (rng or random).random())
    store.save_cover(release_group_mbid, image, content_type, refresh_after, fetched_at=_iso(now))
    return CachedCover(image, content_type, _iso(now), refresh_after)


def _fetch(
    client: CoverArtClient | None, pause: ServicePause, release_group_mbid: str
) -> tuple[bytes | None, str]:
    if client is None:
        raise ServiceError("no contact configured, so covers can't be fetched")
    waiting = pause.remaining()
    if waiting:
        raise ServiceError(
            "The Cover Art Archive is paused after an error; "
            f"try again in {math.ceil(waiting / 60)} min."
        )
    try:
        image, content_type = client.release_group_front(release_group_mbid)
    except NotFound:
        return None, ""
    except ServiceError as exc:
        pause.start(exc.retry_after)
        raise
    content_type = content_type.split(";")[0].strip().lower()
    if content_type not in IMAGE_TYPES or not image or len(image) > MAX_BYTES:
        raise ServiceError(
            f"Unexpected cover from the Cover Art Archive: {content_type!r}, {len(image)} bytes"
        )
    return image, content_type


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")
