"""Covers, cached (roadmap: "Thumbnails" under open questions).

Linked straight from the browser, a Cover Art Archive thumbnail redirects twice (to archive.org,
then to a mirror) and neither redirect can be cached, so every page load waited for both. Covers
of wishlist items are instead fetched by the server the first time they're shown, kept (as files
under the data folder, `Store.covers_dir`) and served from there; the browser can then keep them
too. They're 500 px (docs/ui-roadmap.md, "Covers at 500 px, as files"), sharp in a tile on a
phone; each is stored with its main colour (`tint.main_colour`) for the album page's header.

Like *Where to buy*, nothing is fetched in the background: a cover is asked for when a page
shows it and isn't cached, or its randomized `COVER_TTL` has passed (cover art gets added and
changed, so "no cover" is remembered for the same time). A failed fetch starts
`coverart.PAUSE`, during which the cached cover, if any, is shown as it is.

For a release group in your library (roadmap 5i), the cover comes from your files first: an
image beside the music, else the art embedded in it (`library.cover_art`), made into a 500 px
JPEG with Pillow. No request then, and covers the archive doesn't have. Each view checks that the
file it came from hasn't changed (one `stat`); if it has, or has gone, the cover is made again.
The archive is asked only when the library has no art. A library cover made smaller than
`COVER_SIZE` (before covers were 500 px) is made again when next shown; one from the archive is
replaced at its refresh, which is spread out already.
"""

from __future__ import annotations

import io
import logging
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageOps

from . import library, tint
from .brainz import coverart
from .brainz.coverart import CoverArtClient
from .brainz.http import NotFound, ServiceError, ServicePause
from .db import OWNER_ID, CachedCover, Store

log = logging.getLogger(__name__)

COVER_TTL = (timedelta(days=30), timedelta(days=60))  # refreshed (when shown) at random within
MAX_BYTES = 2_000_000  # a 500 px cover is about 100-150 KB
# Served from our own origin, so only image types a browser won't run as a page
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
COVER_SIZE = coverart.COVER_SIZE  # px, the longer side of a cover made from the library


def cover(
    store: Store,
    client: CoverArtClient | None,
    pause: ServicePause,
    release_group_mbid: str,
    *,
    library_root: Path | None = None,
    user_id: int = OWNER_ID,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> CachedCover:
    """The release group's cover: cached while fresh, otherwise made from the art in the
    user's library folder (`library_root`) if it's there and has some, otherwise one request
    (none without a `client`, i.e. no contact configured, or while paused: then the stale copy
    is kept). An `image` of None means it has no cover art. Raises ServiceError when the fetch
    failed (or can't be made) and nothing is cached."""
    now = now or datetime.now(UTC)
    cached = store.cover(release_group_mbid)
    fresh = cached is not None and cached.refresh_after > _iso(now)
    if (
        fresh
        and cached.source
        and (cached.size < COVER_SIZE or not _unchanged(library_root, cached))
    ):
        fresh = False
    if fresh and (cached.source or library_root is None):
        return cached
    if library_root is not None:
        made = _from_library(store, user_id, library_root, release_group_mbid, now, rng)
        if made is not None:
            return made
    if fresh:
        return cached  # from the archive, and the library has no art for it
    try:
        image, content_type = _fetch(client, pause, release_group_mbid)
    except ServiceError:
        if cached is not None:
            return cached  # stale beats nothing
        raise
    refresh_after = _refresh_after(now, rng)
    size = COVER_SIZE if image else 0
    colour = tint.main_colour(image) if image else ""
    store.save_cover(
        release_group_mbid, image, content_type, refresh_after, fetched_at=_iso(now), size=size,
        tint=colour,
    )  # fmt: skip
    return CachedCover(image, content_type, _iso(now), refresh_after, size=size, tint=colour)


def _from_library(
    store: Store,
    user_id: int,
    root: Path,
    release_group_mbid: str,
    now: datetime,
    rng: random.Random | None,
) -> CachedCover | None:
    """A cover from the first art in the library that makes one, cached; None if none does."""
    for art in library.cover_art(store, user_id, root, release_group_mbid):
        try:
            image = thumbnail(art.image)
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            log.info("Cover art in %s not used: %s", art.source, exc)
            continue
        refresh_after = _refresh_after(now, rng)
        colour = tint.main_colour(image)
        store.save_cover(
            release_group_mbid,
            image,
            "image/jpeg",
            refresh_after,
            fetched_at=_iso(now),
            source=art.source,
            source_mtime_ns=art.mtime_ns,
            size=COVER_SIZE,
            tint=colour,
        )
        return CachedCover(
            image, "image/jpeg", _iso(now), refresh_after, art.source, art.mtime_ns, COVER_SIZE,
            colour,
        )  # fmt: skip
    return None


def thumbnail(image: bytes) -> bytes:
    """`image` (any format Pillow reads) as a JPEG at most COVER_SIZE px on its longer side,
    turned upright, transparency on white. Raises OSError or ValueError if it can't be read."""
    with Image.open(io.BytesIO(image)) as opened:
        picture = ImageOps.exif_transpose(opened)
        picture.thumbnail((COVER_SIZE, COVER_SIZE))
        if picture.mode in ("RGBA", "LA", "PA") or "transparency" in picture.info:
            picture = picture.convert("RGBA")
            flat = Image.new("RGB", picture.size, "white")
            flat.paste(picture, mask=picture.getchannel("A"))
            picture = flat
        out = io.BytesIO()
        picture.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()


def _unchanged(root: Path | None, cached: CachedCover) -> bool:
    """Whether the library file a cover was made from is still there, as it was then."""
    if root is None:
        return False
    try:
        return (root / cached.source).stat().st_mtime_ns == cached.source_mtime_ns
    except OSError:
        return False


def _refresh_after(now: datetime, rng: random.Random | None) -> str:
    low, high = COVER_TTL
    return _iso(now + low + (high - low) * (rng or random).random())


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
