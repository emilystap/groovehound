"""Slow reconciliation (roadmap 2c): while the app runs, a background trickle re-browses
followed artists whose cached browse is due, so release groups MusicBrainz didn't have when
they were last browsed are seen, and become new to their followers (`follows.is_new`).

Each browse is cached for a randomized 7-14 days (`follows.TTL`), so refreshes spread out on
their own. The worker never wakes at a fixed time of day: it sleeps a random `WAKE_INTERVAL`
(drawn afresh each time, the first one too, so a restart asks nothing at once), then browses
at most `BATCH` artists, albums and EPs first, then singles for artists followed for them.
Nothing due means no requests. An error stops the batch and pauses MusicBrainz for everyone
(`ServicePause`); while paused, wake-ups ask nothing.

Browses go through `follows.refresh`, the same cache and `brainz/http.py` limiter as a click
on an artist's page, so a page opened in between counts: a fresh browse isn't made again.

The same wake-ups fetch ListenBrainz's fresh releases (`fresh.py`, roadmap 3a) when that's due,
about once a day: since the worker wakes at random, the fetch drifts around the clock. Its
errors pause ListenBrainz only, and don't stop the MusicBrainz batch. With a ListenBrainz
username configured, they fetch that user's own fresh releases the same way (`personal.py`,
roadmap 3b), and the playlists ListenBrainz created for them (`playlists.py`, roadmap 4a).
"""

from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from . import follows, fresh, personal, playlists
from .brainz import listenbrainz, musicbrainz
from .brainz.http import ServiceError, ServicePause
from .brainz.musicbrainz import MusicBrainzClient
from .config import Settings
from .db import Store

log = logging.getLogger(__name__)

WAKE_INTERVAL = (1200.0, 3600.0)  # seconds asleep between batches (20-60 min), drawn each time
# Artists browsed per wake-up: up to ~100 a day, a few requests each, against a need of one
# browse per followed artist every 7-14 days.
BATCH = 3


@dataclass(frozen=True)
class Result:
    browsed: int = 0  # browses made (an artist's albums and EPs, or their singles)
    new: int = 0  # release groups seen for the first time
    error: str = ""  # why the batch stopped early


def reconcile_once(
    store: Store,
    client: MusicBrainzClient,
    pause: ServicePause,
    *,
    batch: int = BATCH,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> Result:
    """Re-browse up to `batch` followed artists whose cached browse is due, longest due first:
    albums and EPs, then singles (for artists someone follows for them)."""
    now = now or datetime.now(UTC)
    if pause.remaining():
        return Result(error="MusicBrainz is paused after an error")
    stamp = now.astimezone(UTC).isoformat(timespec="seconds")
    due = [(follows.CACHE_KIND, m) for m in store.follows_due(follows.CACHE_KIND, stamp, batch)]
    if len(due) < batch:
        singles = store.follows_due(follows.SINGLES_KIND, stamp, batch - len(due), singles=True)
        due += [(follows.SINGLES_KIND, m) for m in singles]
    browsed = new = 0
    for kind, mbid in due:
        try:
            new += follows.refresh(store, client, pause, kind, mbid, now=now, rng=rng)
        except ServiceError as exc:
            return Result(browsed, new, str(exc))
        browsed += 1
    return Result(browsed, new)


class Reconciler:
    """The background trickle: `start()` with the app, `shutdown()` when it stops."""

    def __init__(
        self,
        settings: Settings,
        *,
        interval: tuple[float, float] = WAKE_INTERVAL,
        batch: int = BATCH,
    ) -> None:
        self.settings = settings
        self.interval = interval
        self.batch = batch
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="groovehound-reconcile", daemon=True
        )
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def run_once(self) -> Result:
        with Store(self.settings.database_path) as store:
            self._fetch_fresh(store)
            result = reconcile_once(
                store,
                musicbrainz.client_for(self.settings),
                musicbrainz.PAUSE,
                batch=self.batch,
            )
        if result.browsed or result.error:
            log.info(
                "Re-browsed %d followed artist(s), %d new release group(s)%s",
                result.browsed,
                result.new,
                f"; stopped: {result.error}" if result.error else "",
            )
        return result

    def _fetch_fresh(self, store: Store) -> None:
        client = listenbrainz.client_for(self.settings)
        try:
            new = fresh.fetch(store, client, listenbrainz.PAUSE)
            # the configured user's own, for user 1 (per-user settings aren't editable yet)
            user_name = self.settings.listenbrainz_user
            listed = personal.fetch(store, client, listenbrainz.PAUSE, user_name)
            asked = playlists.fetch(store, client, listenbrainz.PAUSE, user_name)
        except ServiceError as exc:
            log.info("ListenBrainz fresh releases not fetched: %s", exc)
            return
        if new is not None:
            log.info("Fetched ListenBrainz fresh releases, %d new release group(s)", new)
        if listed is not None:
            log.info("Fetched your ListenBrainz fresh releases, %d release(s)", listed)
        if asked is not None:
            log.info("Fetched your ListenBrainz playlists (%d request(s))", asked)

    def _loop(self) -> None:
        while not self._stop.wait(random.uniform(*self.interval)):
            try:
                self.run_once()
            except Exception:  # keep the trickle alive whatever happens
                log.exception("Background re-browse of followed artists failed")
