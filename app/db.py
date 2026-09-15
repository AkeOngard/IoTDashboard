"""asyncpg pool with a reconnect loop.

The dashboard must keep working when the database is down -- live control is
the safety-critical part, history is not -- so nothing here ever raises into
the request path. Callers check `db.available` and degrade.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import asyncpg

log = logging.getLogger(__name__)

#: Supabase's transaction pooler. It hands a different backend to every
#: transaction, so a named prepared statement prepared on one connection is
#: gone by the next query -- asyncpg then fails with
#: `prepared statement "asyncpg_stmt_N" does not exist`. Setting the cache to
#: zero makes asyncpg use unnamed statements, which the pooler allows.
POOLED_PORTS = {6543}

CAPABILITY_SQL = """
SELECT (SELECT extversion FROM pg_extension WHERE extname = 'timescaledb') AS timescale,
       to_regclass('public.telemetry_5m') IS NOT NULL                      AS rollup
"""


class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None
        self.last_error: str | None = None
        #: TimescaleDB version, or None on plain PostgreSQL. History picks its
        #: SQL from this, so it is probed on every (re)connect rather than
        #: configured -- the same image runs against both.
        self.timescale: str | None = None
        #: Whether the 5-minute continuous aggregate exists.
        self.rollup = False
        #: Whether the two above are knowledge rather than a guess. A failed
        #: probe leaves them at their defaults, and "plain PostgreSQL" is a
        #: licence to delete rows -- so anything destructive checks this first.
        self.probed = False
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
                self.dsn,
                min_size=1,
                max_size=8,
                command_timeout=15,
                timeout=10,
                **_pool_kwargs(self.dsn),
            )
            self.last_error = None
            await self.probe()
            log.info(
                "database connected (%s)",
                "timescaledb " + self.timescale if self.timescale else "plain postgresql",
            )
            return True
        except Exception as exc:  # noqa: BLE001 - any failure means "degraded"
            self.pool = None
            self.last_error = str(exc)
            log.warning("database unavailable: %s", exc)
            return False

    async def probe(self) -> None:
        """Ask the server what it can do, rather than trusting configuration.

        Call again after anything that can change the answer -- applying
        migrations creates the continuous aggregate, and a probe taken before
        that would read a stale False for the rest of the process.
        """
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(CAPABILITY_SQL)
        except Exception as exc:  # noqa: BLE001 - stay honest about not knowing
            log.warning("could not probe database features: %s", exc)
            self.probed = False
            return
        self.timescale = row["timescale"] if row else None
        self.rollup = bool(row["rollup"]) if row else False
        self.probed = True

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
                    # We may come back to a different server entirely.
                    self.probed = False
                    self.last_error = str(exc)
            if await self._try_connect():
                backoff = 2.0
            else:
                backoff = min(backoff * 2, 60.0)

    # ------------------------------------------------------------- shortcuts

    @property
    def features(self) -> dict[str, Any]:
        return {"timescaledb": self.timescale, "rollup": self.rollup, "probed": self.probed}

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


def _dsn_port(dsn: str) -> int | None:
    """asyncpg takes both URL and libpq keyword DSNs; read the port from either."""
    try:
        port = urlsplit(dsn).port
    except ValueError:      # malformed port; let asyncpg produce the error
        return None
    if port is not None:
        return port
    match = re.search(r"(?:^|\s)port\s*=\s*'?(\d+)'?", dsn)
    return int(match.group(1)) if match else None


def _pool_kwargs(dsn: str) -> dict[str, Any]:
    """Connection options a pooled DSN needs to work at all."""
    port = _dsn_port(dsn)
    if port in POOLED_PORTS:
        log.info("port %d looks like a transaction pooler; disabling prepared statements", port)
        return {"statement_cache_size": 0}
    return {}
