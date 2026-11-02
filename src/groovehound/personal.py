"""Personal fresh releases from ListenBrainz (roadmap 3b): with a ListenBrainz username
configured, the user's own fresh-releases feed (`/1/user/<name>/fresh_releases`), which lists
releases near today by artists they listen to. The home page shows the ones by artists they
don't follow as "you might care about", each artist with *Follow*.

Like the sitewide feed (`fresh.py`), it's **one request**, cached raw for a randomized `TTL` of
about a day and fetched by the background trickle on the first wake-up after that, so the time
drifts around the clock. No username means nothing is asked. A 404 (no such user, or none of
their data yet) is cached like a response, so it isn't asked about again until the next day; an
error pauses ListenBrainz only. Nothing here touches `release_groups_seen`: releases by
followed artists come from the sitewide feed and the browses as before.

The feed lists releases, not release groups, so each release group is shown once, dated by its
earliest release in the feed. It has no artist names besides the credit (`credit_names`), and
ranks each release by how much the user listens to its artists (`confidence`): the home page
shows the best ranked. ListenBrainz data is CC0.
"""

from __future__ import annotations

import random
import re
from datetime import UTC, datetime

from . import fresh, library
from .brainz.http import ServicePause
from .brainz.listenbrainz import ListenBrainzClient
from .db import Store
from .follows import NOT_FOLLOWABLE
from .new_releases import DISMISSED_KIND
from .search import Hit

CACHE_KIND = "user-fresh-releases"
DAYS = fresh.DAYS  # days either side of today
SHOWN = 10  # suggestions on the home page

# Between the artists of a credit, as ListenBrainz writes it: "A & B", "A, B and C", "A feat. B"
_JOIN = re.compile(r",\s+|\s+(?:&|and|x|with|vs\.?|feat\.?|ft\.?|featuring)\s+", re.IGNORECASE)


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
    """Fetch the user's feed if there is a user and it's due; how many releases it lists, or
    None if nothing was asked. Raises ServiceError if ListenBrainz failed (starting the pause)
    or is paused."""
    now = now or datetime.now(UTC)
    if not user_name.strip() or not due(store, user_name, now=now):
        return None
    data = fresh.fetch_into_cache(
        store,
        pause,
        CACHE_KIND,
        _key(user_name),
        lambda: client.user_fresh_releases(user_name.strip(), days=DAYS),
        now=now,
        rng=rng,
    )
    return len(fresh.releases(data or {}))


def suggestions(store: Store, user_id: int, user_name: str, *, limit: int = SHOWN) -> list[Hit]:
    """Release groups in the user's cached feed by artists they don't follow (none of the
    credited artists), not dismissed, best ranked first (then newest), with those already in the
    library after the rest. No request."""
    if not user_name.strip():
        return []
    entry = store.cached(fresh.SERVICE, CACHE_KIND, _key(user_name))
    if entry is None or entry.data is None:
        return []
    found: dict[str, tuple[int, Hit]] = {}
    for release in fresh.releases(entry.data):
        group = release.get("release_group_mbid")
        mbids = tuple(m for m in release.get("artist_mbids") or [] if isinstance(m, str))
        if not group or not any(m not in NOT_FOLLOWABLE for m in mbids):
            continue
        confidence = release.get("confidence")
        confidence = confidence if isinstance(confidence, int) else 0
        date = release.get("release_date") or ""
        if group in found:
            best, hit = found[group]
            if date and (not hit.first_release_date or date < hit.first_release_date):
                found[group] = (max(best, confidence), _hit(release, group, mbids, date))
            else:
                found[group] = (max(best, confidence), hit)
            continue
        found[group] = (confidence, _hit(release, group, mbids, date))
    dismissed = store.dismissed(user_id, DISMISSED_KIND)
    followed = store.followed(user_id, (m for _, h in found.values() for m in h.artist_mbids))
    shown = [
        (confidence, hit)
        for confidence, hit in found.values()
        if hit.mbid not in dismissed and not followed.intersection(hit.artist_mbids)
    ]
    # best ranked first, then newest; ISO dates sort as text
    shown.sort(key=lambda s: (s[0], s[1].first_release_date), reverse=True)
    owned = store.in_library(user_id, (h.mbid for _, h in shown))
    return library.owned_last((hit for _, hit in shown), owned, lambda h: h.mbid)[:limit]


def listed(store: Store, user_name: str, release_group_mbid: str) -> bool:
    """Whether the user's cached feed lists this release group (so its cover may be fetched)."""
    if not user_name.strip():
        return False
    entry = store.cached(fresh.SERVICE, CACHE_KIND, _key(user_name))
    return entry is not None and any(
        r.get("release_group_mbid") == release_group_mbid for r in fresh.releases(entry.data or {})
    )


def credit_names(credit: str, count: int) -> tuple[str, ...]:
    """The artists' names, read from the credit: the whole credit for one artist; for several,
    the credit split at its join phrases if that gives one name each, else unknown ("")."""
    if count == 1:
        return (credit,) if credit else ("",)
    names = [n.strip() for n in _JOIN.split(credit)]
    if len(names) == count and all(names):
        return tuple(names)
    return ("",) * count


def _hit(release: dict, group: str, mbids: tuple[str, ...], date: str) -> Hit:
    credit = release.get("artist_credit_name") or ""
    return Hit(
        mbid=group,
        title=release.get("release_name") or "",
        artist_credit=credit,
        artist_mbids=mbids,
        artist_names=credit_names(credit, len(mbids)),
        primary_type=release.get("release_group_primary_type") or "",
        secondary_types=fresh.secondary_types(release.get("release_group_secondary_type")),
        first_release_date=date,
    )


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")
