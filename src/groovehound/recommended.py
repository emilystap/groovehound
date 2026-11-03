"""Recommended albums (roadmap 5h): with a ListenBrainz username configured, ListenBrainz's
collaborative-filtering recording recommendations for that user, shown on the *Discover* page as
the albums those recordings are on, each with *Add to wishlist*, *Follow* for its artists and
*Dismiss*.

**Requests**, all made by the background trickle (`reconcile.py`), never by a page:

- the recommendations (`/1/cf/recommendation/user/<name>/recording`), the best `COUNT`, cached
  raw for a randomized `fresh.TTL` of about a day, like the fresh-releases feeds;
- right after, one metadata request per `BATCH` recordings never played
  (`/1/metadata/recording/?inc=artist release`), for their release groups and artists. Only
  those not fetched already: none while the recommendations' `last_updated` (when the model
  made them) hasn't changed, and all of them again when it has.

So about two requests a day, none to MusicBrainz. The endpoint is experimental, so a 502 (seen
once for 100 recordings at a time; 59 worked) waits for the next day's fetch instead of pausing
ListenBrainz, keeping what's cached. A 204 (no recommendations) and a 404 are cached like a
response; any other error pauses ListenBrainz (`fresh.fetch_into_cache`). ListenBrainz data is
CC0.

**What shows:** recordings with no `latest_listened_at` (never played: 41 of a real user's best
100 had been), as their albums, ranked by the best score of their songs, each with those songs.
An album is left out if it's in the library (5b), was played (`listens.played`), is on the
wishlist or was dismissed (shared with the home page). Artists aren't filtered: an album you
haven't heard by an artist you know is worth seeing.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime

from . import fresh, listens
from .brainz.http import NotFound, ServiceError, ServicePause
from .brainz.listenbrainz import ListenBrainzClient
from .brainz.musicbrainz import is_mbid
from .db import Store
from .new_releases import DISMISSED_KIND
from .search import Hit

CACHE_KIND = "cf-recommendations"
METADATA_KIND = "cf-recordings"
COUNT = 100  # recommendations asked for (ListenBrainz keeps 1,000)
BATCH = 50  # recordings per metadata request: 100 at once got a 502 (checked 2026-11-02)
SHOWN = 20  # albums shown, best first
BAD_GATEWAY = 502


@dataclass(frozen=True)
class Recommended:
    albums: tuple[Hit, ...]  # best first, at most SHOWN
    left_out: int  # albums not shown: in the library, played, on the wishlist or dismissed
    unmatched: int  # unplayed recordings whose album isn't known
    last_updated: str  # when ListenBrainz made them, UTC ISO ("" if it didn't say)


def _key(user_name: str) -> str:
    return user_name.strip().lower()


def due(store: Store, user_name: str, *, now: datetime | None = None) -> bool:
    entry = store.cached(fresh.SERVICE, CACHE_KIND, _key(user_name))
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
    """Fetch the recommendations if there is a user and they're due, then the metadata of the
    unplayed ones not fetched yet. How many requests were made, or None if none. Raises
    ServiceError if ListenBrainz failed (starting the pause) or is paused; a 502 doesn't."""
    now = now or datetime.now(UTC)
    if not user_name.strip() or not due(store, user_name, now=now):
        return None
    key = _key(user_name)
    previous = store.cached(fresh.SERVICE, CACHE_KIND, key)
    try:
        data = fresh.fetch_into_cache(
            store,
            pause,
            CACHE_KIND,
            key,
            lambda: client.recording_recommendations(user_name.strip(), count=COUNT),
            now=now,
            rng=rng,
            no_pause=(BAD_GATEWAY,),
        )
    except ServiceError as exc:
        if exc.status != BAD_GATEWAY:
            raise
        # keep what's cached, and ask again the next day
        store.save_cached(
            fresh.SERVICE,
            CACHE_KIND,
            key,
            previous.data if previous is not None else None,
            fresh.next_refresh(now, rng),
            fetched_at=previous.fetched_at if previous is not None else _iso(now),
        )
        return 1
    return 1 + _fetch_metadata(store, client, pause, key, data or {}, now=now)


def _fetch_metadata(
    store: Store,
    client: ListenBrainzClient,
    pause: ServicePause,
    key: str,
    data: dict,
    *,
    now: datetime,
) -> int:
    """Fetch the metadata of the unplayed recordings not cached for these recommendations
    (same `last_updated`), `BATCH` at a time; how many requests were made. A 502 keeps what was
    found and leaves the rest for the next day."""
    updated = _payload(data).get("last_updated")
    entry = store.cached(fresh.SERVICE, METADATA_KIND, key)
    known = entry.data if entry is not None and isinstance(entry.data, dict) else {}
    found = dict(known.get("recordings") or {}) if known.get("last_updated") == updated else {}
    missing = [r for r, _ in unplayed(data) if r not in found]
    asked = 0
    for start in range(0, len(missing), BATCH):
        batch = missing[start : start + BATCH]
        waiting = math.ceil(pause.remaining() / 60)
        if waiting:
            raise ServiceError(
                f"ListenBrainz is paused after an error; try again in {waiting} min."
            )
        asked += 1
        try:
            reply = client.recording_metadata(batch, artists=True)
        except NotFound:
            reply = {}
        except ServiceError as exc:
            if exc.status == BAD_GATEWAY:
                break
            pause.start(exc.retry_after)
            raise
        found.update({k.lower(): v for k, v in reply.items() if isinstance(v, dict)})
        for recording in batch:  # not known to ListenBrainz: not asked about again
            found.setdefault(recording, {})
    if asked or known.get("last_updated") != updated:
        store.save_cached(
            fresh.SERVICE,
            METADATA_KIND,
            key,
            {"last_updated": updated, "recordings": found},
            fresh.next_refresh(now),  # not used: fetched with the recommendations only
            fetched_at=_iso(now),
        )
    return asked


def recommended(store: Store, user_id: int, user_name: str) -> Recommended | None:
    """The cached recommendations as albums, or None if there are none cached (no user, not
    fetched yet, no such user). No request."""
    if not user_name.strip():
        return None
    entry = store.cached(fresh.SERVICE, CACHE_KIND, _key(user_name))
    if entry is None or entry.data is None:
        return None
    albums, unmatched = _albums(store, _key(user_name), entry.data)
    mbids = [hit.mbid for hit in albums]
    hidden = (
        set(store.in_library(user_id, mbids))
        | listens.played(store, user_id, mbids)
        | store.wishlisted(user_id, mbids)
        | (store.dismissed(user_id, DISMISSED_KIND) & set(mbids))
    )
    kept = [hit for hit in albums if hit.mbid not in hidden]
    updated = _payload(entry.data).get("last_updated")
    return Recommended(
        albums=tuple(kept[:SHOWN]),
        left_out=len(hidden),
        unmatched=unmatched,
        last_updated=_iso(datetime.fromtimestamp(updated, UTC)) if isinstance(updated, int) else "",
    )


def listed(store: Store, user_name: str, release_group_mbid: str) -> bool:
    """Whether a cached recommendation is on this release group (so its cover may be
    fetched)."""
    return find(store, user_name, release_group_mbid) is not None


def find(store: Store, user_name: str, release_group_mbid: str) -> Hit | None:
    """The release group as a cached recommendation has it, shown or not; None if none is on
    it. No request."""
    if not user_name.strip():
        return None
    entry = store.cached(fresh.SERVICE, CACHE_KIND, _key(user_name))
    if entry is None or entry.data is None:
        return None
    albums, _ = _albums(store, _key(user_name), entry.data)
    return next((hit for hit in albums if hit.mbid == release_group_mbid), None)


def _albums(store: Store, key: str, data: dict) -> tuple[list[Hit], int]:
    """The unplayed recordings' albums, best first, and how many recordings have no known
    release group (not in the metadata, or it isn't fetched yet)."""
    meta = store.cached(fresh.SERVICE, METADATA_KIND, key)
    metadata = (meta.data or {}).get("recordings") or {} if meta is not None else {}
    groups: dict[str, dict] = {}
    unmatched = 0
    for recording, score in unplayed(data):
        found = metadata.get(recording) or {}
        release = found.get("release") or {}
        group = release.get("release_group_mbid")
        if not isinstance(group, str) or not is_mbid(group):
            unmatched += 1
            continue
        credit = found.get("artist") or {}
        album = groups.setdefault(
            group.lower(),
            {
                "title": release.get("name") or "",
                "credit": release.get("album_artist_name") or credit.get("name") or "",
                "score": score,
                "artists": {},
                "songs": {},
            },
        )
        album["score"] = max(album["score"], score)
        for artist in credit.get("artists") or []:
            mbid = artist.get("artist_mbid") if isinstance(artist, dict) else None
            if isinstance(mbid, str) and is_mbid(mbid):
                album["artists"].setdefault(mbid.lower(), str(artist.get("name") or ""))
        name = (found.get("recording") or {}).get("name")
        if name:
            album["songs"][name] = None
    ranked = sorted(groups.items(), key=lambda item: -item[1]["score"])  # stable: feed order
    hits = [
        Hit(
            mbid=group,
            title=album["title"],
            artist_credit=album["credit"],
            artist_mbids=tuple(album["artists"]),
            artist_names=tuple(album["artists"].values()),
            songs=tuple(album["songs"]),
        )
        for group, album in ranked
    ]
    return hits, unmatched


def unplayed(data: dict) -> list[tuple[str, float]]:
    """The recommended recordings never played (`latest_listened_at` null), with their scores,
    in the response's order (best first)."""
    found = []
    for entry in _payload(data).get("mbids") or []:
        if not isinstance(entry, dict) or entry.get("latest_listened_at") is not None:
            continue
        recording = entry.get("recording_mbid")
        score = entry.get("score")
        if isinstance(recording, str) and is_mbid(recording):
            found.append((recording.lower(), score if isinstance(score, int | float) else 0.0))
    return found


def _payload(data: object) -> dict:
    payload = data.get("payload") if isinstance(data, dict) else None
    return payload if isinstance(payload, dict) else {}


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")
