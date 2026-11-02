"""ListenBrainz API client (https://listenbrainz.readthedocs.io/en/latest/users/api/).

Rate limiting, the User-Agent, backoff and the `X-RateLimit-*` window live in `http.py`. What's
specific to ListenBrainz:

- the user token is optional for everything used here, sent as `Authorization: Token <token>`
  only when configured, and never logged (it's kept out of `repr`);
- recommendations and generated playlists change daily or weekly, so callers fetch them at most
  once a day (roadmap 3 and 4);
- never page through a user's whole history with `/listens` (the docs ask clients not to); this
  client deliberately has no such call;
- `labs.api.listenbrainz.org` has no published limits or stability promise, so it isn't used.
"""

from __future__ import annotations

import urllib.parse
from datetime import date

from ..config import Settings
from .http import JsonClient, RateLimiter, ServicePause

API_ROOT = "https://api.listenbrainz.org"
WEB_ROOT = "https://listenbrainz.org"
MAX_FRESH_DAYS = 90  # the API's maximum window
MAX_RECORDINGS = 100  # recordings per metadata request (a generated playlist has 50), URL length

# Shared by every client in the process.
LIMITER = RateLimiter()
PAUSE = ServicePause()  # started when a run fails; new runs check it first (http.py)


def client_for(settings: Settings) -> ListenBrainzClient:
    """Raises ValueError without a contact."""
    return ListenBrainzClient(contact=settings.contact, token=settings.listenbrainz_token)


def _user(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("a ListenBrainz username is required")
    return urllib.parse.quote(name, safe="")


class ListenBrainzClient(JsonClient):
    service = "ListenBrainz"

    def __init__(
        self,
        *,
        api_root: str = API_ROOT,
        limiter: RateLimiter = LIMITER,
        token: str = "",
        **kwargs,
    ) -> None:
        headers = {"Authorization": f"Token {token.strip()}"} if token.strip() else None
        super().__init__(api_root=api_root, limiter=limiter, headers=headers, **kwargs)

    def __repr__(self) -> str:  # never show the token
        return f"ListenBrainzClient(api_root={self.api_root!r})"

    def fresh_releases(self, *, days: int = 14, around: date | None = None) -> dict:
        """Every release with a release date within `days` of `around` (default today), past
        and future, sitewide: one request covers all followed artists (roadmap 3a)."""
        return self.get_json(
            "/1/explore/fresh-releases/",
            {
                "days": max(1, min(days, MAX_FRESH_DAYS)),
                "release_date": around.isoformat() if around else None,
            },
        )

    def user_fresh_releases(self, user: str, *, days: int = 14) -> dict:
        """Fresh releases from artists this user listens to (roadmap 3b)."""
        return self.get_json(
            f"/1/user/{_user(user)}/fresh_releases", {"days": max(1, min(days, MAX_FRESH_DAYS))}
        )

    def playlists_created_for(self, user: str, *, count: int = 25) -> dict:
        """Playlists ListenBrainz generated for this user (Weekly Exploration etc.), without
        their tracks; fetch one with `playlist` (roadmap 4a)."""
        return self.get_json(f"/1/user/{_user(user)}/playlists/createdfor", {"count": count})

    def playlist(self, mbid: str) -> dict:
        """A playlist as JSPF, with its recordings."""
        return self.get_json(f"/1/playlist/{urllib.parse.quote(mbid.strip(), safe='')}")

    def similar_artists(self, mbid: str, *, count: int = 20) -> dict:
        """Artists similar to this one, from LB Radio's artist endpoint (roadmap 4b): keyed by
        artist MBID (this one's too, unordered), each with one of their recordings and their
        name. One recording each is the fewest it allows; the closest ("easy") artists only."""
        return self.get_json(
            f"/1/lb-radio/artist/{urllib.parse.quote(mbid.strip(), safe='')}",
            {
                "mode": "easy",
                "max_similar_artists": count,
                "max_recordings_per_artist": 1,
                "pop_begin": 0,
                "pop_end": 100,
            },
        )

    def recording_metadata(self, mbids: list[str]) -> dict:
        """Each recording's release and release group (`inc=release`), keyed by recording MBID:
        one request for a whole playlist, which names only the album (roadmap 4a)."""
        return self.get_json(
            "/1/metadata/recording/",
            {
                "recording_mbids": ",".join(m.strip() for m in mbids[:MAX_RECORDINGS]),
                "inc": "release",
            },
        )
