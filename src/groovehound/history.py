"""Your listen history, from ListenBrainz (roadmap 5f): with a ListenBrainz username configured,
the user's listens (`/1/user/<name>/listens`, public, no token), one row each in `listens`, so
Rediscover can tell an album played yesterday from one played months ago. 5e's all-time stats
(`listens.py`) say what was ever played; this says when.

**Requests:** made by the background trickle (`reconcile.py`), never by a page, 1,000 listens
each (`MAX_LISTENS`). The first fill pages back from the newest with `max_ts`, at most
`PAGES_PER_RUN` requests per wake-up and `MAX_PAGES` in all: ListenBrainz's docs recommend the
listens endpoint for "roughly ten pages or fewer", their export beyond, and ask clients not to
page through a whole history. A longer history keeps its newest 10,000 listens; albums played
before them still count as played through 5e's stats. After the fill, a top-up about once a day
(a randomized `fresh.TTL`) with `min_ts` a week (`OVERLAP`) before the newest listen kept, so
listens a player submits late (offline) are caught; with `min_ts` the API returns the listens
just after it, so a full page is followed by the next. No periodic re-fill: listens deleted or
backdated on ListenBrainz may stay out of step. The latest response is cached raw (`api_cache`),
which also schedules the next request. A 404 (no such user) is cached like a response; any other
error pauses ListenBrainz. None to MusicBrainz.

**Matching:** a listen's album is the release group its scrobbler sent (`additional_info`:
Navidrome sends the ids in your tags, so it's the album you played, as in the library), else
ListenBrainz's match (`mbid_mapping`); its recording is ListenBrainz's match, else the
scrobbler's. A listen with neither is kept and counted, not matched. Listens are keyed by
ListenBrainz's own identity for a listen (`listened_at`, `recording_msid`), so one fetched twice
(the top-up's overlap) counts once. A changed username starts the history again.
ListenBrainz data is CC0.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import fresh
from .brainz.http import ServicePause
from .brainz.listenbrainz import MAX_LISTENS, ListenBrainzClient
from .brainz.musicbrainz import is_mbid
from .db import Listen, Store

CACHE_KIND = "user-listens"
MAX_PAGES = 10  # the first fill's limit: 10,000 listens
PAGES_PER_RUN = 3  # requests per trickle wake-up, filling or catching up
OVERLAP = int(timedelta(days=7).total_seconds())  # a top-up looks back this far for late listens


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
    """Fill or top up the user's listen history if there is a user and it's due; how many
    listens were new, or None if nothing was asked. Raises ServiceError if ListenBrainz failed
    (starting the pause) or is paused; pages fetched before that are kept."""
    now = now or datetime.now(UTC)
    key = _key(user_name)
    if not key or not due(store, user_name, now=now):
        return None
    history = store.listen_history(user_id)
    if history is None or history.user_name != key:
        store.start_listen_history(user_id, key, _iso(now))
        history = store.listen_history(user_id)
    assert history is not None
    name = user_name.strip()
    if history.filled_at is None:
        return _fill(store, client, pause, user_id, name, history.pages, history.before, now, rng)
    return _top_up(store, client, pause, user_id, name, now, rng)


def _fill(
    store: Store,
    client: ListenBrainzClient,
    pause: ServicePause,
    user_id: int,
    name: str,
    pages: int,
    before: int | None,
    now: datetime,
    rng: random.Random | None,
) -> int:
    """Page back from `before` (the newest, if None), until the oldest listen, a short page or
    `MAX_PAGES`; what's left is asked at the next wake-up."""
    added = 0
    for _ in range(PAGES_PER_RUN):
        data = _ask(store, pause, name, lambda b=before: client.user_listens(name, max_ts=b), now)
        page = raw_listens(data or {})
        added += store.save_listens(user_id, parse(page))
        pages += 1
        times = [t for t in (_timestamp(r.get("listened_at")) for r in page) if t is not None]
        payload = (data or {}).get("payload")
        oldest = payload.get("oldest_listen_ts") if isinstance(payload, dict) else None
        done = (
            data is None  # no such user
            or len(page) < MAX_LISTENS
            or not times
            or pages >= MAX_PAGES
            or (isinstance(oldest, int) and min(times) <= oldest)
        )
        # max_ts excludes itself: + 1 keeps listens sharing the page's oldest second (kept once)
        before = min(times) + 1 if times else before
        store.listen_fill_progress(user_id, pages, before, _iso(now) if done else None)
        if done:
            _schedule(store, name, now, rng)
            break
    return added


def _top_up(
    store: Store,
    client: ListenBrainzClient,
    pause: ServicePause,
    user_id: int,
    name: str,
    now: datetime,
    rng: random.Random | None,
) -> int:
    """Listens since a week before the newest kept, page by page while pages come back full."""
    newest = store.newest_listen(user_id)
    since = newest - OVERLAP if newest is not None else None
    added = 0
    for _ in range(PAGES_PER_RUN):
        data = _ask(store, pause, name, lambda s=since: client.user_listens(name, min_ts=s), now)
        page = raw_listens(data or {})
        added += store.save_listens(user_id, parse(page))
        times = [t for t in (_timestamp(r.get("listened_at")) for r in page) if t is not None]
        if data is None or len(page) < MAX_LISTENS or not times or since is None:
            _schedule(store, name, now, rng)
            break
        # min_ts excludes itself: - 1 keeps listens sharing the page's newest second (kept once)
        since = max(max(times) - 1, since + 1)
    return added  # still full after PAGES_PER_RUN: due now, so the next wake-up goes on


def _ask(store: Store, pause: ServicePause, name: str, call, now: datetime) -> dict | None:
    """One request, cached and due again at once (`_schedule` sets the next day when done)."""
    return fresh.fetch_into_cache(
        store, pause, CACHE_KIND, _key(name), call, now=now, refresh_after=_iso(now)
    )


def _schedule(store: Store, name: str, now: datetime, rng: random.Random | None) -> None:
    """Make the cached response due again in a randomized `fresh.TTL` (about a day)."""
    entry = store.cached(fresh.SERVICE, CACHE_KIND, _key(name))
    low, high = fresh.TTL
    store.save_cached(
        fresh.SERVICE,
        CACHE_KIND,
        _key(name),
        entry.data if entry else None,
        _iso(now + low + (high - low) * (rng or random).random()),
        fetched_at=entry.fetched_at if entry else _iso(now),
    )


@dataclass(frozen=True)
class Status:
    """How much of the user's history is kept, for the Library page."""

    listens: int
    with_album: int  # listens matched to a release group
    since: str  # the oldest listen kept, UTC ISO ("" if none)
    filling: bool  # the first fill hasn't finished
    capped: bool  # the fill stopped at MAX_PAGES: older listens aren't kept


def status(store: Store, user_id: int, user_name: str) -> Status | None:
    """None without a user or before the first request for this one. No request."""
    history = store.listen_history(user_id)
    if not _key(user_name) or history is None or history.user_name != _key(user_name):
        return None
    summary = store.listen_summary(user_id)
    return Status(
        listens=summary.listens,
        with_album=summary.with_album,
        since=_iso(datetime.fromtimestamp(summary.oldest, UTC)) if summary.oldest else "",
        filling=history.filled_at is None,
        capped=history.filled_at is not None and history.pages >= MAX_PAGES,
    )


def parse(entries: list[dict]) -> list[Listen]:
    """The listens in a page that ListenBrainz identifies (`listened_at`, `recording_msid`),
    with their recording and album where known."""
    found = []
    for raw in entries:
        when = _timestamp(raw.get("listened_at"))
        msid = raw.get("recording_msid")
        if when is None or not isinstance(msid, str) or not msid:
            continue
        meta = raw.get("track_metadata")
        meta = meta if isinstance(meta, dict) else {}
        info = meta.get("additional_info")
        info = info if isinstance(info, dict) else {}
        mapping = meta.get("mbid_mapping")
        mapping = mapping if isinstance(mapping, dict) else {}
        found.append(
            Listen(
                listened_at=when,
                recording_msid=msid,
                recording_mbid=_mbid(mapping.get("recording_mbid"))
                or _mbid(info.get("recording_mbid")),
                release_group_mbid=_mbid(info.get("release_group_mbid"))
                or _mbid(mapping.get("release_group_mbid")),
            )
        )
    return found


def raw_listens(data: dict) -> list[dict]:
    """The listens in a response: `{"payload": {"listens": [...]}}`."""
    payload = data.get("payload") if isinstance(data, dict) else None
    entries = payload.get("listens") if isinstance(payload, dict) else None
    return [e for e in entries or [] if isinstance(e, dict)]


def _mbid(value: object) -> str | None:
    """An MBID as sent (a scrobbler's are unchecked), or None if it isn't one."""
    return value.strip().lower() if isinstance(value, str) and is_mbid(value) else None


def _timestamp(value: object) -> int | None:
    # an integer from the API (an export has floats)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")
