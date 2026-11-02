"""SQLite persistence: users and the shared cache of MusicBrainz / ListenBrainz responses.

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
from collections.abc import Mapping
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
]


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class User:
    id: int
    name: str


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
