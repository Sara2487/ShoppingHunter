"""SQLite cache for search results and offers (aiosqlite, WAL, versioned schema).

Keys are hashes of typed tuples (see ``AmazonAdapter``): search results depend on marketplace,
normalised query, destination, language and page; offers on marketplace, ASIN, destination,
an account-context hash and whether other sellers were included. Failures are cached only
briefly (negative TTL); robot checks, timeouts and login/location problems are never cached.
Cart revalidation always bypasses the cache.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .logging import log_event

SCHEMA_VERSION = 1


def cache_key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


class Cache:
    def __init__(self, path: Path):
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        cur = await self._db.execute("SELECT v FROM meta WHERE k='schema_version'")
        row = await cur.fetchone()
        if row and int(row[0]) != SCHEMA_VERSION:
            await self._db.execute("DROP TABLE IF EXISTS kv")
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS kv (kind TEXT, key TEXT, value TEXT, created_at REAL, expires_at REAL, "
            "PRIMARY KEY (kind, key))")
        await self._db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def get(self, kind: str, key: str) -> tuple[dict[str, Any], datetime] | None:
        if not self._db:
            return None
        cur = await self._db.execute("SELECT value, created_at, expires_at FROM kv WHERE kind=? AND key=?", (kind, key))
        row = await cur.fetchone()
        if not row:
            self.misses += 1
            return None
        value, created, expires = row
        if expires < time.time():
            async with self._lock:
                await self._db.execute("DELETE FROM kv WHERE kind=? AND key=?", (kind, key))
                await self._db.commit()
            self.misses += 1
            return None
        self.hits += 1
        return json.loads(value), datetime.fromtimestamp(created, tz=timezone.utc)

    async def set(self, kind: str, key: str, value: dict[str, Any], ttl_s: int) -> None:
        if not self._db or ttl_s <= 0:
            return
        now = time.time()
        async with self._lock:
            await self._db.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?,?,?)",
                                   (kind, key, json.dumps(value, default=str), now, now + ttl_s))
            await self._db.commit()

    async def purge_expired(self) -> int:
        if not self._db:
            return 0
        async with self._lock:
            cur = await self._db.execute("DELETE FROM kv WHERE expires_at < ?", (time.time(),))
            await self._db.commit()
            n = cur.rowcount or 0
        if n:
            log_event("cache_purged", rows=n)
        return n
