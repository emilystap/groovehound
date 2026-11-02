""" "Created for you" playlists from ListenBrainz (roadmap 4a): with a ListenBrainz username
configured, the playlists ListenBrainz generates for that user every day or week (Weekly
Exploration, Weekly Jams, Daily Jams), shown on the *Discover* page as the albums their
recordings are on, each with *Add to wishlist* and *Follow* for its artists.

**Requests**, all made by the background trickle (`reconcile.py`), never by a page:

- the list of playlists created for the user (`/1/user/<name>/playlists/createdfor`), cached
  raw for a randomized `fresh.TTL` of about a day, like the fresh-releases feeds;
- for each playlist shown (the newest of each recurring kind, while it hasn't expired), the
  playlist itself (`/1/playlist/<mbid>`), once: a generated playlist doesn't change, and the
  next one has a new MBID;
- and, since a playlist's tracks name only the album, one metadata request for all its
  recordings (`/1/metadata/recording/?inc=release`), giving each one's release group, once.

So about three requests a day: the list, and a new Daily Jams. Both playlist responses are
cached until the playlist expires, and dropped after that. Yearly playlists (Top Discoveries of
2025 etc.) have no expiry and aren't generated again, so they're left out. A 404 is cached like a
response; any other error pauses ListenBrainz (`fresh.fetch_into_cache`). ListenBrainz data is
CC0.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime

from . import fresh
from .brainz.http import ServicePause
from .brainz.listenbrainz import WEB_ROOT, ListenBrainzClient
from .brainz.musicbrainz import is_mbid
from .db import Store
from .new_releases import DISMISSED_KIND
from .search import Hit

LISTING_KIND = "created-for"
PLAYLIST_KIND = "playlist"
METADATA_KIND = "playlist-recordings"
# Shown in this order; other recurring kinds ListenBrainz adds come after, by name.
SOURCES = ("weekly-exploration", "weekly-jams", "daily-jams")
_JSPF_PLAYLIST = "https://musicbrainz.org/doc/jspf#playlist"
_JSPF_TRACK = "https://musicbrainz.org/doc/jspf#track"


@dataclass(frozen=True)
class Listed:
    """A playlist in the list of those created for the user."""

    mbid: str
    title: str
    source: str  # e.g. "weekly-exploration"
    date: str
    expires_at: str  # UTC, ISO


@dataclass(frozen=True)
class Playlist:
    mbid: str
    title: str
    source: str
    releases: tuple[Hit, ...]  # the albums its recordings are on, in playlist order
    hidden: int = 0  # recordings whose album isn't known yet, or dismissed

    @property
    def url(self) -> str:
        return f"{WEB_ROOT}/playlist/{self.mbid}/"


def _key(user_name: str) -> str:
    return user_name.strip().lower()


def due(store: Store, user_name: str, *, now: datetime | None = None) -> bool:
    entry = store.cached(fresh.SERVICE, LISTING_KIND, _key(user_name))
    return entry is None or entry.refresh_after <= _iso(now or datetime.now(UTC))


def fetch(
    store: Store,
    client: ListenBrainzClient,
    pause: ServicePause,
    user_name: str,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> int | None:
    """Fetch what's due: the list of playlists (about daily), then each shown playlist and its
    recordings' albums not fetched yet. How many requests were made, or None if none. Raises
    ServiceError if ListenBrainz failed (starting the pause) or is paused."""
    now = now or datetime.now(UTC)
    if not user_name.strip():
        return None
    asked = 0
    if due(store, user_name, now=now):
        fresh.fetch_into_cache(
            store,
            pause,
            LISTING_KIND,
            _key(user_name),
            lambda: client.playlists_created_for(user_name.strip()),
            now=now,
            rng=rng,
        )
        asked += 1
        for kind in (PLAYLIST_KIND, METADATA_KIND):  # expired playlists aren't shown
            store.drop_cached_before(fresh.SERVICE, kind, _iso(now))
    for listed in shown(store, user_name, now=now):
        entry = store.cached(fresh.SERVICE, PLAYLIST_KIND, listed.mbid)
        if entry is None:
            data = fresh.fetch_into_cache(
                store,
                pause,
                PLAYLIST_KIND,
                listed.mbid,
                lambda mbid=listed.mbid: client.playlist(mbid),
                now=now,
                refresh_after=listed.expires_at,
            )
            asked += 1
        else:
            data = entry.data
        recordings = [r for r, _ in _tracks(data or {})]
        if recordings and store.cached(fresh.SERVICE, METADATA_KIND, listed.mbid) is None:
            fresh.fetch_into_cache(
                store,
                pause,
                METADATA_KIND,
                listed.mbid,
                lambda recordings=recordings: client.recording_metadata(recordings),
                now=now,
                refresh_after=listed.expires_at,
            )
            asked += 1
    return asked or None


def shown(store: Store, user_name: str, *, now: datetime | None = None) -> list[Listed]:
    """From the cached list: the newest playlist of each recurring kind that hasn't expired,
    in `SOURCES` order. No request."""
    if not user_name.strip():
        return []
    entry = store.cached(fresh.SERVICE, LISTING_KIND, _key(user_name))
    if entry is None or not isinstance(entry.data, dict):
        return []
    stamp = _iso(now or datetime.now(UTC))
    newest: dict[str, Listed] = {}
    for item in entry.data.get("playlists") or []:
        listed = _listed((item or {}).get("playlist") if isinstance(item, dict) else None)
        if listed is None or listed.expires_at <= stamp:
            continue
        if listed.source not in newest or newest[listed.source].date < listed.date:
            newest[listed.source] = listed
    order = {source: index for index, source in enumerate(SOURCES)}
    return sorted(newest.values(), key=lambda p: (order.get(p.source, len(SOURCES)), p.source))


def playlists(
    store: Store, user_id: int, user_name: str, *, now: datetime | None = None
) -> list[Playlist]:
    """The shown playlists that are cached, as albums, leaving out those dismissed (shared with
    the home page). No request."""
    dismissed = store.dismissed(user_id, DISMISSED_KIND)
    found = []
    for listed in shown(store, user_name, now=now):
        releases, unknown = _releases(store, listed.mbid)
        if releases is None:
            continue  # not fetched yet, or gone (404)
        kept = tuple(hit for hit in releases if hit.mbid not in dismissed)
        hidden = unknown + sum(len(h.songs) for h in releases if h.mbid in dismissed)
        found.append(Playlist(listed.mbid, listed.title, listed.source, kept, hidden))
    return found


def listed(store: Store, user_name: str, release_group_mbid: str) -> bool:
    """Whether a shown playlist has a recording on this release group (so its cover may be
    fetched)."""
    for playlist in shown(store, user_name):
        releases, _ = _releases(store, playlist.mbid)
        if any(hit.mbid == release_group_mbid for hit in releases or ()):
            return True
    return False


def _releases(store: Store, playlist_mbid: str) -> tuple[list[Hit] | None, int]:
    """A cached playlist's albums, in order of their first recording, and how many recordings
    have no known release group (not in the metadata, or that isn't fetched yet)."""
    entry = store.cached(fresh.SERVICE, PLAYLIST_KIND, playlist_mbid)
    if entry is None or entry.data is None:
        return None, 0
    meta = store.cached(fresh.SERVICE, METADATA_KIND, playlist_mbid)
    metadata = meta.data if meta is not None and isinstance(meta.data, dict) else {}
    groups: dict[str, dict] = {}
    unknown = 0
    for recording, track in _tracks(entry.data):
        release = (metadata.get(recording) or {}).get("release") or {}
        group = release.get("release_group_mbid")
        if not isinstance(group, str) or not is_mbid(group):
            unknown += 1
            continue
        found = groups.setdefault(
            group.lower(),
            {
                "title": release.get("name") or track.get("album") or "",
                "credit": release.get("album_artist_name") or track.get("creator") or "",
                "artists": {},
                "songs": {},
            },
        )
        for mbid, name in _artists(track):
            found["artists"].setdefault(mbid, name)
        if track.get("title"):
            found["songs"][track["title"]] = None
    hits = [
        Hit(
            mbid=group,
            title=found["title"],
            artist_credit=found["credit"],
            artist_mbids=tuple(found["artists"]),
            artist_names=tuple(found["artists"].values()),
            songs=tuple(found["songs"]),
        )
        for group, found in groups.items()
    ]
    return hits, unknown


def _listed(playlist: object) -> Listed | None:
    if not isinstance(playlist, dict):
        return None
    mbid = _last_part(playlist.get("identifier"))
    extension = (playlist.get("extension") or {}).get(_JSPF_PLAYLIST) or {}
    meta = extension.get("additional_metadata") or {}
    source = (meta.get("algorithm_metadata") or {}).get("source_patch")
    expires_at = _utc(meta.get("expires_at"))
    if not is_mbid(mbid) or not isinstance(source, str) or not source or not expires_at:
        return None
    return Listed(
        mbid=mbid.lower(),
        title=str(playlist.get("title") or source),
        source=source,
        date=str(playlist.get("date") or ""),
        expires_at=expires_at,
    )


def _tracks(data: dict) -> list[tuple[str, dict]]:
    """A JSPF playlist's tracks with their recording MBIDs (tracks without one are left out)."""
    tracks = (data.get("playlist") or {}).get("track") or []
    found = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        identifier = track.get("identifier")
        if isinstance(identifier, list):  # JSPF allows several; ListenBrainz gives one
            identifier = next((i for i in identifier if "/recording/" in str(i)), "")
        recording = _last_part(identifier)
        if is_mbid(recording):
            found.append((recording.lower(), track))
    return found


def _artists(track: dict) -> list[tuple[str, str]]:
    """A track's artists (MBID, name), from ListenBrainz's metadata, else the identifiers."""
    extension = (track.get("extension") or {}).get(_JSPF_TRACK) or {}
    found = []
    for artist in (extension.get("additional_metadata") or {}).get("artists") or []:
        mbid = (artist or {}).get("artist_mbid") if isinstance(artist, dict) else None
        if isinstance(mbid, str) and is_mbid(mbid):
            found.append((mbid.lower(), str(artist.get("artist_credit_name") or "")))
    if not found:
        for identifier in extension.get("artist_identifiers") or []:
            mbid = _last_part(identifier)
            if is_mbid(mbid):
                found.append((mbid.lower(), ""))
    return found


def _last_part(url: object) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1] if isinstance(url, str) else ""


def _utc(value: object) -> str:
    """An ISO timestamp as UTC ISO (ListenBrainz sometimes leaves the zone off: UTC), or ""."""
    if not isinstance(value, str):
        return ""
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return ""
    return _iso(when if when.tzinfo else when.replace(tzinfo=UTC))


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")
