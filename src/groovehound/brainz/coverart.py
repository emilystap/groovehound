"""Cover Art Archive client: release group covers, for the cover cache (`covers.py`).

The archive is run by MetaBrainz with the Internet Archive and publishes no rate limit of its
own; it gets the same treatment as MusicBrainz anyway (a shared `RateLimiter`, the
User-Agent with the operator's contact, backoff), since covers are only ever fetched once per
release group and a slow first view costs little. A front image redirects twice (to
archive.org, then a mirror), which `urllib` follows within one request.
"""

from __future__ import annotations

from ..config import Settings
from .http import JsonClient, RateLimiter, ServicePause
from .musicbrainz import _mbid

API_ROOT = "https://coverartarchive.org"
COVER_SIZE = 500  # px; the archive also has 250 (linked directly by search results) and 1200

LIMITER = RateLimiter()  # shared by every client in the process
PAUSE = ServicePause()


class CoverArtClient(JsonClient):
    service = "Cover Art Archive"

    def __init__(self, *, api_root: str = API_ROOT, limiter: RateLimiter = LIMITER, **kwargs):
        super().__init__(api_root=api_root, limiter=limiter, **kwargs)

    def release_group_front(self, mbid: str, size: int = COVER_SIZE) -> tuple[bytes, str]:
        """The release group's front cover at `size` px and its Content-Type. Raises NotFound when
        it has no cover art."""
        return self.get_bytes(f"/release-group/{_mbid(mbid)}/front-{size}", "image/*")


def client_for(settings: Settings) -> CoverArtClient:
    """A client with the configured contact. Raises ValueError without one."""
    return CoverArtClient(contact=settings.contact)
