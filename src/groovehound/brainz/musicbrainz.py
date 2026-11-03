"""MusicBrainz web service client (https://musicbrainz.org/doc/MusicBrainz_API).

Rate limiting, the User-Agent and backoff live in `http.py`. What's specific to MusicBrainz:

- prefer lookups and browses by MBID; `search_*` is only for an explicit search someone typed
  (one request per *Search* press, never as-you-type);
- browses ask for `BROWSE_LIMIT` (100, the maximum) per page and stop after `MAX_BROWSE_PAGES`,
  so an artist with a huge discography stays cheap;
- `inc=` asks for everything a feature will need in the same request (e.g. `media labels url-rels`
  for the wishlist's editions and purchase links), so nothing has to be refetched later;
- `client_for` can point at a mirror of your own (`api_root`), which may use a shorter
  `interval`; musicbrainz.org itself never gets less than `MIN_INTERVAL`.

Tags, genres and ratings are CC BY-NC-SA (the rest is CC0): don't ask for them without
crediting MusicBrainz where they're shown.
"""

from __future__ import annotations

import re
import threading
import urllib.parse
from collections.abc import Iterable

from ..config import Settings
from .http import MIN_INTERVAL, JsonClient, NotFound, RateLimiter, ServicePause

API_ROOT = "https://musicbrainz.org/ws/2"
WEB_ROOT = "https://musicbrainz.org"  # UI links always go to musicbrainz.org, even with a mirror
BROWSE_LIMIT = 100
MAX_BROWSE_PAGES = 3
SEARCH_LIMIT = 25
# Release group types followed by default (roadmap 2b); secondary types such as live or
# compilation are filtered locally, since the browse can only filter primary types.
DEFAULT_TYPES = ("album", "ep")
_MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# Shared by every client in the process, so two jobs can never double the rate.
LIMITER = RateLimiter()
_MIRROR_LIMITERS: dict[tuple[str, float], RateLimiter] = {}  # likewise, per other server
_MIRROR_LOCK = threading.Lock()
# Started when a run fails; new runs check it first (http.py). One for MusicBrainz, mirror or not.
PAUSE = ServicePause()


def is_mbid(value: str) -> bool:
    return bool(_MBID.fullmatch(value.strip()))


def _mbid(value: str) -> str:
    if not is_mbid(value):
        raise ValueError(f"not an MBID: {value!r}")
    return value.strip().lower()


def normalize_api_root(value: str) -> str:
    """A web service root as the client wants it: http(s), no trailing slash, ending in
    /ws/2 (added when only the server is given). "" = musicbrainz.org."""
    value = value.strip().rstrip("/")
    if not value:
        return API_ROOT
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query:
        raise ValueError(
            f"musicbrainz api_root must be an http(s) URL such as http://mirror:5000: {value!r}"
        )
    return value if parts.path.endswith("/ws/2") else value + "/ws/2"


def is_public_server(api_root: str) -> bool:
    """musicbrainz.org itself (or one of its subdomains, e.g. beta, test)."""
    host = urllib.parse.urlsplit(api_root).hostname or ""
    return host == "musicbrainz.org" or host.endswith(".musicbrainz.org")


def client_for(settings: Settings) -> MusicBrainzClient:
    """The client for the configured server: musicbrainz.org (the shared `LIMITER`) unless
    `musicbrainz_api_root` names another, which gets its own shared limiter with
    `musicbrainz_interval`. Raises ValueError for a missing contact, a bad root, or an interval
    below `MIN_INTERVAL` for musicbrainz.org."""
    api_root = normalize_api_root(settings.musicbrainz_api_root)
    interval = settings.musicbrainz_interval
    if interval is not None and interval < 0:
        raise ValueError(f"musicbrainz interval can't be negative: {interval}")
    if is_public_server(api_root):
        if interval is not None and interval < MIN_INTERVAL:
            raise ValueError(
                f"musicbrainz interval can't be below {MIN_INTERVAL:g} s for musicbrainz.org "
                "(its rate limit); a shorter one is only for a server of your own (api_root)"
            )
        limiter = LIMITER if interval in (None, MIN_INTERVAL) else _limiter(api_root, interval)
    else:
        limiter = _limiter(api_root, MIN_INTERVAL if interval is None else interval)
    return MusicBrainzClient(api_root=api_root, limiter=limiter, contact=settings.contact)


def _limiter(api_root: str, interval: float) -> RateLimiter:
    with _MIRROR_LOCK:
        return _MIRROR_LIMITERS.setdefault((api_root, interval), RateLimiter(interval))


def lucene_phrase(text: str) -> str:
    """`text` as a quoted Lucene phrase, so punctuation in a title can't change the query."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def search_query(**fields: str) -> str:
    """A Lucene query matching every non-empty field as a phrase: `search_query(releasegroup=
    "Brat", artist="Charli xcx")` -> `releasegroup:"Brat" AND artist:"Charli xcx"`."""
    terms = [f"{name}:{lucene_phrase(value.strip())}" for name, value in fields.items() if value]
    if not terms:
        raise ValueError("search needs at least one non-empty field")
    return " AND ".join(terms)


class MusicBrainzClient(JsonClient):
    service = "MusicBrainz"

    def __init__(
        self, *, api_root: str = API_ROOT, limiter: RateLimiter = LIMITER, **kwargs
    ) -> None:
        super().__init__(api_root=api_root, limiter=limiter, **kwargs)

    # --- Lookups (by MBID) ----------------------------------------------------------------

    def release_group(self, mbid: str) -> dict:
        """A release group with its artist credit, releases (at most 25) and URL relationships.
        Raises NotFound, or ServiceError."""
        return self._get(f"/release-group/{_mbid(mbid)}", inc="artist-credits releases url-rels")

    def release(self, mbid: str) -> dict:
        """A release with its artist credit, release group and URL relationships (purchase and
        streaming links). Raises NotFound, or ServiceError."""
        return self._get(f"/release/{_mbid(mbid)}", inc="artist-credits release-groups url-rels")

    def release_tracks(self, mbid: str) -> dict:
        """A release with its media and every track, each with its recording and artist credit:
        a track list (roadmap 5k). A browse of the release group's releases could include
        recordings too, but MusicBrainz then cuts each page at 500 tracks, which would cost
        *Choose edition* most of its editions; so it's one lookup per edition, when asked for.
        Raises NotFound, or ServiceError."""
        return self._get(f"/release/{_mbid(mbid)}", inc="recordings artist-credits")

    def artist(self, mbid: str) -> dict:
        """An artist (name, sort name, disambiguation, type, area, life span)."""
        return self._get(f"/artist/{_mbid(mbid)}")

    # --- Browses ----------------------------------------------------------------------------

    def artist_release_groups(self, mbid: str, types: Iterable[str] = DEFAULT_TYPES) -> dict:
        """The artist's release groups of these primary types, with first release dates:
        `{"release-group-count": N, "release-groups": [...]}`, at most `MAX_BROWSE_PAGES`
        requests, so the list can be shorter than the count. Release groups made only of
        promotional, bootleg or pseudo releases are left out (`website-default`, as on
        musicbrainz.org)."""
        mbid = _mbid(mbid)
        groups: list[dict] = []
        count = 0
        for _ in range(MAX_BROWSE_PAGES):
            try:
                data = self._get(
                    "/release-group",
                    artist=mbid,
                    type="|".join(types) or None,
                    **{"release-group-status": "website-default"},
                    inc="artist-credits",
                    limit=BROWSE_LIMIT,
                    offset=len(groups),
                )
            except NotFound:
                break
            page = data.get("release-groups") or []
            groups += page
            count = int(data.get("release-group-count") or len(groups))
            if not page or len(groups) >= count:
                break
        return {"release-group-count": max(count, len(groups)), "release-groups": groups}

    def release_group_releases(self, mbid: str) -> dict:
        """The release group's releases (editions) with their media (formats, track counts),
        labels and catalogue numbers, and URL relationships, purchase and streaming links among
        them: the wishlist's *Choose edition* and *Where to buy*. `{"release-count": N,
        "releases": [...]}`. One request: the first `BROWSE_LIMIT` editions (fewer for big box
        sets, since MusicBrainz caps a page at 500 tracks) are plenty. Raises NotFound, or
        ServiceError."""
        return self._get(
            "/release",
            **{"release-group": _mbid(mbid)},
            inc="media labels url-rels",
            limit=BROWSE_LIMIT,
        )

    # --- Search (only when someone presses *Search*) -----------------------------------------

    def search_release_groups(
        self, title: str, artist: str = "", limit: int = SEARCH_LIMIT
    ) -> dict:
        """Release groups by title (and artist): `{"count": N, "release-groups": [...]}`."""
        return self._search("release-group", search_query(releasegroup=title, artist=artist), limit)

    def search_recordings(self, title: str, artist: str = "", limit: int = SEARCH_LIMIT) -> dict:
        """Recordings by title (and artist), each with the releases it's on: for "I heard this
        song", to find the album it's from."""
        return self._search("recording", search_query(recording=title, artist=artist), limit)

    def search_artists(self, name: str, limit: int = SEARCH_LIMIT) -> dict:
        return self._search("artist", search_query(artist=name), limit)

    def _search(self, entity: str, query: str, limit: int) -> dict:
        return self._get(f"/{entity}", query=query, limit=max(1, min(limit, 100)))

    def _get(self, path: str, **params: object) -> dict:
        return self.get_json(path, {**params, "fmt": "json"})
