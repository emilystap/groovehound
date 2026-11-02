"""A release group's editions (releases), from one MusicBrainz browse that feeds both the
wishlist's *Where to buy* (roadmap 1c) and *Choose edition* (1g).

The browse asks for `inc=media labels url-rels`: formats and track counts, labels and catalogue
numbers, and the purchase and streaming links, all in one request. It's only made when someone
opens one of those on an item, so items nobody looks at cost nothing; the raw response is
cached and refreshed (again only when opened) after a randomized `TTL`, since MusicBrainz asks
clients not to poll for changes. Responses cached before 1g lack media and labels: they still
give links, but count as due, so they're refetched when next opened. All of it is CC0.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .brainz.http import NotFound, ServiceError, ServicePause
from .brainz.musicbrainz import MusicBrainzClient
from .db import Store, WishlistItem

CACHE_KIND = "release-group-releases"
TTL = (timedelta(days=30), timedelta(days=60))  # refreshed (when opened) at random within
COUNTRIES = {"XW": "Worldwide", "XE": "Europe"}  # MusicBrainz's pseudo-countries; others as is


@dataclass(frozen=True)
class Cached:
    """A cached browse: `data` ({} when MusicBrainz had nothing), and whether it's `due` a
    refresh (old, or from before media and labels were asked for)."""

    data: dict
    due: bool

    @property
    def complete(self) -> bool:
        """It has what *Choose edition* shows (media and labels), not just links."""
        return _complete(self.data)


@dataclass(frozen=True)
class Edition:
    """One row of *Choose edition*: an edition, or several identical in everything shown."""

    mbids: tuple[str, ...]  # the first is the one saved when the row is picked
    details: str  # e.g. "2024-06-07 · GB · 2×CD · 15 tracks"  # noqa: RUF003
    labels: str  # e.g. "Atlantic · 075678 · explicit"
    official: bool = True

    @property
    def mbid(self) -> str:
        return self.mbids[0]


@dataclass(frozen=True)
class Choice:
    """What an item's *Choose edition* shows without a request. `editions` is None until
    MusicBrainz has been asked (or only asked before 1g); `due` = opening it should ask
    (again). `chosen` is the saved edition's row, when the cached browse has it."""

    editions: tuple[Edition, ...] | None
    chosen: Edition | None
    due: bool


def choice(store: Store, item: WishlistItem, *, now: datetime | None = None) -> Choice:
    entry = cached(store, item.release_group_mbid, now=now)
    if entry is None or not entry.complete:
        return Choice(None, None, due=True)
    rows = tuple(editions(entry.data, item.title))
    chosen = next((e for e in rows if item.release_mbid in e.mbids), None)
    return Choice(rows, chosen if item.release_mbid else None, due=entry.due)


def cached(store: Store, mbid: str, *, now: datetime | None = None) -> Cached | None:
    """The cached browse of the release group's releases; None if it's never been asked for."""
    entry = store.cached("musicbrainz", CACHE_KIND, mbid)
    if entry is None:
        return None
    data = entry.data or {}
    fresh = entry.refresh_after > _iso(now or datetime.now(UTC)) and _complete(data)
    return Cached(data, due=not fresh)


def fetch(
    store: Store,
    client: MusicBrainzClient,
    pause: ServicePause,
    mbid: str,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> dict:
    """The release group's releases: from the cache while it's fresh, otherwise one request
    ({} if the release group is gone). Raises ServiceError if MusicBrainz failed or is paused
    after a failure (starting the pause, for a new failure)."""
    now = now or datetime.now(UTC)
    entry = cached(store, mbid, now=now)
    if entry is not None and not entry.due:
        return entry.data
    waiting = pause.remaining()
    if waiting:
        raise ServiceError(
            f"MusicBrainz is paused after an error; try again in {math.ceil(waiting / 60)} min."
        )
    try:
        data = client.release_group_releases(mbid)
    except NotFound:
        data = None  # merged or deleted since it was saved: remembered, so not asked again soon
    except ServiceError as exc:
        pause.start(exc.retry_after)
        raise
    low, high = TTL
    ttl = low + (high - low) * (rng or random).random()
    store.save_cached("musicbrainz", CACHE_KIND, mbid, data, _iso(now + ttl), fetched_at=_iso(now))
    return data or {}


def editions(data: dict, group_title: str = "") -> list[Edition]:
    """*Choose edition*'s rows: what tells the editions apart, as on the release group's
    MusicBrainz page. Editions identical in everything shown share a row; official ones first,
    then oldest first (undated last), then in MusicBrainz's order."""
    rows: dict[tuple[str, str], tuple[tuple, list[str]]] = {}
    for order, release in enumerate(data.get("releases") or []):
        if not release.get("id"):
            continue
        official = release.get("status") == "Official"
        details, labels = _describe(release, group_title)
        date = release.get("date") or ""
        key = (details, labels)
        if key not in rows:
            rows[key] = ((not official, not date, date, order), [])
        rows[key][1].append(release["id"])
    return [
        Edition(tuple(mbids), details, labels, official=not sort[0])
        for (details, labels), (sort, mbids) in sorted(rows.items(), key=lambda r: r[1][0])
    ]


def formats_text(media: list[dict] | None) -> str:
    """A release's formats as MusicBrainz writes them: "2×CD", "CD + DVD"."""  # noqa: RUF002
    formats = Counter(m.get("format") or "" for m in media or [])
    return " + ".join(f"{n}×{f}" if n > 1 else f for f, n in formats.items() if f)  # noqa: RUF001


def _describe(release: dict, group_title: str) -> tuple[str, str]:
    """Two short lines: date · country · formats · tracks (· status, unless official), over
    title (when it differs from the group's) · label · catalogue number · disambiguation.
    Barcode and packaging are left out: the MusicBrainz page has them when they matter."""
    media = release.get("media") or []
    tracks = sum(int(m.get("track-count") or 0) for m in media)
    status = release.get("status") or ""
    country = release.get("country") or ""
    label_info = release.get("label-info") or []
    title = release.get("title") or ""
    details = [
        release.get("date") or "",
        COUNTRIES.get(country, country),
        formats_text(media),
        f"{tracks} track{'s' if tracks != 1 else ''}" if tracks else "",
        status if status != "Official" else "",
    ]
    labels = [
        title if title != group_title else "",
        _unique((i.get("label") or {}).get("name") for i in label_info),
        _unique(i.get("catalog-number") for i in label_info),
        release.get("disambiguation") or "",
    ]
    return " · ".join(p for p in details if p), " · ".join(p for p in labels if p)


def _unique(values) -> str:
    return ", ".join(dict.fromkeys(v for v in values if v))


def _complete(data: dict) -> bool:
    return all("media" in r and "label-info" in r for r in data.get("releases") or [])


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")
