"""Search for something heard in the wild (roadmap 1a): an album by title, or a song by title to
find the albums it's on.

One MusicBrainz search request per *Search* press, never as-you-type. The raw response is cached
for `SEARCH_TTL`, so reloading the results page, going back to it, or pressing *Search* again with
the same words doesn't ask again. A failed search starts `musicbrainz.PAUSE`, and while it runs only
cached results are shown.

Results are release groups (the album, whatever the edition), with the editions seen where the
response lists them, so *Add to wishlist* can save one. Tags and ratings are CC BY-NC-SA and not
used here.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from .brainz.http import ServiceError, ServicePause
from .brainz.musicbrainz import MusicBrainzClient
from .db import Store

KINDS = {"album": "release-group", "song": "recording"}  # what's searched -> MusicBrainz entity
SEARCH_TTL = timedelta(days=1)  # a repeated search within this reuses the cached response
SEARCH_KEEP = timedelta(days=7)  # older cached searches are dropped
MAX_TITLE = 200  # longer input is cut, so the query stays sane


@dataclass(frozen=True)
class Edition:
    """A specific release of a release group, for the *which edition* choice."""

    mbid: str
    label: str  # e.g. "Brat · 2024-06-07 · GB · CD"


@dataclass(frozen=True)
class Hit:
    """One release group in the results."""

    mbid: str
    title: str
    artist_credit: str
    artist_mbids: tuple[str, ...] = ()
    primary_type: str = ""
    secondary_types: tuple[str, ...] = ()
    first_release_date: str = ""
    editions: tuple[Edition, ...] = ()
    songs: tuple[str, ...] = ()  # song searches: the matching recordings on it

    @property
    def type_label(self) -> str:
        """e.g. "Album", "Album · Live", or "" when MusicBrainz has no type."""
        return " · ".join(t for t in (self.primary_type, *self.secondary_types) if t)


@dataclass
class _Group:
    """A release group being assembled from a song search's releases."""

    hit: Hit
    order: int
    official: bool = False
    dates: list[str] = field(default_factory=list)
    editions: dict[str, tuple[str, Edition]] = field(default_factory=dict)  # id -> (date, ...)
    songs: dict[str, None] = field(default_factory=dict)  # ordered set


def clean(text: str) -> str:
    """Input as searched and cached: whitespace collapsed, at most MAX_TITLE characters."""
    return " ".join(text.split())[:MAX_TITLE]


def search(
    store: Store,
    client: MusicBrainzClient,
    pause: ServicePause,
    kind: str,
    title: str,
    artist: str = "",
    *,
    now: datetime | None = None,
) -> list[Hit]:
    """Release groups matching an album (`kind` "album") or a song ("song") search: at most one
    request, none when the same search is cached. Raises ServiceError if MusicBrainz failed or
    is paused after a failure, ValueError for an unknown kind or an empty title."""
    entity = KINDS.get(kind)
    title, artist = clean(title), clean(artist)
    if entity is None:
        raise ValueError(f"unknown search kind: {kind!r}")
    if not title:
        raise ValueError("enter a title to search for")
    now = now or datetime.now(UTC)
    cache_kind = f"search-{entity}"
    key = f"{title.casefold()}\x1f{artist.casefold()}"

    entry = store.cached("musicbrainz", cache_kind, key)
    if entry is not None and entry.data is not None and entry.refresh_after > _iso(now):
        data = entry.data
    else:
        waiting = pause.remaining()
        if waiting:
            raise ServiceError(
                "Searching is paused after a MusicBrainz error; "
                f"try again in {math.ceil(waiting / 60)} min."
            )
        try:
            if entity == "release-group":
                data = client.search_release_groups(title, artist)
            else:
                data = client.search_recordings(title, artist)
        except ServiceError as exc:
            pause.start(exc.retry_after)
            raise
        store.drop_cached_before("musicbrainz", cache_kind, _iso(now - SEARCH_KEEP))
        store.save_cached(
            "musicbrainz", cache_kind, key, data, _iso(now + SEARCH_TTL), fetched_at=_iso(now)
        )
    return release_group_hits(data) if entity == "release-group" else recording_hits(data)


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")


def credit_text(credit: list[dict] | None) -> str:
    """An artist credit as MusicBrainz shows it: "Charli xcx", "A feat. B"."""
    return "".join(f"{c.get('name', '')}{c.get('joinphrase', '')}" for c in credit or []).strip()


def credit_mbids(credit: list[dict] | None) -> tuple[str, ...]:
    return tuple(c["artist"]["id"] for c in credit or [] if (c.get("artist") or {}).get("id"))


def release_group_hits(data: dict) -> list[Hit]:
    """Hits from a release group search, in MusicBrainz's (relevance) order."""
    hits = []
    for group in data.get("release-groups") or []:
        if not group.get("id"):
            continue
        hits.append(
            Hit(
                mbid=group["id"],
                title=group.get("title") or "",
                artist_credit=credit_text(group.get("artist-credit")),
                artist_mbids=credit_mbids(group.get("artist-credit")),
                primary_type=group.get("primary-type") or "",
                secondary_types=tuple(group.get("secondary-types") or ()),
                first_release_date=group.get("first-release-date") or "",
                editions=tuple(
                    Edition(r["id"], _edition_label(r, group.get("title") or ""))
                    for r in group.get("releases") or []
                    if r.get("id")
                ),
            )
        )
    return hits


def recording_hits(data: dict) -> list[Hit]:
    """The release groups a song search's recordings appear on. Original albums come first:
    groups with an official release and no secondary type (compilation, live, ...), then the
    rest, each in the order MusicBrainz ranked the recordings."""
    groups: dict[str, _Group] = {}
    for recording in data.get("recordings") or []:
        song = recording.get("title") or ""
        for release in recording.get("releases") or []:
            rg = release.get("release-group") or {}
            if not rg.get("id"):
                continue
            group = groups.get(rg["id"])
            if group is None:
                credit = release.get("artist-credit") or recording.get("artist-credit")
                group = groups[rg["id"]] = _Group(
                    Hit(
                        mbid=rg["id"],
                        title=rg.get("title") or release.get("title") or "",
                        artist_credit=credit_text(credit),
                        artist_mbids=credit_mbids(credit),
                        primary_type=rg.get("primary-type") or "",
                        secondary_types=tuple(rg.get("secondary-types") or ()),
                    ),
                    order=len(groups),
                )
            group.official |= release.get("status") == "Official"
            if release.get("date"):
                group.dates.append(release["date"])
            if release.get("id") and release["id"] not in group.editions:
                label = _edition_label(release, group.hit.title)
                group.editions[release["id"]] = (
                    release.get("date") or "",
                    Edition(release["id"], label),
                )
            if song:
                group.songs[song] = None
    ordered = sorted(
        groups.values(), key=lambda g: (bool(g.hit.secondary_types), not g.official, g.order)
    )
    return [
        replace(
            g.hit,
            # the earliest release seen: the response doesn't carry the group's own date
            first_release_date=min(g.dates, default=""),
            # oldest first, undated last
            editions=tuple(e for _, e in sorted(g.editions.values(), key=_by_date)),
            songs=tuple(g.songs),
        )
        for g in ordered
    ]


def _by_date(dated: tuple[str, Edition]) -> tuple[bool, str, str]:
    date, edition = dated
    return (not date, date, edition.label)


def _edition_label(release: dict, group_title: str) -> str:
    """What tells editions apart: the title when it differs from the group's (e.g. a deluxe
    edition), date, country and formats ("2×CD", as MusicBrainz writes it)."""  # noqa: RUF002
    formats = Counter(m.get("format") or "" for m in release.get("media") or [])
    parts = [
        release.get("title") if release.get("title") != group_title else "",
        release.get("date") or "",
        release.get("country") or "",
        " + ".join(f"{n}×{f}" if n > 1 else f for f, n in formats.items() if f),  # noqa: RUF001
    ]
    return " · ".join(p for p in parts if p) or release.get("title") or release["id"]
