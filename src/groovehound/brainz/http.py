"""The HTTP layer shared by the MusicBrainz and ListenBrainz clients.

Both services are run by MetaBrainz, a small non-profit, and ask every client to make at most
one request per second and to identify itself with a contact address
(https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting,
https://listenbrainz.readthedocs.io/en/latest/users/api/index.html). So, for every request:

- a process-wide `RateLimiter` per service waits at least `MIN_INTERVAL` after each response
  before the next request goes out, whichever thread makes it (counting from the response, not
  the request, so network jitter can't make two requests arrive less than a second apart);
- the User-Agent is `Groovehound/<version> ( <contact> )`, and a client can't be built without
  a contact;
- a 503 (MusicBrainz) or 429 (ListenBrainz) backs off `BACKOFF` (15 s, then 60 s), or longer if
  the service asks (`Retry-After`, `X-RateLimit-Reset-In`), then gives up with `ServiceError`;
  a wait over `MAX_RETRY_WAIT` gives up at once rather than block a thread;
- when ListenBrainz says the window is spent (`X-RateLimit-Remaining: 0`), the limiter holds the
  next request until the window resets, before it can be refused;
- responses are requested gzip-compressed.

Callers decide what happens after a `ServiceError`: a run that fails starts the service's
`ServicePause`, and new runs (a background batch, a *Search* press) check it first, so repeated
clicks during an outage don't keep poking it. Tests never reach the network:
`tests/conftest.py` replaces `_urlopen`.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from .. import __version__

log = logging.getLogger(__name__)

MIN_INTERVAL = 1.5  # seconds from one response to the next request; the documented limit is 1/s
BACKOFF = (15.0, 60.0)  # waits after a 503 / 429 before retrying; then give up
MAX_RETRY_WAIT = 300.0  # a longer requested wait ends the call instead of blocking the thread
RETRY_STATUSES = frozenset({429, 503})
PAUSE_AFTER_ERROR = 3600.0  # a failed run stops new runs for this long (or as asked, if longer)


class ServiceError(Exception):
    """A request failed in a way worth stopping for (rate limited, network, server error).
    `retry_after`: seconds the service asked us to wait, if it said."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class NotFound(Exception):
    """The service has nothing at this path (404): an unknown or deleted MBID, a user without
    data yet."""


def user_agent(contact: str) -> str:
    """The User-Agent both services ask for: application, version, and how to reach whoever
    runs this instance."""
    return f"Groovehound/{__version__} ( {validate_contact(contact)} )"


def validate_contact(contact: str) -> str:
    """A contact is required; refuse values that would break the header (control characters,
    parentheses)."""
    contact = contact.strip()
    if not contact:
        raise ValueError(
            "a contact (email address or URL) is required to call MusicBrainz or ListenBrainz: "
            "set [brainz] contact or GROOVEHOUND_CONTACT"
        )
    if len(contact) > 200 or any(c in contact for c in "()") or not contact.isprintable():
        raise ValueError(
            "contact must be a short email address or URL on one line, "
            f"without parentheses: {contact!r}"
        )
    return contact


def requested_wait(headers: Mapping[str, str] | None, now: datetime | None = None) -> float | None:
    """How long a response asks us to wait, in seconds: `Retry-After` (delta-seconds or an HTTP
    date) or ListenBrainz's `X-RateLimit-Reset-In`, whichever is longer."""
    headers = headers or {}
    waits: list[float] = []
    retry_after = (headers.get("Retry-After") or "").strip()
    if retry_after.isdigit():
        waits.append(float(retry_after))
    elif retry_after:
        try:
            when = parsedate_to_datetime(retry_after)
            waits.append(max(0.0, (when - (now or datetime.now(UTC))).total_seconds()))
        except (TypeError, ValueError):
            pass
    reset_in = (headers.get("X-RateLimit-Reset-In") or "").strip()
    with contextlib.suppress(ValueError):
        waits.append(max(0.0, float(reset_in)))
    return max(waits) if waits else None


class RateLimiter:
    """Spaces calls across threads: `wait()` before each call, `done()` after it; the next
    call goes out at least `interval` seconds after the previous one finished."""

    def __init__(
        self,
        interval: float = MIN_INTERVAL,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if now < self._next:
                self._sleep(self._next - now)
                now = self._next
            self._next = now + self.interval

    def done(self) -> None:
        """The call finished (answered or failed): count the interval from now."""
        with self._lock:
            self._next = max(self._next, self._clock() + self.interval)

    def pause(self, seconds: float) -> None:
        """Push the next allowed call at least `seconds` into the future."""
        with self._lock:
            self._next = max(self._next, self._clock() + seconds)


class ServicePause:
    """Once a run against a service fails, new runs don't start for `PAUSE_AFTER_ERROR` (or as
    long as the service asked, if longer). One per service, shared by the whole process."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._until = 0.0

    def start(self, retry_after: float | None = None) -> None:
        with self._lock:
            self._until = self._clock() + max(PAUSE_AFTER_ERROR, retry_after or 0)

    def remaining(self) -> float:
        """Seconds until new runs may start; 0 when they may now."""
        with self._lock:
            return max(0.0, self._until - self._clock())

    def clear(self) -> None:
        with self._lock:
            self._until = 0.0


Opener = Callable[..., object]
# Looked up at request time, so tests can block real requests (tests/conftest.py).
_urlopen: Opener = urllib.request.urlopen


class JsonClient:
    """GETs JSON (or, with `get_bytes`, an image) from one service through its shared limiter.
    Subclasses add the calls."""

    service = "service"  # for messages and the cache's `service` column

    def __init__(
        self,
        *,
        api_root: str,
        limiter: RateLimiter,
        contact: str,
        timeout: float = 30,
        opener: Opener | None = None,
        backoff: tuple[float, ...] = BACKOFF,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.api_root = api_root.rstrip("/")
        self.user_agent = user_agent(contact)
        self.limiter = limiter
        self.timeout = timeout
        self._open = opener
        self.backoff = backoff
        self._headers = dict(headers or {})
        self.requests = 0  # made by this client, for logs and tests

    def get_json(self, path: str, params: Mapping[str, object] | None = None) -> dict:
        """GET `api_root + path` with query `params` (None values dropped). Raises NotFound
        for a 404, ServiceError when the caller should stop. An empty reply (a 204: nothing
        yet) is an empty dict."""
        body, _ = self._request(path, params, "application/json")
        if not body.strip():
            return {}
        try:
            return json.loads(body)
        except ValueError as exc:
            raise ServiceError(f"Unexpected reply from {self.service}: {exc}") from exc

    def get_bytes(self, path: str, accept: str) -> tuple[bytes, str]:
        """GET `api_root + path` as raw bytes, with its Content-Type. Redirects are followed
        (as for every request); errors as for `get_json`."""
        return self._request(path, None, accept)

    def _request(
        self, path: str, params: Mapping[str, object] | None, accept: str
    ) -> tuple[bytes, str]:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = self.api_root + path + (f"?{query}" if query else "")
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": accept,
                "Accept-Encoding": "gzip",
                **self._headers,
            },
        )
        for attempt in range(len(self.backoff) + 1):
            self.limiter.wait()
            self.requests += 1
            try:
                try:
                    with (self._open or _urlopen)(request, timeout=self.timeout) as response:
                        body = response.read()
                        headers = response.headers
                finally:
                    self.limiter.done()  # whatever the outcome, space from the response
                self._respect_window(headers)
                if headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body, headers.get("Content-Type") or ""
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise NotFound(path) from exc
                wait = requested_wait(exc.headers) if exc.code in RETRY_STATUSES else None
                if (
                    exc.code in RETRY_STATUSES
                    and attempt < len(self.backoff)
                    and (wait or 0) <= MAX_RETRY_WAIT
                ):
                    delay = max(self.backoff[attempt], wait or 0)
                    log.warning("%s is rate limiting us; waiting %.0fs", self.service, delay)
                    self.limiter.pause(delay)
                    continue
                if wait is not None:
                    self.limiter.pause(wait)  # in case another run starts sooner
                raise ServiceError(
                    f"{self.service} answered {exc.code} for {path}"
                    + (f" (retry after {wait:.0f}s)" if wait else ""),
                    retry_after=wait,
                ) from exc
            except (urllib.error.URLError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                raise ServiceError(f"Couldn't reach {self.service}: {reason}") from exc
            except (ValueError, EOFError) as exc:  # e.g. a broken gzip body
                raise ServiceError(f"Unexpected reply from {self.service}: {exc}") from exc
        raise AssertionError("unreachable")

    def _respect_window(self, headers: Mapping[str, str]) -> None:
        """ListenBrainz reports its rate-limit window on every response; when it's spent, hold
        the next request until it resets instead of being refused."""
        remaining = (headers.get("X-RateLimit-Remaining") or "").strip()
        if remaining == "0":
            wait = requested_wait({"X-RateLimit-Reset-In": headers.get("X-RateLimit-Reset-In")})
            if wait:
                self.limiter.pause(min(wait, MAX_RETRY_WAIT))
