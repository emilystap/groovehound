"""What you've played, from ListenBrainz (roadmap 5e): with a ListenBrainz username configured,
the user's all-time listen counts per release group (`/1/stats/user/<name>/release-groups`,
public, no token). Library minus played is *never played*: a badge wherever an album in the
library shows, and a count per album artist on the Library page.

**Requests:** the stats are fetched by the background trickle (`reconcile.py`), never by a page,
and cached raw for a randomized `fresh.TTL` of about a day, like the fresh-releases feeds.
ListenBrainz recalculates them about daily anyway. A page holds up to `MAX_STATS` (1,000)
release groups, so most users need one request; a longer list is paged with `offset`, up to
`MAX_PAGES`. Checked 2026-11-02: the lists aren't cut to a top N (all 238 release groups of a
real user came back, down to those played once). A 204 (stats not calculated yet) and a 404
(no such user) are cached like a response; any other error pauses ListenBrainz. None to
MusicBrainz. The artist stats aren't fetched: what's played per artist follows from these.

**Matching** is by release group MBID only. A listen ListenBrainz couldn't link to MusicBrainz
(the stats list those without a release group, counted as `unlinked`), or linked to a song on
another album (a compilation, a single), doesn't make an album played; the Library page says so.
Library releases without a release group id can't be matched, so they get no badge.
ListenBrainz data is CC0.
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import fresh
from .brainz.http import ServicePause
from .brainz.listenbrainz import ListenBrainzClient
from .db import Store
from .follows import NOT_FOLLOWABLE

CACHE_KIND = "user-release-group-stats"
MAX_PAGES = 10  # 10,000 release groups; a longer list stops there


@dataclass(frozen=True)
class Stats:
    """What the cached stats say, apart from the counts (in the `listened` table)."""

    release_groups: int  # played release groups listed
    unlinked: int  # entries without a release group (listens not linked to MusicBrainz)
    last_updated: str  # when ListenBrainz calculated them, UTC ISO ("" if it didn't say)
    fetched_at: str


@dataclass
class ArtistUnplayed:
    """An album artist's release groups in the library, and which were never played."""

    name: str
    mbid: str  # the first album artist's, to link to their page ("" if none or not an artist)
    owned: int = 0
    never_played: list[str] = field(default_factory=list)  # titles, by title

    @property
    def unplayed(self) -> int:
        return len(self.never_played)


def _key(user_name: str) -> str:
    return user_name.strip().lower()


def due(store: Store, user_name: str, *, now: datetime | None = None) -> bool:
    entry = store.cached(fresh.SERVICE, CACHE_KIND, _key(user_name))
    return entry is None or entry.refresh_after <= _iso(now or datetime.now(UTC))


def fetch(
    store: Store,
    client: ListenBrainzClient,
    pause: ServicePause,
    user_id: int,
    user_name: str,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> int | None:
    """Fetch the user's stats if there is a user and they're due, and replace what the user has
    played; how many release groups they list, or None if nothing was asked. Raises
    ServiceError if ListenBrainz failed (starting the pause) or is paused."""
    now = now or datetime.now(UTC)
    if not user_name.strip() or not due(store, user_name, now=now):
        return None
    data = fresh.fetch_into_cache(
        store,
        pause,
        CACHE_KIND,
        _key(user_name),
        lambda: _all_pages(client, user_name.strip()),
        now=now,
        rng=rng,
    )
    if data is None:  # no such user: nothing is played
        store.replace_listened(user_id, {})
        return 0
    if not data:  # not calculated yet: keep what's known
        return 0
    counts: dict[str, int] = {}
    for entry in release_groups(data):
        mbid = entry.get("release_group_mbid")
        listens = entry.get("listen_count")
        if isinstance(mbid, str) and mbid and isinstance(listens, int):
            counts[mbid] = counts.get(mbid, 0) + listens
    store.replace_listened(user_id, counts)
    return len(counts)


def _all_pages(client: ListenBrainzClient, user_name: str) -> dict:
    """Every page of the stats as one response, with all the release groups in the first's
    payload. An empty dict if they haven't been calculated (a 204)."""
    data = client.user_release_group_stats(user_name)
    payload = data.get("payload") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return {}
    entries = list(release_groups(data))
    total = payload.get("total_release_group_count")
    total = total if isinstance(total, int) else 0
    for _ in range(MAX_PAGES - 1):
        if len(entries) >= total:
            break
        page = release_groups(client.user_release_group_stats(user_name, offset=len(entries)))
        if not page:
            break
        entries += page
    payload = {**payload, "release_groups": entries, "count": len(entries), "offset": 0}
    return {"payload": payload}


def stats(store: Store, user_name: str) -> Stats | None:
    """The cached stats' summary, or None if there are none (no user, not fetched yet, no such
    user, or not calculated yet). No request."""
    if not user_name.strip():
        return None
    entry = store.cached(fresh.SERVICE, CACHE_KIND, _key(user_name))
    if entry is None or not entry.data:
        return None
    entries = release_groups(entry.data)
    linked = sum(1 for e in entries if e.get("release_group_mbid"))
    updated = (entry.data.get("payload") or {}).get("last_updated")
    return Stats(
        release_groups=linked,
        unlinked=len(entries) - linked,
        last_updated=_iso(datetime.fromtimestamp(updated, UTC)) if isinstance(updated, int) else "",
        fetched_at=entry.fetched_at,
    )


def never_played(store: Store, user_id: int, user_name: str, mbids: Iterable[str]) -> set[str]:
    """Which of these release groups (in the library) were never played: empty while there are
    no stats, so nothing is marked on a guess. No request."""
    if stats(store, user_name) is None:
        return set()
    mbids = set(mbids)
    return mbids - set(store.listened(user_id, mbids))


def unplayed_by_artist(store: Store, user_id: int, user_name: str) -> list[ArtistUnplayed]:
    """Album artists with release groups in the library never played, most such first (then by
    name), each with how many they have; empty while there are no stats. No request."""
    if stats(store, user_name) is None:
        return []
    by_artist: dict[tuple[str, ...], ArtistUnplayed] = {}
    titles: dict[tuple[str, ...], dict[str, str]] = {}
    for release in store.library(user_id):
        if not release.release_group_mbid:
            continue
        key = release.album_artist_mbids or (release.artist.casefold(),)
        if key not in by_artist:
            first = release.album_artist_mbids[0] if release.album_artist_mbids else ""
            mbid = first if first and first not in NOT_FOLLOWABLE else ""
            by_artist[key] = ArtistUnplayed(name=release.artist, mbid=mbid)
        titles.setdefault(key, {}).setdefault(release.release_group_mbid, release.title)
    played = store.listened(user_id, (m for t in titles.values() for m in t))
    found = []
    for key, artist in by_artist.items():
        artist.owned = len(titles[key])
        artist.never_played = sorted(
            (t for m, t in titles[key].items() if m not in played), key=str.casefold
        )
        if artist.never_played:
            found.append(artist)
    found.sort(key=lambda a: (-a.unplayed, a.name.casefold()))
    return found


def release_groups(data: dict) -> list[dict]:
    """The entries in a stats response: `{"payload": {"release_groups": [...]}}`."""
    payload = data.get("payload") if isinstance(data, dict) else None
    entries = payload.get("release_groups") if isinstance(payload, dict) else None
    return [e for e in entries or [] if isinstance(e, dict)]


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")
