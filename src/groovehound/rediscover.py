"""Rediscover (roadmap 5f): one page of albums to put on next, drawn at random in fixed
proportions from what the library scan (`library.py`), the all-time stats (`listens.py`), the
listen history (`history.py`) and the new releases (`new_releases.py`) already know:

- **never played, by artists you play most** (`FAVOURITES`),
- **never played, by artists you play less** or not at all (`OTHERS`),
- **not played lately**: played, but not in the last `LATELY` (`NOT_LATELY`), and
- **new from artists you play most**: new releases by followed artists among them, not played
  yet (`NEW`), in the library or not.

An album artist's plays are the all-time listens of their release groups in the library; the
artists you play most are the top `TOP_SHARE` of those with any (at least one), Various Artists
and the like aside. An album is played if the stats or the history have a listen of it.
"Lately" needs the dates only the history has, so that section waits for its first fill; an
album the stats count but the history doesn't (played before its oldest listen) was played
before that, so not lately. Fixed proportions on purpose: no tunable weights or saved mixes.
A section with fewer albums than its share shows what it has.

A draw is an order, not a sample: each section is sorted by a hash of the draw's `seed` and the
album, and cut to size. So the same seed gives the same albums, a larger size only adds to them
(Home's shelf shows the first few of what its *See all* shows, `SEE_ALL` a section), and
dismissing one leaves the rest where they were.

**No requests**: everything comes from tables and the cache, so *Shuffle* only draws again.
Dismissed albums (`DISMISSED_KIND`, per user) stay off this page only; new releases dismissed
on the home page aren't in the feed either.
"""

from __future__ import annotations

import hashlib
import math
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from . import history, library_albums, listens, new_releases
from .db import Store
from .follows import NOT_FOLLOWABLE
from .library_albums import LATELY
from .search import Hit

FAVOURITES = 4  # albums drawn for each section
OTHERS = 3
NOT_LATELY = 3
NEW = 2
SEE_ALL = 12  # albums a section on the page: divides by 2, 3 and 4 columns, so rows end full
TOP_SHARE = 0.25  # the artists you play most: this share of the album artists played
DISMISSED_KIND = "rediscover"  # in the dismissed table

_SEED = re.compile(r"[0-9a-f]{8}")


@dataclass(frozen=True)
class Pick:
    """An album drawn for the page."""

    mbid: str  # release group
    title: str
    artist: str
    artist_mbid: str  # to link to their page ("" if none or not an artist)
    listens: int = 0  # all-time, from the stats (or the history, if it knows more)
    last_played: str = ""  # the latest listen in the history, UTC ISO ("" if none kept)
    hit: Hit | None = None  # new releases only: for *Add to wishlist*


@dataclass(frozen=True)
class Draw:
    favourites: tuple[Pick, ...] = ()
    others: tuple[Pick, ...] = ()
    not_lately: tuple[Pick, ...] = ()
    new: tuple[Pick, ...] = ()
    history_ready: bool = False  # the history's first fill is done, so "lately" is known
    history_since: str = ""  # its oldest listen, UTC ISO
    seed: str = ""  # draws these albums again

    @property
    def picks(self) -> tuple[Pick, ...]:
        return self.favourites + self.others + self.not_lately + self.new

    def __bool__(self) -> bool:
        return bool(self.picks)


def draw(
    store: Store,
    user_id: int,
    user_name: str,
    *,
    seed: str = "",
    per_section: int | None = None,
    now: datetime | None = None,
) -> Draw | None:
    """A draw: the one `seed` names, else a fresh one, with `per_section` albums a section
    instead of the fixed proportions. None while there's nothing to draw from: no ListenBrainz
    user or no stats yet (so nothing is called unplayed on a guess). No request."""
    if listens.stats(store, user_name) is None:
        return None
    now = now or datetime.now(UTC)
    seed = seed if _SEED.fullmatch(seed) else secrets.token_hex(4)
    dismissed = store.dismissed(user_id, DISMISSED_KIND)
    status = history.status(store, user_id, user_name)
    history_ready = status is not None and not status.filling

    owned = library_albums.albums(store, user_id)
    albums = {
        a.mbid: Pick(a.mbid, a.title, a.artist, a.artist_mbid, a.listens, a.last_played)
        for a in owned
    }
    artist_of = {a.mbid: a.artist_key for a in owned}
    plays = store.album_plays(user_id)

    by_artist: dict[tuple[str, ...], int] = {}
    for mbid, key in artist_of.items():
        by_artist[key] = by_artist.get(key, 0) + albums[mbid].listens
    played = sorted(
        (k for k, n in by_artist.items() if n and not set(k) & NOT_FOLLOWABLE),
        key=lambda k: -by_artist[k],
    )
    top = set(played[: math.ceil(len(played) * TOP_SHARE)])
    top_mbids = {m for k in top for m in k}

    lately = _iso(now - LATELY)
    favourites, others, not_lately = [], [], []
    for mbid, pick in albums.items():
        if mbid in dismissed:
            continue
        if not pick.listens:
            (favourites if artist_of[mbid] in top else others).append(pick)
        elif history_ready and pick.last_played < lately:
            not_lately.append(pick)

    new = []
    for release in new_releases.feed(store, user_id, now=now).new:
        hit = release.hit
        if hit.mbid in dismissed or release.artist.artist_mbid not in top_mbids:
            continue
        if (albums[hit.mbid].listens if hit.mbid in albums else 0) or hit.mbid in plays:
            continue
        new.append(
            Pick(hit.mbid, hit.title, hit.artist_credit, release.artist.artist_mbid, hit=hit)
        )

    return Draw(
        favourites=_sample(seed, favourites, per_section or FAVOURITES),
        others=_sample(seed, others, per_section or OTHERS),
        not_lately=_sample(seed, not_lately, per_section or NOT_LATELY),
        new=_sample(seed, new, per_section or NEW),
        history_ready=history_ready,
        history_since=status.since if status else "",
        seed=seed,
    )


def _sample(seed: str, picks: list[Pick], count: int) -> tuple[Pick, ...]:
    def order(pick: Pick) -> bytes:
        return hashlib.sha256(f"{seed}:{pick.mbid}".encode()).digest()

    return tuple(sorted(picks, key=order)[:count])


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")
