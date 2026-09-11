"""asyncpg pool with a reconnect loop.

The dashboard must keep working when the database is down -- live control is
the safety-critical part, history is not -- so nothing here ever raises into
the request path. Callers check `db.available` and degrade.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import asyncpg

log = logging.getLogger(__name__)


class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._closing = False

    @property
    def enabled(self) -> bool:
        return bool(self.dsn)

    @property
    def available(self) -> bool:
        return self.pool is not None

    async def start(self) -> None:
        if not self.enabled:
            log.info("DATABASE_URL is empty; running live-only (no history)")
            return
        self._closing = False
        # One synchronous attempt so a healthy start-up is fully ready before
        # the first request, then hand off to the background reconnect loop.
        await self._try_connect()
        self._task = asyncio.create_task(self._keep_alive())

    async def stop(self) -> None:
        self._closing = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def _try_connect(self) -> bool:
        try:
            self.pool = await asyncpg.create_pool(
                self.dsn, min_size=1, max_size=8, command_timeout=15, timeout=10
            )
            self.last_error = None
            log.info("database connected")
            return True
        except Exception as exc:  # noqa: BLE001 - any failure means "degraded"
            self.pool = None
            self.last_error = str(exc)
            log.warning("database unavailable: %s", exc)
            return False

    async def _keep_alive(self) -> None:
        backoff = 2.0
        while not self._closing:
            await asyncio.sleep(5 if self.available else backoff)
            if self._closing:
                return
            if self.available:
                try:
                    async with self.pool.acquire() as conn:  # type: ignore[union-attr]
                        await conn.execute("SELECT 1")
                    continue
                except Exception as exc:  # noqa: BLE001
                    log.warning("database health check failed: %s", exc)
                    with contextlib.suppress(Exception):
                        await self.pool.close()  # type: ignore[union-attr]
                    self.pool = None
                    self.last_error = str(exc)
            if await self._try_connect():
                backoff = 2.0
            else:
                backoff = min(backoff * 2, 60.0)

    # ------------------------------------------------------------- shortcuts

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        if self.pool is None:
            raise ConnectionError(self.last_error or "database is not connected")
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        if self.pool is None:
            raise ConnectionError(self.last_error or "database is not connected")
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def executemany(self, query: str, rows: list[tuple]) -> None:
        if self.pool is None:
            raise ConnectionError(self.last_error or "database is not connected")
        async with self.pool.acquire() as conn:
            await conn.executemany(query, rows)
