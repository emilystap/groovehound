"""SQLite persistence: users, their wishlists and follows, and the shared caches of MusicBrainz /
ListenBrainz responses and cover thumbnails.

Plain `sqlite3` keeps the dependency list short. Schema changes go in MIGRATIONS (append
only); `PRAGMA user_version` records which have run.

Multi-user ready: anything a user owns (wishlist items, follows, dismissed suggestions) gets a
`user_id` column referencing `users`. Caches of API responses are shared and have none, so one
artist followed by two users is still fetched once. Today there is one user, id `OWNER_ID`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

OWNER_ID = 1  # the single user until accounts exist (roadmap: "Users")

MIGRATIONS: list[str] = [
    # 1: users, and a cache of raw API responses
    """
    CREATE TABLE users (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL UNIQUE,
        created_at  TEXT NOT NULL
    );
    INSERT INTO users (id, name, created_at)
        VALUES (1, 'owner', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));

    -- One row per response: service = musicbrainz | listenbrainz, kind = what was asked
    -- (e.g. release-group, artist-release-groups, fresh-releases), key = MBID or parameters.
    -- data is the zlib-compressed JSON, NULL = the service had nothing (404).
    -- refresh_after is randomized per row so refreshes trickle in instead of bunching up.
    CREATE TABLE api_cache (
        service       TEXT NOT NULL,
        kind          TEXT NOT NULL,
        key           TEXT NOT NULL,
        data          BLOB,
        fetched_at    TEXT NOT NULL,
        refresh_after TEXT NOT NULL,
        PRIMARY KEY (service, kind, key)
    );
    CREATE INDEX api_cache_due ON api_cache(refresh_after);
    """,
    # 2: the wishlist (roadmap 1a). Title, credit, types and date are a snapshot from the search
    # result (CC0 data), so the list renders without a request; release_mbid is an optional
    # specific edition. artist_mbids and secondary_types are JSON lists.
    """
    CREATE TABLE wishlist (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        release_group_mbid  TEXT NOT NULL,
        release_mbid        TEXT,
        title               TEXT NOT NULL,
        artist_credit       TEXT NOT NULL,
        artist_mbids        TEXT NOT NULL DEFAULT '[]',
        primary_type        TEXT NOT NULL DEFAULT '',
        secondary_types     TEXT NOT NULL DEFAULT '[]',
        first_release_date  TEXT NOT NULL DEFAULT '',
        added_at            TEXT NOT NULL,
        UNIQUE (user_id, release_group_mbid)
    );
    CREATE INDEX wishlist_added ON wishlist(user_id, added_at);
    """,
    # 3: the wishlist page (roadmap 1b): the user's own notes, where they heard it, and when they
    # bought it (NULL = not yet; bought items are kept but hidden by default).
    """
    ALTER TABLE wishlist ADD COLUMN notes TEXT NOT NULL DEFAULT '';
    ALTER TABLE wishlist ADD COLUMN heard TEXT NOT NULL DEFAULT '';
    ALTER TABLE wishlist ADD COLUMN bought_at TEXT;
    """,
    # 4: quick capture (roadmap 1d): free text saved when there's no time to search, looked up
    # later. Filing one on a wishlist item moves its text into the item's notes.
    """
    CREATE TABLE captures (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        text         TEXT NOT NULL,
        captured_at  TEXT NOT NULL
    );
    CREATE INDEX captures_added ON captures(user_id, captured_at);
    """,
    # 5: cover thumbnails from the Cover Art Archive (covers.py), shared like api_cache. image
    # NULL = the release group has no cover art. refresh_after is randomized per row.
    """
    CREATE TABLE covers (
        release_group_mbid  TEXT PRIMARY KEY,
        image               BLOB,
        content_type        TEXT NOT NULL DEFAULT '',
        fetched_at          TEXT NOT NULL,
        refresh_after       TEXT NOT NULL
    );
    """,
    # 6: following artists (roadmap 2a). follows is the user's, with the artist's name as of
    # following (CC0), so the list renders without a request. release_groups_seen is shared,
    # like the caches: every release group an artist browse has returned, with when it was
    # first seen, so a release group is new to a follower only if first seen after following.
    # wishlist.artist_names: the credited artists' names, in artist_mbids' order (JSON list).
    """
    CREATE TABLE follows (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        artist_mbid     TEXT NOT NULL,
        name            TEXT NOT NULL,
        sort_name       TEXT NOT NULL DEFAULT '',
        disambiguation  TEXT NOT NULL DEFAULT '',
        followed_at     TEXT NOT NULL,
        UNIQUE (user_id, artist_mbid)
    );
    CREATE TABLE release_groups_seen (
        artist_mbid         TEXT NOT NULL,
        release_group_mbid  TEXT NOT NULL,
        first_seen          TEXT NOT NULL,
        PRIMARY KEY (artist_mbid, release_group_mbid)
    );
    ALTER TABLE wishlist ADD COLUMN artist_names TEXT NOT NULL DEFAULT '[]';
    """,
    # 7: release group types per follow (roadmap 2b). types is a JSON list of primary types
    # (album, ep, single) and the secondary ones also wanted (live, compilation, ...; see
    # follows.TYPES). singles_since: when singles were last turned on (NULL = off), since
    # singles are browsed only from then: older ones were already out, not new.
    """
    ALTER TABLE follows ADD COLUMN types TEXT NOT NULL DEFAULT '["album", "ep"]';
    ALTER TABLE follows ADD COLUMN singles_since TEXT;
    """,
    # 8: dismissed (roadmap 2d): what the user doesn't want shown again. kind = what the mbid
    # is: release-group (a new release on the home page); suggestions later (phase 4).
    """
    CREATE TABLE dismissed (
        user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind          TEXT NOT NULL,
        mbid          TEXT NOT NULL,
        dismissed_at  TEXT NOT NULL,
        PRIMARY KEY (user_id, kind, mbid)
    );
    """,
    # 9: the library scan (roadmap 5a). library_files: every audio file under the user's library
    # folder (path relative to it, "/"-separated), with its mtime and size (a rescan re-reads
    # only files whose either changed) and what its MusicBrainz tags say; release_mbid NULL =
    # untagged, or unreadable (problem says why). library: one row per release, rebuilt from
    # library_files after each scan; added_at = when it was first found. library_scans: each
    # user's latest scan, for the Library page (finished_at NULL while it runs, or if the app
    # stopped during it).
    """
    CREATE TABLE library_files (
        user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        path                TEXT NOT NULL,
        mtime_ns            INTEGER NOT NULL,
        size                INTEGER NOT NULL,
        release_mbid        TEXT,
        release_group_mbid  TEXT,
        album_artist_mbids  TEXT NOT NULL DEFAULT '[]',
        album               TEXT NOT NULL DEFAULT '',
        album_artist        TEXT NOT NULL DEFAULT '',
        problem             TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (user_id, path)
    );
    CREATE TABLE library (
        user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        release_mbid        TEXT NOT NULL,
        release_group_mbid  TEXT,
        album_artist_mbids  TEXT NOT NULL DEFAULT '[]',
        title               TEXT NOT NULL DEFAULT '',
        artist              TEXT NOT NULL DEFAULT '',
        folder              TEXT NOT NULL DEFAULT '',
        files               INTEGER NOT NULL,
        added_at            TEXT NOT NULL,
        last_seen           TEXT NOT NULL,
        PRIMARY KEY (user_id, release_mbid)
    );
    CREATE INDEX library_release_group ON library(user_id, release_group_mbid);
    CREATE TABLE library_scans (
        user_id      INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        root         TEXT NOT NULL,
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        files        INTEGER NOT NULL DEFAULT 0,
        read         INTEGER NOT NULL DEFAULT 0,
        left_over    INTEGER NOT NULL DEFAULT 0,
        error        TEXT NOT NULL DEFAULT ''
    );
    """,
    # 10: covers from the library (roadmap 5i). source: the file a cover was made from, relative
    # to the owner's library folder (an image beside the music, or an audio file with the art
    # embedded); '' = the Cover Art Archive. source_mtime_ns: that file's mtime then, so a
    # changed or removed file makes the cover again.
    """
    ALTER TABLE covers ADD COLUMN source TEXT NOT NULL DEFAULT '';
    ALTER TABLE covers ADD COLUMN source_mtime_ns INTEGER NOT NULL DEFAULT 0;
    """,
]


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def local_zone() -> tzinfo:
    """The time zone dates are shown in: `TZ` (an IANA name such as America/New_York, as Docker
    images take it), else the system's own. Timestamps are always stored in UTC."""
    name = os.environ.get("TZ", "").lstrip(":")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass  # not an IANA name (e.g. a POSIX rule string): fall back to the system's
    return datetime.now().astimezone().tzinfo


def local_date(timestamp: str | None) -> str:
    """A stored UTC timestamp's date in the local time zone, e.g. "2026-11-01"; "" for none."""
    if not timestamp:
        return ""
    return datetime.fromisoformat(timestamp).astimezone(local_zone()).date().isoformat()


def local_time(timestamp: str | None) -> str:
    """A stored UTC timestamp in the local time zone, to the minute: "2026-11-01 14:05"."""
    if not timestamp:
        return ""
    return datetime.fromisoformat(timestamp).astimezone(local_zone()).strftime("%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class User:
    id: int
    name: str


@dataclass(frozen=True)
class WishlistItem:
    release_group_mbid: str
    title: str
    artist_credit: str
    artist_mbids: tuple[str, ...] = ()
    artist_names: tuple[str, ...] = ()  # in artist_mbids' order; () for items saved before 2a
    primary_type: str = ""
    secondary_types: tuple[str, ...] = ()
    first_release_date: str = ""
    release_mbid: str | None = None  # a specific edition, when one was chosen
    added_at: str = ""
    notes: str = ""
    heard: str = ""  # where the user heard it (free text)
    bought_at: str | None = None
    id: int | None = None  # set once saved

    @property
    def type_label(self) -> str:
        """e.g. "Album", "Album · Live", or "" when MusicBrainz has no type."""
        return " · ".join(t for t in (self.primary_type, *self.secondary_types) if t)


@dataclass(frozen=True)
class Follow:
    """An artist the user follows (roadmap 2a)."""

    artist_mbid: str
    name: str
    sort_name: str = ""
    disambiguation: str = ""  # e.g. "UK singer", to tell artists with one name apart
    followed_at: str = ""
    types: tuple[str, ...] = ("album", "ep")  # release group types followed (follows.TYPES)
    singles_since: str | None = None  # when singles were turned on; None = off


@dataclass(frozen=True)
class Capture:
    """Something heard, written down to look up later (roadmap 1d)."""

    id: int
    text: str
    captured_at: str


@dataclass(frozen=True)
class LibraryFile:
    """An audio file in the user's library folder, and what its tags say (roadmap 5a)."""

    path: str  # relative to the library folder, "/"-separated
    mtime_ns: int
    size: int
    release_mbid: str | None = None  # None: untagged, or unreadable (see problem)
    release_group_mbid: str | None = None
    album_artist_mbids: tuple[str, ...] = ()
    album: str = ""
    album_artist: str = ""
    problem: str = ""  # why it couldn't be read, if it couldn't


@dataclass(frozen=True)
class LibraryRelease:
    """A release in the user's library, from its files' tags (roadmap 5a)."""

    release_mbid: str
    release_group_mbid: str | None  # None: the files have an album id but no release group id
    album_artist_mbids: tuple[str, ...]
    title: str
    artist: str
    folder: str  # the folder holding all its files, relative to the library folder
    files: int
    added_at: str = ""  # when it was first found
    last_seen: str = ""


@dataclass(frozen=True)
class LibraryScan:
    """The user's latest library scan."""

    root: str
    started_at: str
    finished_at: str | None  # None: still running, or the app stopped during it
    files: int  # audio files found (so far, while it runs)
    read: int  # files read
    left_over: int  # changed files left for the next scan
    error: str


@dataclass(frozen=True)
class LibrarySummary:
    releases: int = 0
    release_groups: int = 0
    without_release_group: int = 0  # releases whose files have no release group id
    files: int = 0
    untagged: int = 0  # files without an album id
    unreadable: int = 0


@dataclass(frozen=True)
class CacheEntry:
    data: dict | None  # None = the service had nothing for this key
    fetched_at: str
    refresh_after: str


@dataclass(frozen=True)
class CachedCover:
    image: bytes | None  # None: the release group has no cover art
    content_type: str
    fetched_at: str
    refresh_after: str
    source: str = ""  # the library file it was made from; "" = the Cover Art Archive
    source_mtime_ns: int = 0


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the web app opens one Store per request, and FastAPI may run
        # a request's dependency and handler on different threadpool threads.
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for index, script in enumerate(MIGRATIONS[version:], start=version + 1):
            with self.conn:
                self.conn.executescript(script)
                self.conn.execute(f"PRAGMA user_version = {index}")

    # --- Users ----------------------------------------------------------------------------

    def user(self, user_id: int) -> User | None:
        row = self.conn.execute("SELECT id, name FROM users WHERE id = ?", (user_id,)).fetchone()
        return User(row["id"], row["name"]) if row else None

    # --- Wishlist -------------------------------------------------------------------------

    def add_to_wishlist(self, user_id: int, item: WishlistItem) -> bool:
        """Save `item` for this user; False if the release group was already on the list (left
        as it was)."""
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO wishlist (user_id, release_group_mbid, release_mbid, title, "
                "artist_credit, artist_mbids, artist_names, primary_type, secondary_types, "
                "first_release_date, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (user_id, release_group_mbid) DO NOTHING",
                (
                    user_id,
                    item.release_group_mbid,
                    item.release_mbid,
                    item.title,
                    item.artist_credit,
                    json.dumps(list(item.artist_mbids)),
                    json.dumps(list(item.artist_names)),
                    item.primary_type,
                    json.dumps(list(item.secondary_types)),
                    item.first_release_date,
                    item.added_at or utcnow(),
                ),
            )
        return cursor.rowcount == 1

    def wishlist(
        self, user_id: int, limit: int | None = None, *, bought: bool = False
    ) -> list[WishlistItem]:
        """This user's wishlist, most recently added first: items still to buy, or with
        `bought`, only the ones already bought."""
        rows = self.conn.execute(
            "SELECT * FROM wishlist WHERE user_id = ? AND (bought_at IS NOT NULL) = ? "
            "ORDER BY added_at DESC, id DESC LIMIT ?",
            (user_id, bought, -1 if limit is None else limit),
        )
        return [_wishlist_item(r) for r in rows]

    def wishlist_item(self, user_id: int, item_id: int) -> WishlistItem | None:
        row = self.conn.execute(
            "SELECT * FROM wishlist WHERE user_id = ? AND id = ?", (user_id, item_id)
        ).fetchone()
        return _wishlist_item(row) if row else None

    def wishlist_count(self, user_id: int, *, bought: bool = False) -> int:
        """How many items are still to buy (or, with `bought`, already bought)."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM wishlist WHERE user_id = ? AND (bought_at IS NOT NULL) = ?",
            (user_id, bought),
        ).fetchone()[0]

    def annotate_wishlist_item(self, user_id: int, item_id: int, notes: str, heard: str) -> bool:
        """Replace an item's notes and "where I heard it"; False if it isn't this user's."""
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE wishlist SET notes = ?, heard = ? WHERE user_id = ? AND id = ?",
                (notes, heard, user_id, item_id),
            )
        return cursor.rowcount == 1

    def choose_edition(self, user_id: int, item_id: int, release_mbid: str | None) -> bool:
        """Save which edition (release) an item is for; None = any edition. False if it isn't
        this user's."""
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE wishlist SET release_mbid = ? WHERE user_id = ? AND id = ?",
                (release_mbid, user_id, item_id),
            )
        return cursor.rowcount == 1

    def mark_bought(self, user_id: int, item_id: int, bought: bool = True) -> bool:
        """Mark an item bought (kept, but hidden by default) or back to still wanted. Marking
        an item already bought keeps its original date. False if it isn't this user's."""
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE wishlist SET bought_at = CASE WHEN ? THEN COALESCE(bought_at, ?) END "
                "WHERE user_id = ? AND id = ?",
                (bought, utcnow(), user_id, item_id),
            )
        return cursor.rowcount == 1

    def remove_from_wishlist(self, user_id: int, item_id: int) -> bool:
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM wishlist WHERE user_id = ? AND id = ?", (user_id, item_id)
            )
        return cursor.rowcount == 1

    def wishlisted(self, user_id: int, release_group_mbids: Iterable[str]) -> set[str]:
        """Which of these release groups are on this user's wishlist."""
        mbids = list(set(release_group_mbids))
        if not mbids:
            return set()
        rows = self.conn.execute(
            "SELECT release_group_mbid FROM wishlist WHERE user_id = ? AND release_group_mbid "
            f"IN ({', '.join('?' * len(mbids))})",
            (user_id, *mbids),
        )
        return {r[0] for r in rows}

    # --- Follows --------------------------------------------------------------------------

    def follow(self, user_id: int, follow: Follow) -> bool:
        """Follow an artist; False if this user already did (left as it was, date and all)."""
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO follows (user_id, artist_mbid, name, sort_name, disambiguation, "
                "followed_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (user_id, artist_mbid) DO NOTHING",
                (
                    user_id,
                    follow.artist_mbid,
                    follow.name,
                    follow.sort_name,
                    follow.disambiguation,
                    follow.followed_at or utcnow(),
                ),
            )
        return cursor.rowcount == 1

    def set_follow_types(
        self, user_id: int, artist_mbid: str, types: Iterable[str], singles_since: str | None
    ) -> bool:
        """Change which release group types a follow is for; False if not following."""
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE follows SET types = ?, singles_since = ? "
                "WHERE user_id = ? AND artist_mbid = ?",
                (json.dumps(list(types)), singles_since, user_id, artist_mbid),
            )
        return cursor.rowcount == 1

    def unfollow(self, user_id: int, artist_mbid: str) -> bool:
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM follows WHERE user_id = ? AND artist_mbid = ?", (user_id, artist_mbid)
            )
        return cursor.rowcount == 1

    def follows(self, user_id: int) -> list[Follow]:
        """The artists this user follows, by sort name (as MusicBrainz sorts artists)."""
        rows = self.conn.execute(
            "SELECT * FROM follows WHERE user_id = ? "
            "ORDER BY COALESCE(NULLIF(sort_name, ''), name) COLLATE NOCASE, id",
            (user_id,),
        )
        return [_follow(r) for r in rows]

    def follow_of(self, user_id: int, artist_mbid: str) -> Follow | None:
        row = self.conn.execute(
            "SELECT * FROM follows WHERE user_id = ? AND artist_mbid = ?", (user_id, artist_mbid)
        ).fetchone()
        return _follow(row) if row else None

    def followed(self, user_id: int, artist_mbids: Iterable[str]) -> set[str]:
        """Which of these artists this user follows."""
        mbids = list(set(artist_mbids))
        if not mbids:
            return set()
        rows = self.conn.execute(
            "SELECT artist_mbid FROM follows WHERE user_id = ? AND artist_mbid "
            f"IN ({', '.join('?' * len(mbids))})",
            (user_id, *mbids),
        )
        return {r[0] for r in rows}

    def followed_artists(self) -> set[str]:
        """Every artist someone follows."""
        return {r[0] for r in self.conn.execute("SELECT DISTINCT artist_mbid FROM follows")}

    def see_release_groups(
        self, artist_mbid: str, release_group_mbids: Iterable[str], when: str | None = None
    ) -> None:
        """Remember that an artist browse returned these release groups; ones seen before
        keep the date they were first seen."""
        when = when or utcnow()
        with self.conn:
            self.conn.executemany(
                "INSERT INTO release_groups_seen (artist_mbid, release_group_mbid, first_seen) "
                "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                ((artist_mbid, mbid, when) for mbid in dict.fromkeys(release_group_mbids)),
            )

    def follows_due(self, kind: str, now: str, limit: int, *, singles: bool = False) -> list[str]:
        """Artists someone follows (with singles on, for `singles`) whose cached browse of
        `kind` is due a refresh, or missing: longest due first, missing ones before them."""
        rows = self.conn.execute(
            "SELECT f.artist_mbid FROM (SELECT DISTINCT artist_mbid FROM follows"
            f"{' WHERE singles_since IS NOT NULL' if singles else ''}) f "
            "LEFT JOIN api_cache c ON c.service = 'musicbrainz' AND c.kind = ? "
            "AND c.key = f.artist_mbid "
            "WHERE c.refresh_after IS NULL OR c.refresh_after <= ? "
            "ORDER BY COALESCE(c.refresh_after, ''), f.artist_mbid LIMIT ?",
            (kind, now, limit),
        )
        return [r[0] for r in rows]

    def release_groups_seen(self, artist_mbid: str) -> dict[str, str]:
        """Release group MBID -> when it was first seen, for one artist."""
        rows = self.conn.execute(
            "SELECT release_group_mbid, first_seen FROM release_groups_seen WHERE artist_mbid = ?",
            (artist_mbid,),
        )
        return {r[0]: r[1] for r in rows}

    def followed_release_group(self, user_id: int, release_group_mbid: str) -> bool:
        """Whether a browse of an artist this user follows has returned this release group."""
        row = self.conn.execute(
            "SELECT 1 FROM release_groups_seen s JOIN follows f ON f.artist_mbid = s.artist_mbid "
            "WHERE f.user_id = ? AND s.release_group_mbid = ? LIMIT 1",
            (user_id, release_group_mbid),
        ).fetchone()
        return row is not None

    # --- Dismissed ------------------------------------------------------------------------

    def dismiss(self, user_id: int, kind: str, mbid: str) -> bool:
        """Don't show this again; False if it was already dismissed (keeping the date)."""
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO dismissed (user_id, kind, mbid, dismissed_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                (user_id, kind, mbid, utcnow()),
            )
        return cursor.rowcount == 1

    def dismissed(self, user_id: int, kind: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT mbid FROM dismissed WHERE user_id = ? AND kind = ?", (user_id, kind)
        )
        return {r[0] for r in rows}

    # --- Quick capture --------------------------------------------------------------------

    def add_capture(self, user_id: int, text: str) -> int:
        """Save free text to look up later; its id."""
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO captures (user_id, text, captured_at) VALUES (?, ?, ?)",
                (user_id, text, utcnow()),
            )
        return cursor.lastrowid

    def captures(self, user_id: int) -> list[Capture]:
        """This user's captures, most recent first."""
        rows = self.conn.execute(
            "SELECT id, text, captured_at FROM captures WHERE user_id = ? "
            "ORDER BY captured_at DESC, id DESC",
            (user_id,),
        )
        return [Capture(r["id"], r["text"], r["captured_at"]) for r in rows]

    def capture(self, user_id: int, capture_id: int) -> Capture | None:
        row = self.conn.execute(
            "SELECT id, text, captured_at FROM captures WHERE user_id = ? AND id = ?",
            (user_id, capture_id),
        ).fetchone()
        return Capture(row["id"], row["text"], row["captured_at"]) if row else None

    def remove_capture(self, user_id: int, capture_id: int) -> bool:
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM captures WHERE user_id = ? AND id = ?", (user_id, capture_id)
            )
        return cursor.rowcount == 1

    def file_capture(self, user_id: int, capture_id: int, release_group_mbid: str) -> bool:
        """Turn a capture into part of a wishlist item: its text (with the date it was written
        down) goes at the end of the item's notes and the capture is gone. False, changing
        nothing, if either isn't this user's."""
        capture = self.capture(user_id, capture_id)
        if capture is None:
            return False
        note = f"{capture.text} ({local_date(capture.captured_at)})"
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE wishlist SET notes = CASE WHEN notes = '' THEN ? "
                "ELSE notes || char(10) || ? END WHERE user_id = ? AND release_group_mbid = ?",
                (note, note, user_id, release_group_mbid),
            )
            if cursor.rowcount != 1:
                return False
            self.conn.execute(
                "DELETE FROM captures WHERE user_id = ? AND id = ?", (user_id, capture_id)
            )
        return True

    # --- Library --------------------------------------------------------------------------

    def library_index(self, user_id: int) -> dict[str, tuple[int, int]]:
        """Path -> (mtime_ns, size) of every file the last scans read for this user."""
        rows = self.conn.execute(
            "SELECT path, mtime_ns, size FROM library_files WHERE user_id = ?", (user_id,)
        )
        return {r[0]: (r[1], r[2]) for r in rows}

    def save_library_files(self, user_id: int, files: Iterable[LibraryFile]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO library_files (user_id, path, mtime_ns, size, "
                "release_mbid, release_group_mbid, album_artist_mbids, album, album_artist, "
                "problem) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        user_id,
                        f.path,
                        f.mtime_ns,
                        f.size,
                        f.release_mbid,
                        f.release_group_mbid,
                        json.dumps(list(f.album_artist_mbids)),
                        f.album,
                        f.album_artist,
                        f.problem,
                    )
                    for f in files
                ),
            )

    def forget_library_files(self, user_id: int, paths: Iterable[str]) -> None:
        with self.conn:
            self.conn.executemany(
                "DELETE FROM library_files WHERE user_id = ? AND path = ?",
                ((user_id, p) for p in paths),
            )

    def library_files(self, user_id: int, *, tagged: bool | None = None) -> list[LibraryFile]:
        """This user's indexed files by path: all of them, or only those with (`tagged`) or
        without an album id."""
        where = "" if tagged is None else f" AND release_mbid IS {'NOT ' if tagged else ''}NULL"
        rows = self.conn.execute(
            f"SELECT * FROM library_files WHERE user_id = ?{where} ORDER BY path", (user_id,)
        )
        return [
            LibraryFile(
                path=r["path"],
                mtime_ns=r["mtime_ns"],
                size=r["size"],
                release_mbid=r["release_mbid"],
                release_group_mbid=r["release_group_mbid"],
                album_artist_mbids=tuple(json.loads(r["album_artist_mbids"])),
                album=r["album"],
                album_artist=r["album_artist"],
                problem=r["problem"],
            )
            for r in rows
        ]

    def replace_library(self, user_id: int, releases: Iterable[LibraryRelease], now: str) -> None:
        """Make these the user's library: releases already there keep the date they were first
        found; any others are gone."""
        releases = list(releases)
        with self.conn:
            self.conn.executemany(
                "INSERT INTO library (user_id, release_mbid, release_group_mbid, "
                "album_artist_mbids, title, artist, folder, files, added_at, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (user_id, release_mbid) "
                "DO UPDATE SET release_group_mbid = excluded.release_group_mbid, "
                "album_artist_mbids = excluded.album_artist_mbids, title = excluded.title, "
                "artist = excluded.artist, folder = excluded.folder, files = excluded.files, "
                "last_seen = excluded.last_seen",
                (
                    (
                        user_id,
                        r.release_mbid,
                        r.release_group_mbid,
                        json.dumps(list(r.album_artist_mbids)),
                        r.title,
                        r.artist,
                        r.folder,
                        r.files,
                        now,
                        now,
                    )
                    for r in releases
                ),
            )
            kept = {r.release_mbid for r in releases}
            existing = self.conn.execute(
                "SELECT release_mbid FROM library WHERE user_id = ?", (user_id,)
            ).fetchall()
            self.conn.executemany(
                "DELETE FROM library WHERE user_id = ? AND release_mbid = ?",
                ((user_id, r[0]) for r in existing if r[0] not in kept),
            )

    def library(self, user_id: int) -> list[LibraryRelease]:
        """This user's library, by album artist then title."""
        rows = self.conn.execute(
            "SELECT * FROM library WHERE user_id = ? "
            "ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE, release_mbid",
            (user_id,),
        )
        return [_library_release(r) for r in rows]

    def library_editions(self, user_id: int, release_group_mbid: str) -> list[LibraryRelease]:
        """The releases of this release group in the user's library, most files first."""
        rows = self.conn.execute(
            "SELECT * FROM library WHERE user_id = ? AND release_group_mbid = ? "
            "ORDER BY files DESC, release_mbid",
            (user_id, release_group_mbid),
        )
        return [_library_release(r) for r in rows]

    def library_paths(self, user_id: int, release_mbid: str) -> list[str]:
        """The files of a release in the user's library, by path."""
        rows = self.conn.execute(
            "SELECT path FROM library_files WHERE user_id = ? AND release_mbid = ? ORDER BY path",
            (user_id, release_mbid),
        )
        return [r[0] for r in rows]

    def in_library(self, user_id: int, mbids: Iterable[str]) -> dict[str, str]:
        """Which of these release groups or releases (an MBID is never both) are in this user's
        library, with when each was first found (a release group's earliest edition)."""
        mbids = list(set(mbids))
        if not mbids:
            return {}
        marks = ", ".join("?" * len(mbids))
        rows = self.conn.execute(
            "SELECT release_group_mbid, MIN(added_at) FROM library WHERE user_id = ? "
            f"AND release_group_mbid IN ({marks}) GROUP BY release_group_mbid "
            "UNION ALL SELECT release_mbid, added_at FROM library WHERE user_id = ? "
            f"AND release_mbid IN ({marks})",
            (user_id, *mbids, user_id, *mbids),
        )
        return {r[0]: r[1] for r in rows}

    def library_summary(self, user_id: int) -> LibrarySummary:
        releases = self.conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT release_group_mbid), "
            "COUNT(*) - COUNT(release_group_mbid) FROM library WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        files = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(release_mbid IS NULL AND problem = ''), 0), "
            "COALESCE(SUM(problem != ''), 0) FROM library_files WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return LibrarySummary(*releases, *files)

    def start_library_scan(self, user_id: int, root: str, now: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO library_scans (user_id, root, started_at) VALUES (?, ?, ?)",
                (user_id, root, now),
            )

    def library_scan_progress(self, user_id: int, files: int, read: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE library_scans SET files = ?, read = ? WHERE user_id = ?",
                (files, read, user_id),
            )

    def finish_library_scan(
        self, user_id: int, files: int, read: int, left_over: int, error: str
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE library_scans SET finished_at = ?, files = ?, read = ?, left_over = ?, "
                "error = ? WHERE user_id = ?",
                (utcnow(), files, read, left_over, error, user_id),
            )

    def library_scan(self, user_id: int) -> LibraryScan | None:
        row = self.conn.execute(
            "SELECT * FROM library_scans WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return LibraryScan(
            row["root"],
            row["started_at"],
            row["finished_at"],
            row["files"],
            row["read"],
            row["left_over"],
            row["error"],
        )

    # --- API cache ------------------------------------------------------------------------

    def save_cached(
        self,
        service: str,
        kind: str,
        key: str,
        data: Mapping[str, Any] | None,
        refresh_after: str,
        *,
        fetched_at: str | None = None,
    ) -> None:
        blob = zlib.compress(json.dumps(data).encode()) if data is not None else None
        with self.conn:
            self.conn.execute(
                "INSERT INTO api_cache (service, kind, key, data, fetched_at, refresh_after) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (service, kind, key) DO UPDATE SET "
                "data = excluded.data, fetched_at = excluded.fetched_at, "
                "refresh_after = excluded.refresh_after",
                (service, kind, key, blob, fetched_at or utcnow(), refresh_after),
            )

    def cached(self, service: str, kind: str, key: str) -> CacheEntry | None:
        row = self.conn.execute(
            "SELECT data, fetched_at, refresh_after FROM api_cache "
            "WHERE service = ? AND kind = ? AND key = ?",
            (service, kind, key),
        ).fetchone()
        if row is None:
            return None
        data = json.loads(zlib.decompress(row["data"])) if row["data"] is not None else None
        return CacheEntry(data, row["fetched_at"], row["refresh_after"])

    def cache_due(self, service: str, kind: str, now: str, limit: int) -> list[str]:
        """Keys of `kind` whose refresh is due, oldest due first."""
        rows = self.conn.execute(
            "SELECT key FROM api_cache WHERE service = ? AND kind = ? AND refresh_after <= ? "
            "ORDER BY refresh_after LIMIT ?",
            (service, kind, now, limit),
        )
        return [r["key"] for r in rows]

    # --- Cover thumbnails -----------------------------------------------------------------

    def save_cover(
        self,
        release_group_mbid: str,
        image: bytes | None,
        content_type: str,
        refresh_after: str,
        *,
        fetched_at: str | None = None,
        source: str = "",
        source_mtime_ns: int = 0,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO covers (release_group_mbid, image, content_type, fetched_at, "
                "refresh_after, source, source_mtime_ns) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (release_group_mbid) DO UPDATE SET image = excluded.image, "
                "content_type = excluded.content_type, fetched_at = excluded.fetched_at, "
                "refresh_after = excluded.refresh_after, source = excluded.source, "
                "source_mtime_ns = excluded.source_mtime_ns",
                (
                    release_group_mbid,
                    image,
                    content_type,
                    fetched_at or utcnow(),
                    refresh_after,
                    source,
                    source_mtime_ns,
                ),
            )

    def cover(self, release_group_mbid: str) -> CachedCover | None:
        row = self.conn.execute(
            "SELECT image, content_type, fetched_at, refresh_after, source, source_mtime_ns "
            "FROM covers WHERE release_group_mbid = ?",
            (release_group_mbid,),
        ).fetchone()
        if row is None:
            return None
        return CachedCover(
            row["image"],
            row["content_type"],
            row["fetched_at"],
            row["refresh_after"],
            row["source"],
            row["source_mtime_ns"],
        )

    def drop_cached_before(self, service: str, kind: str, before: str) -> None:
        """Forget responses of `kind` whose refresh was due before `before` (for kinds nothing
        refreshes, such as searches)."""
        with self.conn:
            self.conn.execute(
                "DELETE FROM api_cache WHERE service = ? AND kind = ? AND refresh_after < ?",
                (service, kind, before),
            )


def _wishlist_item(row: sqlite3.Row) -> WishlistItem:
    return WishlistItem(
        id=row["id"],
        release_group_mbid=row["release_group_mbid"],
        title=row["title"],
        artist_credit=row["artist_credit"],
        artist_mbids=tuple(json.loads(row["artist_mbids"])),
        artist_names=tuple(json.loads(row["artist_names"])),
        primary_type=row["primary_type"],
        secondary_types=tuple(json.loads(row["secondary_types"])),
        first_release_date=row["first_release_date"],
        release_mbid=row["release_mbid"],
        added_at=row["added_at"],
        notes=row["notes"],
        heard=row["heard"],
        bought_at=row["bought_at"],
    )


def _library_release(row: sqlite3.Row) -> LibraryRelease:
    return LibraryRelease(
        release_mbid=row["release_mbid"],
        release_group_mbid=row["release_group_mbid"],
        album_artist_mbids=tuple(json.loads(row["album_artist_mbids"])),
        title=row["title"],
        artist=row["artist"],
        folder=row["folder"],
        files=row["files"],
        added_at=row["added_at"],
        last_seen=row["last_seen"],
    )


def _follow(row: sqlite3.Row) -> Follow:
    return Follow(
        artist_mbid=row["artist_mbid"],
        name=row["name"],
        sort_name=row["sort_name"],
        disambiguation=row["disambiguation"],
        followed_at=row["followed_at"],
        types=tuple(json.loads(row["types"])),
        singles_since=row["singles_since"],
    )
