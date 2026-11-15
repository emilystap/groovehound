"""Search for something heard in the wild (roadmap 1a, read as one bar in U6): albums, the
albums a song is on, or artists.

What's typed is read first (`read`), with no request: "title by artist" is split at the last
" by " into the two fields, which ranks best; an open artist box gives the fields as typed; an
album search with neither is sent as free text, which MusicBrainz matches against title and
artist together. Songs never go as free text (it found covers and mashups, not the original:
checked 2026-11-02 on musicbrainz.org and 2026-11-14 on a mirror), so a song title alone is
searched as a title, with a hint to add the artist.

One MusicBrainz search request per Enter, never as-you-type. The raw response is cached for
`SEARCH_TTL`, so reloading the results page, going back to it, or pressing Enter again with the
same words doesn't ask again. A failed search starts `musicbrainz.PAUSE`, and while it runs only
cached results are shown.

Album and song results are release groups (the album, whatever the edition). A song search's
response says enough about each edition (date, country, formats) for *Add to wishlist* to offer
them; an album search's doesn't, so its editions are chosen later (`releases.py`). Album results
are re-ranked locally (`rerank`). Tags and ratings are CC BY-NC-SA and not used here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from .brainz.http import ServiceError, ServicePause
from .brainz.musicbrainz import MusicBrainzClient
from .db import Store
from .releases import formats_text

KINDS = {"album": "release-group", "song": "recording", "artist": "artist"}  # -> entity
SEARCH_TTL = timedelta(days=1)  # a repeated search within this reuses the cached response
SEARCH_KEEP = timedelta(days=7)  # older cached searches are dropped
MAX_TITLE = 200  # longer input is cut, so the query stays sane
BY = " by "
# Re-ranking: results scoring below this share of the best keep MusicBrainz's order, after the
# rest; the rest go by type (albums, EPs, singles, any other primary type, then secondary types).
CLOSE = 0.5
TYPE_RANK = {"Album": 0, "EP": 1, "Single": 2}
OTHER_TYPE, SECONDARY = 3, 4


@dataclass(frozen=True)
class Reading:
    """How what was typed is searched.

    `how`: "by" ("title by artist", split), "fields" (the artist box, open), "words" (an album
    search's free text), "title" (a song title alone) or "name" (an artist)."""

    kind: str
    title: str  # the title, the free text or the artist's name
    artist: str = ""
    how: str = "fields"

    @property
    def text(self) -> str:
        """What was searched, as one line ("title by artist")."""
        return f"{self.title}{BY}{self.artist}" if self.artist else self.title

    @property
    def cache_key(self) -> str:
        if self.how == "words":
            return f"words\x1f{self.title.casefold()}"
        return f"{self.title.casefold()}\x1f{self.artist.casefold()}"


def split_by(text: str) -> tuple[str, str] | None:
    """(title, artist) from "<title> by <artist>", split at the last " by " (a title can have
    one: "Stand by Me by Ben E. King"), or None."""
    title, by, artist = clean(text).rpartition(BY)
    if not by or not title.strip() or not artist.strip():
        return None
    return clean(title), clean(artist)


def read(
    kind: str, text: str, artist: str = "", *, box: bool = False, words: bool = False
) -> Reading:
    """How to search what was typed (no request). `box`: the artist box is open (its contents
    are `artist`, possibly empty); `words`: don't split at " by " (*Search the words instead*).
    Raises ValueError for an unknown kind or nothing typed."""
    if kind not in KINDS:
        raise ValueError(f"unknown search kind: {kind!r}")
    text, artist = clean(text), clean(artist)
    if not text:
        raise ValueError("type something to search for")
    if kind == "artist":
        return Reading(kind, text, how="name")
    if box:
        return Reading(kind, text, artist, how="fields")
    if not words and (found := split_by(text)):
        return Reading(kind, *found, how="by")
    return Reading(kind, text, how="words" if kind == "album" else "title")


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
    artist_names: tuple[str, ...] = ()  # in artist_mbids' order
    primary_type: str = ""
    secondary_types: tuple[str, ...] = ()
    first_release_date: str = ""
    editions: tuple[Edition, ...] = ()  # song searches only
    songs: tuple[str, ...] = ()  # song searches: the matching recordings on it
    score: int = 0  # album searches: MusicBrainz's relevance, 0-100
    release_count: int = 0  # album searches: how many editions it has

    @property
    def type_label(self) -> str:
        """e.g. "Album", "Album · Live", or "" when MusicBrainz has no type."""
        return " · ".join(t for t in (self.primary_type, *self.secondary_types) if t)


@dataclass(frozen=True)
class ArtistHit:
    """One artist in the results."""

    mbid: str
    name: str
    disambiguation: str = ""
    type: str = ""  # Person, Group, ...
    country: str = ""

    @property
    def detail(self) -> str:
        """e.g. "Group · US · virtual band"."""
        return " · ".join(p for p in (self.type, self.country, self.disambiguation) if p)


@dataclass(frozen=True)
class Found:
    """A search's results: release groups for albums and songs, artists for artists."""

    reading: Reading
    hits: list[Hit] = field(default_factory=list)
    artists: list[ArtistHit] = field(default_factory=list)


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
    reading: Reading,
    *,
    now: datetime | None = None,
) -> Found:
    """The results for `reading`: at most one request, none when the same search is cached.
    Raises ServiceError if MusicBrainz failed or is paused after a failure."""
    entity = KINDS[reading.kind]
    now = now or datetime.now(UTC)
    cache_kind = f"search-{entity}"
    key = reading.cache_key

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
            data = _ask(client, reading)
        except ServiceError as exc:
            pause.start(exc.retry_after)
            raise
        store.drop_cached_before("musicbrainz", cache_kind, _iso(now - SEARCH_KEEP))
        store.save_cached(
            "musicbrainz", cache_kind, key, data, _iso(now + SEARCH_TTL), fetched_at=_iso(now)
        )
    if entity == "artist":
        return Found(reading, artists=artist_hits(data))
    if entity == "recording":
        return Found(reading, hits=recording_hits(data))
    return Found(reading, hits=rerank(release_group_hits(data)))


def _ask(client: MusicBrainzClient, reading: Reading) -> dict:
    if reading.kind == "artist":
        return client.search_artists(reading.title)
    if reading.kind == "song":
        return client.search_recordings(reading.title, reading.artist)
    if reading.how == "words":
        return client.search_release_group_words(reading.title)
    return client.search_release_groups(reading.title, reading.artist)


def rerank(hits: list[Hit]) -> list[Hit]:
    """Album results in a better order than MusicBrainz's scores alone. Free text otherwise
    puts a remix single ahead of the album ("demon days gorillaz": 100 for the 2021 remix, 57
    for the album, tried on a mirror 2026-11-14). Those scoring at least `CLOSE` of the best
    come first: by type, then score, then how many editions it has (a stand-in for how well
    known it is); the rest after them, as MusicBrainz ranked them."""
    if not hits:
        return hits
    floor = max(h.score for h in hits) * CLOSE

    def key(indexed: tuple[int, Hit]) -> tuple[int, int, int, int, int]:
        i, h = indexed
        if h.score < floor:
            return (1, 0, 0, 0, i)
        kind = SECONDARY if h.secondary_types else TYPE_RANK.get(h.primary_type, OTHER_TYPE)
        return (0, kind, -h.score, -h.release_count, i)

    return [h for _, h in sorted(enumerate(hits), key=key)]


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")


def credit_text(credit: list[dict] | None) -> str:
    """An artist credit as MusicBrainz shows it: "Charli xcx", "A feat. B"."""
    return "".join(f"{c.get('name', '')}{c.get('joinphrase', '')}" for c in credit or []).strip()


def credit_mbids(credit: list[dict] | None) -> tuple[str, ...]:
    return tuple(c["artist"]["id"] for c in credit or [] if (c.get("artist") or {}).get("id"))


def credit_names(credit: list[dict] | None) -> tuple[str, ...]:
    """The credited artists' own names (not as credited), in `credit_mbids`' order."""
    return tuple(
        c["artist"].get("name") or c.get("name") or ""
        for c in credit or []
        if (c.get("artist") or {}).get("id")
    )


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
                artist_names=credit_names(group.get("artist-credit")),
                primary_type=group.get("primary-type") or "",
                secondary_types=tuple(group.get("secondary-types") or ()),
                first_release_date=group.get("first-release-date") or "",
                score=_int(group.get("score")),
                release_count=_int(group.get("count")),
            )
        )
    return hits


def artist_hits(data: dict) -> list[ArtistHit]:
    """Artists from an artist search, in MusicBrainz's (relevance) order."""
    return [
        ArtistHit(
            mbid=a["id"],
            name=a.get("name") or "",
            disambiguation=a.get("disambiguation") or "",
            type=a.get("type") or "",
            country=a.get("country") or "",
        )
        for a in data.get("artists") or []
        if a.get("id")
    ]


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


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
                        artist_names=credit_names(credit),
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
    parts = [
        release.get("title") if release.get("title") != group_title else "",
        release.get("date") or "",
        release.get("country") or "",
        formats_text(release.get("media")),
    ]
    return " · ".join(p for p in parts if p) or release.get("title") or release["id"]
