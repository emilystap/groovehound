"""The album page (docs/ui-roadmap.md, "Album pages", phase U2): one release group, with its
track list (roadmap 5k), your copy, the wishlist item and the editions and store links.

**The header from what's stored**, no request: every link to the page comes from something
that knows the release group, so `header` asks each in turn: the wishlist, the cached browse of
its editions (`releases.py`, which asks for the release group with them), the browses of
artists it was seen in (followed artists, and artist pages opened), the new releases, the
ListenBrainz feeds (personal fresh releases, recommendations, the generated playlists) and the
library. A release group none of them knows (a search result, a pasted URL) needs the editions
browse first: one request, which the track list would make anyway.

**The track list** comes from two cached responses: the editions browse (which says which
editions there are, and so the default one) and one edition's lookup (`tracks.fetch`). The
edition is the one asked for (`?edition=`, one of those two know), else the library's, else the
wishlist item's chosen one, else `tracks.default_edition`. An edition in the library with a
file for every track needs neither: its files' tags give the list (`tracks.local_track_list`),
unless a lookup is cached. `track_list` reads only the cache and the library;
`fetch_track_list` makes what's missing of the two requests, for the page to load after it has
shown (explicit, like opening an artist page). Tracks ahead of time are a later trickle.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime

from . import (
    follows,
    listens,
    new_releases,
    personal,
    playlists,
    recommended,
    releases,
    tracks,
)
from .brainz.http import ServicePause
from .brainz.musicbrainz import MusicBrainzClient
from .db import AlbumPlays, LibraryRelease, Store, WishlistItem, local_date
from .search import Hit, release_group_hits


@dataclass(frozen=True)
class Copy:
    """Your copy of the album: the editions in the library, and what's known about playing it."""

    editions: tuple[LibraryRelease, ...]  # most files first
    since: str  # when the library scan first found it (UTC)
    listens: int  # all-time, from the stats (or the history, if it knows more)
    last_played: str  # the latest listen in the history (UTC), "" if none is kept
    never_played: bool  # the stats or history say so (False without stats)
    partial: tracks.Partial | None  # "8 of 12 tracks", when the browse is cached


@dataclass(frozen=True)
class Album:
    hit: Hit  # title, credit, types and date
    item: WishlistItem | None  # on the wishlist (to buy or bought)
    copy: Copy | None  # in the library
    upcoming: bool  # first released after today
    tint: str  # its cover's main colour, "" if none is cached (tint.py)


@dataclass(frozen=True)
class TrackList:
    """What the page's *Tracks* shows: the edition, and its tracks once they're cached."""

    pick: tracks.Pick | None  # None: no edition known yet (or MusicBrainz has none)
    details: str  # the edition as *Choose edition* describes it, "" if unknown
    tracks: tracks.TrackList | None  # None: not fetched yet, or the edition is gone
    due: bool  # asking MusicBrainz would get more: the page loads it after showing


def album(
    store: Store, user_id: int, user_name: str, mbid: str, *, now: datetime | None = None
) -> Album | None:
    """The album page's header and side sections, from what's stored (no request); None if
    nothing stored knows the release group (then `releases.fetch` its editions first)."""
    item = store.wishlist_item_of(user_id, mbid)
    hit = header(store, user_id, user_name, mbid, item)
    if hit is None:
        return None
    cover = store.cover(mbid)
    today = local_date((now or datetime.now(UTC)).isoformat())
    return Album(
        hit=hit,
        item=item,
        copy=copy(store, user_id, user_name, mbid),
        upcoming=hit.first_release_date > today,
        tint=cover.tint if cover else "",
    )


def header(
    store: Store, user_id: int, user_name: str, mbid: str, item: WishlistItem | None = None
) -> Hit | None:
    """The release group as the first stored source that knows it has it (see the module's
    docstring), or None."""
    if item is not None:
        return Hit(
            mbid=mbid,
            title=item.title,
            artist_credit=item.artist_credit,
            artist_mbids=item.artist_mbids,
            artist_names=item.artist_names,
            primary_type=item.primary_type,
            secondary_types=item.secondary_types,
            first_release_date=item.first_release_date,
        )
    browse = releases.cached(store, mbid)
    if browse is not None and (hit := browsed(browse.data, mbid)):
        return hit
    for artist in store.artists_seen_with(mbid):
        for kind in (follows.CACHE_KIND, follows.SINGLES_KIND):
            entry = store.cached("musicbrainz", kind, artist)
            hits = release_group_hits(entry.data or {}) if entry is not None else ()
            if hit := next((h for h in hits if h.mbid == mbid), None):
                return hit
    feed = new_releases.feed(store, user_id, user_name)
    if hit := next((r.hit for r in (*feed.upcoming, *feed.new) if r.hit.mbid == mbid), None):
        return hit
    for find in (personal.find, recommended.find, playlists.find):
        if hit := find(store, user_name, mbid):
            return hit
    editions = store.library_editions(user_id, mbid)
    if editions:
        best = editions[0]
        return Hit(
            mbid=mbid,
            title=best.title,
            artist_credit=best.artist,
            artist_mbids=best.album_artist_mbids,
            artist_names=personal.credit_names(best.artist, len(best.album_artist_mbids)),
        )
    return None


def browsed(data: dict, mbid: str) -> Hit | None:
    """The release group as an editions browse names it (asked for with `release-groups`; a
    browse cached before that has only the editions), or None."""
    for release in data.get("releases") or []:
        group = release.get("release-group") or {}
        if group.get("id") == mbid:
            return release_group_hits({"release-groups": [group]})[0]
    return None


def copy(store: Store, user_id: int, user_name: str, mbid: str) -> Copy | None:
    """Your copy of the release group, if the library has it (no request)."""
    editions = store.library_editions(user_id, mbid)
    if not editions:
        return None
    stats = store.listened(user_id, [mbid]).get(mbid, 0)
    plays = store.album_plays(user_id).get(mbid) or AlbumPlays(0, 0)
    return Copy(
        editions=tuple(editions),
        since=min(e.added_at for e in editions),
        listens=max(stats, plays.listens),
        last_played=_iso(datetime.fromtimestamp(plays.last_listened_at, UTC))
        if plays.last_listened_at
        else "",
        never_played=mbid in listens.never_played(store, user_id, user_name, [mbid]),
        partial=tracks.partial_albums(store, user_id, [mbid]).get(mbid),
    )


# --- The track list -------------------------------------------------------------------------


def track_list(
    store: Store,
    user_id: int,
    mbid: str,
    item: WishlistItem | None = None,
    edition: str | None = None,
    *,
    now: datetime | None = None,
) -> TrackList:
    """The track list as far as it's cached or in the library (no request): `due` if the
    editions browse or the edition's lookup is missing, and the files don't cover the edition.
    `edition` is shown if the library or the cached browse has it
    (anything else is ignored); `item` is the album's wishlist item, for its chosen edition."""
    browse = releases.cached(store, mbid, now=now)
    data = browse.data if browse is not None and browse.complete else None
    pick = _pick(store, user_id, mbid, item, edition, data)
    if pick is None:
        return TrackList(None, "", None, due=data is None)
    looked_up = tracks.cached(store, pick.release_mbid, now=now)
    if looked_up is not None:
        listing = tracks.track_list(store, user_id, looked_up)
    else:
        listing = tracks.local_track_list(store, user_id, pick.release_mbid)
    due = looked_up is None and listing is None
    return TrackList(pick, _details(data, pick, listing), listing, due=due)


def fetch_track_list(
    store: Store,
    client: MusicBrainzClient,
    pause: ServicePause,
    user_id: int,
    mbid: str,
    item: WishlistItem | None = None,
    edition: str | None = None,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> TrackList:
    """The track list, asking MusicBrainz for what isn't cached: the editions browse if the
    edition isn't known without it, then the edition's lookup (so at most two requests).
    Raises ServiceError as `releases.fetch` and `tracks.fetch` do."""
    found = track_list(store, user_id, mbid, item, edition, now=now)
    if not found.due:
        return found
    if found.pick is None:
        releases.fetch(store, client, pause, mbid, now=now, rng=rng)
        found = track_list(store, user_id, mbid, item, edition, now=now)
    if found.pick is not None and found.due:
        tracks.fetch(store, client, pause, found.pick.release_mbid, now=now, rng=rng)
        found = track_list(store, user_id, mbid, item, edition, now=now)
    return found


def _pick(
    store: Store,
    user_id: int,
    mbid: str,
    item: WishlistItem | None,
    edition: str | None,
    browse: dict | None,
) -> tracks.Pick | None:
    if edition:
        known = {e.release_mbid for e in store.library_editions(user_id, mbid)}
        known |= {r.get("id") for r in (browse or {}).get("releases") or []}
        if edition in known:
            return tracks.Pick(edition, "chosen")
    return tracks.pick_edition(
        store, user_id, mbid, item.release_mbid if item else None, browse=browse
    )


def edition_details(store: Store, mbid: str) -> dict[str, str]:
    """Each edition of the release group as *Choose edition* describes it ("2019-03-01 · GB ·
    CD · 12 tracks"), by release MBID, from the cached browse (none if it isn't cached)."""
    browse = releases.cached(store, mbid)
    return _edition_details(browse.data if browse is not None else None)


def _edition_details(browse: dict | None) -> dict[str, str]:
    return {m: row.details for row in releases.editions(browse or {}) for m in row.mbids}


def _details(browse: dict | None, pick: tracks.Pick, listing: tracks.TrackList | None) -> str:
    """The edition as *Choose edition* describes it, else what its track list says ("CD · 12
    tracks"), else ""."""
    if details := _edition_details(browse).get(pick.release_mbid):
        return details
    if listing is None:
        return ""
    formats = releases.formats_text([{"format": m.format} for m in listing.media])
    count = f"{listing.total} track{'s' if listing.total != 1 else ''}"
    return " · ".join(p for p in (formats, count) if p)


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")
