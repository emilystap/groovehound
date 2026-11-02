"""SQLite persistence: users, their wishlists, and the shared cache of MusicBrainz /
ListenBrainz responses.

Plain `sqlite3` keeps the dependency list short. Schema changes go in MIGRATIONS (append
only); `PRAGMA user_version` records which have run.

Multi-user ready: anything a user owns (wishlist items, follows, dismissed suggestions) gets a
`user_id` column referencing `users`. Caches of API responses are shared and have none, so one
artist followed by two users is still fetched once. Today there is one user, id `OWNER_ID`.
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
]


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
    primary_type: str = ""
    secondary_types: tuple[str, ...] = ()
    first_release_date: str = ""
    release_mbid: str | None = None  # a specific edition, when one was chosen
    added_at: str = ""

    @property
    def type_label(self) -> str:
        """e.g. "Album", "Album · Live", or "" when MusicBrainz has no type."""
        return " · ".join(t for t in (self.primary_type, *self.secondary_types) if t)


@dataclass(frozen=True)
class CacheEntry:
    data: dict | None  # None = the service had nothing for this key
    fetched_at: str
    refresh_after: str


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
                "artist_credit, artist_mbids, primary_type, secondary_types, first_release_date, "
                "added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (user_id, release_group_mbid) DO NOTHING",
                (
                    user_id,
                    item.release_group_mbid,
                    item.release_mbid,
                    item.title,
                    item.artist_credit,
                    json.dumps(list(item.artist_mbids)),
                    item.primary_type,
                    json.dumps(list(item.secondary_types)),
                    item.first_release_date,
                    item.added_at or utcnow(),
                ),
            )
        return cursor.rowcount == 1

    def wishlist(self, user_id: int, limit: int | None = None) -> list[WishlistItem]:
        """This user's wishlist, most recently added first."""
        rows = self.conn.execute(
            "SELECT * FROM wishlist WHERE user_id = ? ORDER BY added_at DESC, id DESC LIMIT ?",
            (user_id, -1 if limit is None else limit),
        )
        return [
            WishlistItem(
                release_group_mbid=r["release_group_mbid"],
                title=r["title"],
                artist_credit=r["artist_credit"],
                artist_mbids=tuple(json.loads(r["artist_mbids"])),
                primary_type=r["primary_type"],
                secondary_types=tuple(json.loads(r["secondary_types"])),
                first_release_date=r["first_release_date"],
                release_mbid=r["release_mbid"],
                added_at=r["added_at"],
            )
            for r in rows
        ]

    def wishlist_count(self, user_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM wishlist WHERE user_id = ?", (user_id,)
        ).fetchone()[0]

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

    def drop_cached_before(self, service: str, kind: str, before: str) -> None:
        """Forget responses of `kind` whose refresh was due before `before` (for kinds nothing
        refreshes, such as searches)."""
        with self.conn:
            self.conn.execute(
                "DELETE FROM api_cache WHERE service = ? AND kind = ? AND refresh_after < ?",
                (service, kind, before),
            )
