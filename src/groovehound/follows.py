"""Following artists (roadmap 2a): follow and unfollow from search results, wishlist items and an
artist's page, which lists the albums and EPs MusicBrainz has for them.

Both come from one browse of the artist's release groups (`artist_release_groups`: albums and
EPs, official releases only, at most `MAX_BROWSE_PAGES` requests), cached for a randomized
`TTL`: opening the artist's page and then following costs one browse, not two. Every browse
records the release groups it returned in `release_groups_seen` with the date first seen, so
what was already out when someone followed is never reported as new; the slow re-browse that
finds new ones (`refresh`, roadmap 2c) runs in the background (`reconcile.py`). The artist's
name comes from the browse's artist credits, so the artist lookup is only made when there are
none (an artist with no albums or EPs). All of it is CC0.

Each follow is for some release group types (roadmap 2b, `TYPES`): albums and EPs by default,
singles if asked for, and live, compilations, remixes etc. only if asked for, since they're
noisy. Secondary types are filtered locally: the album and EP browse returns them anyway.
Singles need a browse of their own (`SINGLES_KIND`), made only for artists someone follows
with singles on; the singles already out when that was turned on count as seen, not new.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .brainz.http import NotFound, ServiceError, ServicePause
from .brainz.musicbrainz import MusicBrainzClient
from .db import Follow, Store
from .search import Hit, release_group_hits

CACHE_KIND = "artist-release-groups"  # albums and EPs
SINGLES_KIND = "artist-singles"
LOOKUP_KIND = "artist"
TTL = (timedelta(days=7), timedelta(days=14))  # a browse is refreshed at random within (2c)
LOOKUP_TTL = (timedelta(days=30), timedelta(days=60))
# MusicBrainz's special purpose artists that turn up in credits: following one would mean
# following every compilation (or every unattributed release) there is.
NOT_FOLLOWABLE = {
    "89ad4ac3-39f7-470e-963a-56509c546377",  # Various Artists
    "125ec42a-7229-4250-afc5-e057484327fe",  # [unknown]
}
# What a follow can be for, as stored -> as shown. Primary types first: a follow needs one.
PRIMARY_TYPES = {"album": "Albums", "ep": "EPs", "single": "Singles"}
# MusicBrainz's secondary types (lowercased); a release group with any secondary type not
# wanted is left out. "other" stands for the rarer ones, and any MusicBrainz adds later.
SECONDARY_TYPES = {
    "live": "Live",
    "compilation": "Compilations",
    "remix": "Remixes",
    "soundtrack": "Soundtracks",
    "dj-mix": "DJ mixes",
    "mixtape/street": "Mixtapes",
    "demo": "Demos",
    "other": "Spoken word, interviews and the like",
}
TYPES = {**PRIMARY_TYPES, **SECONDARY_TYPES}
DEFAULT_TYPES = ("album", "ep")


@dataclass(frozen=True)
class Credited:
    """An artist in a credit, as a *Follow* button shows them."""

    mbid: str
    name: str


@dataclass(frozen=True)
class Artist:
    mbid: str
    name: str
    sort_name: str = ""
    disambiguation: str = ""
    type: str = ""  # Person, Group, ...


@dataclass(frozen=True)
class Discography:
    """An artist and their albums and EPs, newest first (undated last)."""

    artist: Artist
    release_groups: tuple[Hit, ...]
    count: int  # MusicBrainz's count: more than len(release_groups) if the browse stopped early
    fetched_at: str
    singles: tuple[Hit, ...] | None = None  # None: not browsed (no one follows them for singles)

    @property
    def main(self) -> tuple[Hit, ...]:
        """Studio albums and EPs: no secondary type (live, compilation, remix, ...)."""
        return tuple(h for h in self.release_groups if not h.secondary_types)

    @property
    def other(self) -> tuple[Hit, ...]:
        return tuple(h for h in self.release_groups if h.secondary_types)


def credited(mbids: tuple[str, ...], names: tuple[str, ...], credit: str = "") -> list[Credited]:
    """The followable artists in a credit. Wishlist items saved before 2a have no names: a
    single artist is then named by the credit, several are numbered."""
    found = []
    for index, mbid in enumerate(mbids):
        if mbid in NOT_FOLLOWABLE:
            continue
        if index < len(names) and names[index]:
            name = names[index]
        else:
            name = credit if len(mbids) == 1 and credit else f"artist {index + 1}"
        found.append(Credited(mbid, name))
    return found


def wanted(types: tuple[str, ...], hit: Hit) -> bool:
    """Whether a follow for `types` is for this release group: its primary type is one of
    them, and so is each of its secondary types."""
    if hit.primary_type.lower() not in types:
        return False
    return all(_secondary(t) in types for t in hit.secondary_types)


def is_new(follow: Follow, hit: Hit, first_seen: str) -> bool:
    """Whether a release group first seen at `first_seen` is new to this follower: one they
    follow, first seen after they followed (singles: after singles were turned on)."""
    if not wanted(follow.types, hit):
        return False
    since = follow.singles_since if hit.primary_type.lower() == "single" else follow.followed_at
    return bool(since) and first_seen > since


def types_label(types: tuple[str, ...]) -> str:
    """e.g. "Albums, EPs" or "Albums, Singles + Live, Remixes"."""
    primary = ", ".join(v for k, v in PRIMARY_TYPES.items() if k in types)
    secondary = ", ".join(v for k, v in SECONDARY_TYPES.items() if k in types and k != "other")
    if "other" in types:
        secondary = ", ".join(p for p in (secondary, "more") if p)
    return f"{primary} + {secondary}" if secondary else primary


def clean_types(types: list[str]) -> tuple[str, ...]:
    """A follow's types from a form, in `TYPES`' order. Raises ValueError for an unknown type,
    or none of the primary ones (which would follow nothing)."""
    unknown = set(types) - set(TYPES)
    if unknown:
        raise ValueError(f"unknown release group type: {', '.join(sorted(unknown))}")
    if not set(types) & set(PRIMARY_TYPES):
        raise ValueError("Pick at least one of albums, EPs and singles.")
    return tuple(t for t in TYPES if t in types)


def set_types(
    store: Store,
    client: MusicBrainzClient,
    pause: ServicePause,
    user_id: int,
    mbid: str,
    types: tuple[str, ...],
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> Follow | None:
    """Change which types a follow is for (`clean_types`' output); None if not following.
    Turning singles on browses them (unless cached) first, so the ones already out are seen,
    not new. Raises ServiceError as `discography`, leaving the follow as it was."""
    current = store.follow_of(user_id, mbid)
    if current is None:
        return None
    now = now or datetime.now(UTC)
    since = current.singles_since if "single" in current.types else None
    if "single" in types and since is None:
        singles(store, client, pause, mbid, now=now, rng=rng)
        since = _iso(now)
    store.set_follow_types(user_id, mbid, types, since if "single" in types else None)
    return store.follow_of(user_id, mbid)


def singles(
    store: Store,
    client: MusicBrainzClient,
    pause: ServicePause,
    mbid: str,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> tuple[Hit, ...]:
    """The artist's singles, newest first: from the cache while it's fresh, otherwise their
    own browse. Raises ServiceError as `discography`."""
    data, _ = _browse(store, client, pause, SINGLES_KIND, mbid, ("single",), now, rng)
    return _newest_first(release_group_hits(data))


def cached_singles(store: Store, mbid: str) -> tuple[Hit, ...] | None:
    entry = store.cached("musicbrainz", SINGLES_KIND, mbid)
    return None if entry is None else _newest_first(release_group_hits(entry.data or {}))


def cached_discography(
    store: Store, mbid: str, *, now: datetime | None = None
) -> tuple[Discography | None, bool]:
    """The cached browse as a Discography (None if never browsed, or with no name to show
    without a request), and whether it's due a refresh."""
    entry = store.cached("musicbrainz", CACHE_KIND, mbid)
    if entry is None:
        return None, True
    due = entry.refresh_after <= _iso(now or datetime.now(UTC))
    artist = _artist_in_credits(entry.data or {}, mbid)
    if artist is None:
        lookup = store.cached("musicbrainz", LOOKUP_KIND, mbid)
        if lookup is None or lookup.data is None:
            return None, True
        artist = _artist(lookup.data, mbid)
    found = _discography(artist, entry.data or {}, entry.fetched_at, cached_singles(store, mbid))
    return found, due


def discography(
    store: Store,
    client: MusicBrainzClient,
    pause: ServicePause,
    mbid: str,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> Discography:
    """The artist and their albums and EPs: from the cache while it's fresh, otherwise one
    browse (a page per 100 release groups, at most `MAX_BROWSE_PAGES`), plus the artist lookup
    if the browse names no one. Raises NotFound if there's no such artist; ServiceError if
    MusicBrainz failed or is paused after a failure (starting the pause, for a new failure)."""
    now = now or datetime.now(UTC)
    data, fetched_at = _browse(store, client, pause, CACHE_KIND, mbid, DEFAULT_TYPES, now, rng)
    artist = _artist_in_credits(data, mbid) or _looked_up(store, client, pause, mbid, now, rng)
    return _discography(artist, data, fetched_at, cached_singles(store, mbid))


def refresh(
    store: Store,
    client: MusicBrainzClient,
    pause: ServicePause,
    kind: str,
    mbid: str,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> int:
    """Browse the artist's albums and EPs (`CACHE_KIND`) or singles (`SINGLES_KIND`) again if
    the cached browse is due (roadmap 2c), and how many release groups that saw for the first
    time: new to whoever follows them. Raises ServiceError as `discography`."""
    before = store.release_groups_seen(mbid)
    types = ("single",) if kind == SINGLES_KIND else DEFAULT_TYPES
    data, _ = _browse(store, client, pause, kind, mbid, types, now, rng)
    return len({g.get("id") for g in data["release-groups"]} - set(before) - {None})


def _browse(
    store: Store,
    client: MusicBrainzClient,
    pause: ServicePause,
    kind: str,
    mbid: str,
    types: tuple[str, ...],
    now: datetime | None,
    rng: random.Random | None,
) -> tuple[dict, str]:
    """One browse of the artist's release groups of these primary types, cached as `kind`
    for `TTL`, and when it was made. What it returns is recorded as seen."""
    now = now or datetime.now(UTC)
    entry = store.cached("musicbrainz", kind, mbid)
    if entry is not None and entry.refresh_after > _iso(now):
        return entry.data or {}, entry.fetched_at
    _check(pause)
    try:
        data = client.artist_release_groups(mbid, types)
    except ServiceError as exc:
        pause.start(exc.retry_after)
        raise
    fetched_at = _iso(now)
    store.save_cached(
        "musicbrainz", kind, mbid, data, _iso(now + _ttl(TTL, rng)), fetched_at=fetched_at
    )
    store.see_release_groups(
        mbid, (g["id"] for g in data["release-groups"] if g.get("id")), fetched_at
    )
    return data, fetched_at


def follow(
    store: Store,
    client: MusicBrainzClient,
    pause: ServicePause,
    user_id: int,
    mbid: str,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> Follow:
    """Follow an artist. The first time, their release groups are browsed (unless cached), so
    what's already out is recorded as seen; already following asks nothing. Raises NotFound
    for no such artist (or a special purpose one), ServiceError as `discography`."""
    if mbid in NOT_FOLLOWABLE:
        raise NotFound(f"{mbid} is a special purpose artist")
    existing = store.follow_of(user_id, mbid)
    if existing is not None:
        return existing
    artist = discography(store, client, pause, mbid, now=now, rng=rng).artist
    store.follow(
        user_id,
        Follow(
            artist_mbid=mbid,
            name=artist.name,
            sort_name=artist.sort_name,
            disambiguation=artist.disambiguation,
            followed_at=_iso(now) if now else "",
        ),
    )
    return store.follow_of(user_id, mbid)


def _looked_up(
    store: Store,
    client: MusicBrainzClient,
    pause: ServicePause,
    mbid: str,
    now: datetime,
    rng: random.Random | None,
) -> Artist:
    """The artist from the lookup (cached for `LOOKUP_TTL`). Raises NotFound, ServiceError."""
    entry = store.cached("musicbrainz", LOOKUP_KIND, mbid)
    if entry is None or entry.refresh_after <= _iso(now):
        _check(pause)
        try:
            data = client.artist(mbid)
        except NotFound:
            data = None  # remembered, so a bad link isn't asked about again soon
        except ServiceError as exc:
            pause.start(exc.retry_after)
            raise
        store.save_cached(
            "musicbrainz", LOOKUP_KIND, mbid, data, _iso(now + _ttl(LOOKUP_TTL, rng)),
            fetched_at=_iso(now),
        )  # fmt: skip
    else:
        data = entry.data
    if data is None:
        raise NotFound(f"no artist {mbid} on MusicBrainz")
    return _artist(data, mbid)


def _artist_in_credits(data: dict, mbid: str) -> Artist | None:
    for group in data.get("release-groups") or []:
        for credit in group.get("artist-credit") or []:
            artist = credit.get("artist") or {}
            if artist.get("id") == mbid and artist.get("name"):
                return _artist(artist, mbid)
    return None


def _artist(data: dict, mbid: str) -> Artist:
    return Artist(
        mbid=mbid,
        name=data.get("name") or mbid,
        sort_name=data.get("sort-name") or "",
        disambiguation=data.get("disambiguation") or "",
        type=data.get("type") or "",
    )


def _discography(
    artist: Artist, data: dict, fetched_at: str, singles: tuple[Hit, ...] | None
) -> Discography:
    hits = release_group_hits(data)
    count = max(int(data.get("release-group-count") or 0), len(hits))
    return Discography(artist, _newest_first(hits), count, fetched_at, singles)


def _newest_first(hits: list[Hit]) -> tuple[Hit, ...]:
    """Newest first, undated last; ISO dates (YYYY, YYYY-MM, YYYY-MM-DD) sort as text."""
    dated = sorted((h for h in hits if h.first_release_date), key=lambda h: h.first_release_date)
    return (*reversed(dated), *(h for h in hits if not h.first_release_date))


def _secondary(name: str) -> str:
    """A MusicBrainz secondary type as `SECONDARY_TYPES` has it ("other" for the rest)."""
    name = name.lower()
    return name if name in SECONDARY_TYPES else "other"


def _check(pause: ServicePause) -> None:
    waiting = pause.remaining()
    if waiting:
        raise ServiceError(
            f"MusicBrainz is paused after an error; try again in {math.ceil(waiting / 60)} min."
        )


def _ttl(bounds: tuple[timedelta, timedelta], rng: random.Random | None) -> timedelta:
    low, high = bounds
    return low + (high - low) * (rng or random).random()


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")
