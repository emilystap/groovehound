"""Covers ahead of time (roadmap 2e): a background trickle that fills the cover cache for the
release groups people follow artists for, so a followed artist's page, or the home page, doesn't
wait on the Cover Art Archive's limiter (one cover per 1.5 s or more) the first time it's shown.

Which covers: the release groups in each follow's cached browses (albums and EPs, and singles
for follows with singles on) of the types the follow is for (`follows.wanted`), newest first,
with no cached cover or one whose randomized `covers.COVER_TTL` has passed. Release groups of
other types are left to a page view. Nothing is browsed for this: only what 2c's trickle or a
page view already cached.

Each cover goes through `covers.cover`, the same cache and limiter as `/cover`, so a page view
in between counts; a release group in the owner's library gets its cover from the files first
(roadmap 5i), with no request. Like 2c, the worker never wakes at a fixed time of day: it sleeps
a random `WAKE_INTERVAL`, drawn afresh each time (the first one too), then makes at most `BATCH`
covers. Nothing due, or the archive paused after an error, asks nothing; an error stops the
batch (and starts the pause). It only runs with a contact configured.
"""

from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import covers, follows
from .brainz import coverart
from .brainz.coverart import CoverArtClient
from .brainz.http import ServiceError, ServicePause
from .config import Settings
from .db import OWNER_ID, Store
from .search import Hit, release_group_hits

log = logging.getLogger(__name__)

WAKE_INTERVAL = (1200.0, 3600.0)  # seconds asleep between batches (20-60 min), drawn each time
# Covers per wake-up: with 100 follows (~3,000 covers) the backlog takes a few days, then about
# one a day per new release group plus the 30-60 day refreshes.
BATCH = 10


@dataclass(frozen=True)
class Result:
    made: int = 0  # covers cached (from the library or the archive), "no cover" included
    left: int = 0  # covers still due after this batch
    error: str = ""  # why the batch stopped early


def wanted(store: Store) -> list[str]:
    """Release groups followed artists are followed for, newest first (undated last), once
    each, from the cached browses only."""
    hits: dict[str, Hit] = {}
    for follow in store.all_follows():
        kinds = [follows.CACHE_KIND]
        if follow.singles_since:
            kinds.append(follows.SINGLES_KIND)
        for kind in kinds:
            entry = store.cached("musicbrainz", kind, follow.artist_mbid)
            for hit in release_group_hits((entry.data if entry else None) or {}):
                if follows.wanted(follow.types, hit):
                    hits.setdefault(hit.mbid, hit)
    dated = sorted(hits.values(), key=lambda h: h.first_release_date, reverse=True)
    return [h.mbid for h in dated if h.first_release_date] + [
        h.mbid for h in dated if not h.first_release_date
    ]


def fetch_once(
    store: Store,
    client: CoverArtClient,
    pause: ServicePause,
    *,
    library_root: Path | None = None,
    batch: int = BATCH,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> Result:
    """Cache up to `batch` of the covers that are due, newest release groups first."""
    now = now or datetime.now(UTC)
    if pause.remaining():
        return Result(error="the Cover Art Archive is paused after an error")
    order = wanted(store)
    due_set = store.covers_due(order, now.astimezone(UTC).isoformat(timespec="seconds"))
    due = [m for m in order if m in due_set]
    made = 0
    for mbid in due[:batch]:
        if pause.remaining():  # a failure that kept a stale cover instead of raising
            return Result(made, len(due) - made, "the Cover Art Archive is paused after an error")
        try:
            covers.cover(
                store, client, pause, mbid, library_root=library_root, user_id=OWNER_ID,
                now=now, rng=rng,
            )  # fmt: skip
        except ServiceError as exc:
            return Result(made, len(due) - made, str(exc))
        made += 1
    return Result(made, len(due) - made)


class CoverFetcher:
    """The background trickle: `start()` with the app, `shutdown()` when it stops. Needs a
    contact (the Cover Art Archive client refuses to start without one)."""

    def __init__(
        self,
        settings: Settings,
        *,
        interval: tuple[float, float] = WAKE_INTERVAL,
        batch: int = BATCH,
    ) -> None:
        self.settings = settings
        self.client = coverart.client_for(settings)
        self.interval = interval
        self.batch = batch
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="groovehound-covers", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def run_once(self) -> Result:
        with Store(self.settings.database_path) as store:
            result = fetch_once(
                store,
                self.client,
                coverart.PAUSE,
                library_root=self.settings.library_path,
                batch=self.batch,
            )
        if result.made or result.error:
            log.info(
                "Cached %d cover(s) ahead of time, %d still due%s",
                result.made,
                result.left,
                f"; stopped: {result.error}" if result.error else "",
            )
        return result

    def _loop(self) -> None:
        while not self._stop.wait(random.uniform(*self.interval)):
            try:
                self.run_once()
            except Exception:  # keep the trickle alive whatever happens
                log.exception("Fetching covers ahead of time failed")
