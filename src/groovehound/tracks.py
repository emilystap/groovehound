"""Tracks (roadmap 5k): which songs of an album you have, mostly for albums you own only part of.

Two things, both matched against the library scan's per-file tags:

- **Partial albums**, without a request: a library edition's file count against its track
  count in the release group's cached browse (`releases.py`, asked for `inc=media` by *Choose
  edition*), else as its files' tags give it (`library.coverage`): "8 of 12 tracks". Never
  fetched to fill it in.
- **Track lists**: an edition's tracks, each marked owned or not *by recording*, so a song you
  have on a compilation or a single counts wherever else it appears. One release lookup
  (`inc=recordings artist-credits`) per edition, only when a track list is asked for; the raw
  response is cached (shared) and refreshed, again only when asked for, after a randomized
  `TTL`. The edition is the one in the library, else the wishlist item's chosen one, else
  `default_edition` of the cached browse. An edition you have every track of needs no lookup:
  its files' tags, which Picard wrote from MusicBrainz, give the list (`local_track_list`).
"""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import library, releases
from .brainz.http import NotFound, ServiceError, ServicePause
from .brainz.musicbrainz import MusicBrainzClient
from .db import LibraryFile, Store
from .search import credit_text

CACHE_KIND = "release-tracks"
TTL = (timedelta(days=30), timedelta(days=60))  # refreshed (when asked for) at random within


@dataclass(frozen=True)
class Partial:
    """A release group you have only some tracks of: the best-covered edition in the library."""

    release_mbid: str
    have: int  # files in the library
    of: int  # tracks on the edition


@dataclass(frozen=True)
class Track:
    number: str  # as printed: "3", "A1"
    title: str
    artist: str  # the track's artist credit
    length_ms: int | None
    recording_mbid: str
    owned: bool  # the recording is in the library, on this release or any other


@dataclass(frozen=True)
class Medium:
    position: int
    format: str  # "CD", "Digital Media", ...; "" when MusicBrainz doesn't say
    title: str  # a disc's own title, if it has one
    tracks: tuple[Track, ...]


@dataclass(frozen=True)
class TrackList:
    release_mbid: str
    title: str
    artist: str
    media: tuple[Medium, ...]
    local: bool = False  # from the files' tags, not a MusicBrainz lookup

    @property
    def total(self) -> int:
        return sum(len(m.tracks) for m in self.media)

    @property
    def owned(self) -> int:
        return sum(t.owned for m in self.media for t in m.tracks)

    @property
    def length_ms(self) -> int | None:
        """The whole edition's length, if every track's is known."""
        lengths = [t.length_ms for m in self.media for t in m.tracks]
        if not lengths or None in lengths:
            return None
        return sum(n or 0 for n in lengths)


@dataclass(frozen=True)
class Pick:
    """The edition a track list is for, and why that one."""

    release_mbid: str
    # "library", "wishlist" (the item's chosen edition), "default", or "chosen" (picked to look
    # at on the album page)
    reason: str


# --- Partial albums (no requests) ---------------------------------------------------------


def partial_albums(
    store: Store, user_id: int, release_group_mbids: Iterable[str]
) -> dict[str, Partial]:
    """Which of these release groups the user has only part of, by release group: those with
    an edition in the library whose track count is known (from the cached browse, else the
    files' tags), and none complete."""
    result = {}
    for mbid in dict.fromkeys(release_group_mbids):
        editions = store.library_editions(user_id, mbid)
        if not editions:
            continue
        entry = releases.cached(store, mbid)
        counts = track_counts(entry.data) if entry is not None else {}
        known = [(e, of) for e in editions if (of := counts.get(e.release_mbid) or e.tracks)]
        if not known or any(e.complete or e.files >= of for e, of in known):
            continue
        best, of = max(known, key=lambda k: k[0].files / k[1])
        result[mbid] = Partial(best.release_mbid, best.files, of)
    return result


def track_counts(browse: dict) -> dict[str, int]:
    """Each edition's track count, from a browse of the release group's releases with media;
    editions without media (a response from before 1g) are left out."""
    counts = {}
    for release in browse.get("releases") or []:
        media = release.get("media")
        if release.get("id") and media:
            tracks = sum(int(m.get("track-count") or 0) for m in media)
            if tracks:
                counts[release["id"]] = tracks
    return counts


# --- Which edition ------------------------------------------------------------------------


def default_edition(browse: dict) -> str | None:
    """The edition a release group's track list shows when nothing says which: the earliest
    official one (any, if none is official) with the commonest track count, so a deluxe edition
    or a bonus-track single release doesn't stand for the album. None without editions."""
    counts = track_counts(browse)
    candidates = [r for r in browse.get("releases") or [] if r.get("id") in counts]
    official = [r for r in candidates if r.get("status") == "Official"]
    candidates = official or candidates
    if not candidates:
        return None
    candidates.sort(key=lambda r: (not r.get("date"), r.get("date") or ""))  # undated last
    tally = Counter(counts[r["id"]] for r in candidates)
    top = max(tally.values())
    # the earliest with a commonest count (of counts tied for commonest, the earliest's)
    return next(r["id"] for r in candidates if tally[counts[r["id"]]] == top)


def pick_edition(
    store: Store,
    user_id: int,
    release_group_mbid: str,
    chosen: str | None = None,
    browse: dict | None = None,
) -> Pick | None:
    """The edition to show the tracks of: the one in the library (the most files), else the
    wishlist item's `chosen` one, else the default of the release group's `browse` (none
    without one: fetch it with `releases.fetch` first)."""
    editions = store.library_editions(user_id, release_group_mbid)
    if editions:
        return Pick(editions[0].release_mbid, "library")
    if chosen:
        return Pick(chosen, "wishlist")
    default = default_edition(browse or {})
    return Pick(default, "default") if default else None


# --- Track lists (one lookup per edition, on request) --------------------------------------


def cached(store: Store, release_mbid: str, *, now: datetime | None = None) -> dict | None:
    """The edition's cached lookup while it's fresh ({} if MusicBrainz didn't have it), else
    None: then `fetch` asks."""
    entry = store.cached("musicbrainz", CACHE_KIND, release_mbid)
    if entry is None or entry.refresh_after <= _iso(now or datetime.now(UTC)):
        return None
    return entry.data or {}


def fetch(
    store: Store,
    client: MusicBrainzClient,
    pause: ServicePause,
    release_mbid: str,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> dict:
    """The edition with its tracks: from the cache while it's fresh, otherwise one request ({}
    if the release is gone). Raises ServiceError if MusicBrainz failed or is paused after a
    failure (starting the pause, for a new failure)."""
    now = now or datetime.now(UTC)
    data = cached(store, release_mbid, now=now)
    if data is not None:
        return data
    waiting = pause.remaining()
    if waiting:
        raise ServiceError(
            f"MusicBrainz is paused after an error; try again in {math.ceil(waiting / 60)} min."
        )
    try:
        data = client.release_tracks(release_mbid)
    except NotFound:
        data = None  # merged or deleted: remembered, so not asked again soon
    except ServiceError as exc:
        pause.start(exc.retry_after)
        raise
    low, high = TTL
    ttl = low + (high - low) * (rng or random).random()
    store.save_cached(
        "musicbrainz", CACHE_KIND, release_mbid, data, _iso(now + ttl), fetched_at=_iso(now)
    )
    return data or {}


def track_list(store: Store, user_id: int, release: dict) -> TrackList | None:
    """A looked-up release's tracks, each marked owned if its recording is in the user's
    library. None for an empty response (a release MusicBrainz no longer has)."""
    if not release.get("id"):
        return None
    media = release.get("media") or []
    recordings = (
        (t.get("recording") or {}).get("id") for m in media for t in m.get("tracks") or []
    )
    owned = store.owned_recordings(user_id, (r for r in recordings if r))
    return TrackList(
        release_mbid=release["id"],
        title=release.get("title") or "",
        artist=credit_text(release.get("artist-credit")),
        media=tuple(
            Medium(
                position=int(m.get("position") or n),
                format=m.get("format") or "",
                title=m.get("title") or "",
                tracks=tuple(_track(t, owned) for t in m.get("tracks") or []),
            )
            for n, m in enumerate(media, start=1)
        ),
    )


def local_track_list(store: Store, user_id: int, release_mbid: str) -> TrackList | None:
    """A library edition's tracks from its files' tags, if there's a file for every track
    (`library.coverage`), else None: then it takes a lookup to know what's missing. Every
    track is owned; a track in two formats is listed once."""
    files = store.library_release_files(user_id, release_mbid)
    totals = library.disc_totals(files)
    if totals is None:
        return None
    by_track: dict[tuple[int, int], LibraryFile] = {}
    for f in files:  # by path, so the first of a track's files
        disc = f.disc_number or 1
        if f.track_number and disc in totals:
            by_track.setdefault((disc, f.track_number), f)
    if any((d, n) not in by_track for d, total in totals.items() for n in range(1, total + 1)):
        return None
    media = []
    for disc in totals:
        on_disc = sorted((n, f) for (d, n), f in by_track.items() if d == disc)
        media.append(
            Medium(
                position=disc,
                format=_most(f.media for _, f in on_disc),
                title=_most(f.disc_title for _, f in on_disc),
                tracks=tuple(
                    Track(
                        number=str(n),
                        title=f.title,
                        artist=f.artist,
                        length_ms=f.length_ms,
                        recording_mbid=f.recording_mbid or "",
                        owned=True,
                    )
                    for n, f in on_disc
                ),
            )
        )
    return TrackList(
        release_mbid=release_mbid,
        title=_most(f.album for f in files),
        artist=_most(f.album_artist for f in files),
        media=tuple(media),
        local=True,
    )


def _most(values: Iterable[str]) -> str:
    """The commonest value that isn't empty (the first of equals), or ""."""
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else ""


def _track(track: dict, owned: set[str]) -> Track:
    recording = track.get("recording") or {}
    length = track.get("length") or recording.get("length")
    return Track(
        number=track.get("number") or str(track.get("position") or ""),
        title=track.get("title") or recording.get("title") or "",
        artist=credit_text(track.get("artist-credit") or recording.get("artist-credit")),
        length_ms=int(length) if length else None,
        recording_mbid=recording.get("id") or "",
        owned=recording.get("id") in owned,
    )


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")
