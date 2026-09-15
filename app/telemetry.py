"""Telemetry persistence: deadband filter, heartbeat, batched writes.

A Zigbee climate sensor that reports every 30s produces ~2900 rows a day per
capability, almost all of them identical to the row before. Writing every one
of them buys nothing and costs chunk space forever, so a value is only stored
when it moved by more than the deadband -- with a heartbeat so a perfectly
stable sensor still leaves a trail proving it was alive.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import timedelta
from typing import Any

from app.db import Database
from app.models import Capability, Device, StateEvent

log = logging.getLogger(__name__)

#: Absolute change required before a new sample is stored.
ABS_DEADBAND: dict[Capability, float] = {
    Capability.TEMPERATURE: 0.2,
    Capability.HUMIDITY: 1.0,
    Capability.BATTERY: 1.0,
    Capability.BRIGHTNESS: 2.0,
    Capability.COLOR_TEMP: 50.0,
}

#: Fractional change, for readings that span orders of magnitude.
REL_DEADBAND: dict[Capability, float] = {
    Capability.ILLUMINANCE: 0.10,
}

#: Booleans are rare and meaningful -- every transition is worth a row.
BOOLEAN_CAPS = {Capability.SWITCH, Capability.CONTACT, Capability.OCCUPANCY}

#: Store an unchanged reading at least this often, so a gap in the chart means
#: "sensor stopped reporting" rather than "value happened to be stable".
HEARTBEAT_SECONDS = 300.0

INSERT_SQL = (
    "INSERT INTO telemetry (ts, device_id, capability, value) "
    "VALUES (to_timestamp($1), $2, $3, $4)"
)

UPSERT_DEVICE_SQL = """
INSERT INTO devices (id, name, room, adapter, native_id, kind, capabilities, last_seen)
VALUES ($1, $2, $3, $4, $5, $6, $7, now())
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name,
    room = EXCLUDED.room,
    adapter = EXCLUDED.adapter,
    native_id = EXCLUDED.native_id,
    kind = EXCLUDED.kind,
    capabilities = EXCLUDED.capabilities,
    last_seen = now()
"""

LAST_STATE_SQL = """
SELECT DISTINCT ON (device_id, capability)
       device_id, capability, value, extract(epoch FROM ts) AS ts
FROM telemetry
WHERE ts > now() - INTERVAL '7 days'
ORDER BY device_id, capability, ts DESC
"""

#: Retention on plain PostgreSQL, where there is no TimescaleDB policy to do
#: it. Deleted in batches so the first sweep after a long gap cannot hold a
#: long lock or blow up the WAL on a small managed instance.
#:
#: ONLY is load-bearing, not decoration. ctid is unique within one heap, and a
#: hypertable spreads rows over chunk tables that each start again at (0,1) --
#: so without it, an IN-list of old ctids also matches *recent* rows in other
#: chunks. Measured on timescaledb 2.30.0: ten 40-day-old rows and ten from
#: today, and the unqualified form reported `DELETE 20`. ONLY confines both
#: halves to the parent table, which on a hypertable holds nothing, making this
#: an inert no-op there and leaving retention to the policy that owns it.
PRUNE_SQL = """
DELETE FROM ONLY telemetry
WHERE ctid IN (
    SELECT ctid FROM ONLY telemetry WHERE ts < now() - $1::interval LIMIT $2
)
"""
PRUNE_BATCH = 5000


class Recorder:
    def __init__(
        self,
        db: Database,
        flush_interval: float = 2.0,
        max_buffer: int = 5000,
        retention_days: float = 0.0,
        prune_interval: float = 6 * 3600.0,
    ) -> None:
        self._db = db
        self._flush_interval = flush_interval
        self._max_buffer = max_buffer
        self._retention_days = retention_days
        self._prune_interval = prune_interval
        self._buffer: list[tuple[float, str, str, float]] = []
        self._last_written: dict[tuple[str, Capability], tuple[float, float]] = {}
        self._tasks: list[asyncio.Task] = []
        self.written = 0
        self.dropped = 0
        self.pruned = 0

    async def start(self) -> None:
        if not self._db.enabled:
            return
        self._tasks.append(asyncio.create_task(self._flusher()))
        if self._retention_days > 0:
            self._tasks.append(asyncio.create_task(self._pruner()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        with contextlib.suppress(Exception):
            await self._flush()

    @property
    def stats(self) -> dict[str, Any]:
        stats = {"written": self.written, "dropped": self.dropped, "buffered": len(self._buffer)}
        # Only meaningful where this process owns retention: history on, and no
        # TimescaleDB policy doing the job instead.
        if self._db.enabled and self._retention_days > 0 and not self._db.timescale:
            stats["pruned"] = self.pruned
        return stats

    # ------------------------------------------------------------------ write

    async def record(self, event: StateEvent) -> None:
        if not self._db.enabled:
            return
        value = _to_number(event.value)
        if value is None:
            return
        key = (event.device_id, event.capability)
        previous = self._last_written.get(key)
        if previous is not None and not _worth_storing(event.capability, previous, value, event.ts):
            return
        self._last_written[key] = (value, event.ts)
        self._buffer.append((event.ts, event.device_id, event.capability.value, value))
        if len(self._buffer) >= self._max_buffer:
            await self._flush()

    async def sync_devices(self, devices: list[Device]) -> None:
        if not self._db.available or not devices:
            return
        rows = [
            (d.id, d.name, d.room, d.adapter, d.native_id, d.kind, [c.value for c in d.capabilities])
            for d in devices
        ]
        try:
            await self._db.executemany(UPSERT_DEVICE_SQL, rows)
        except Exception as exc:  # noqa: BLE001 - inventory is best-effort
            log.warning("device upsert failed: %s", exc)

    async def load_last_states(self) -> list[StateEvent]:
        """Seed the in-memory state from the DB so a restart does not show an
        empty dashboard until every sensor happens to report again."""
        if not self._db.available:
            return []
        try:
            rows = await self._db.fetch(LAST_STATE_SQL)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not restore last states: %s", exc)
            return []
        events: list[StateEvent] = []
        for row in rows:
            try:
                cap = Capability(row["capability"])
            except ValueError:
                continue
            events.append(
                StateEvent(row["device_id"], cap, _from_number(cap, row["value"]), float(row["ts"]))
            )
            self._last_written[(row["device_id"], cap)] = (float(row["value"]), float(row["ts"]))
        log.info("restored %d cached readings from the database", len(events))
        return events

    # ------------------------------------------------------------------ flush

    async def _flusher(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            with contextlib.suppress(Exception):
                await self._flush()

    # -------------------------------------------------------------- retention

    async def _pruner(self) -> None:
        # A short first delay so a restart loop cannot turn into a delete loop,
        # but soon enough that a box left off for months tidies up on boot.
        await asyncio.sleep(60)
        while True:
            with contextlib.suppress(Exception):
                await self.prune()
            await asyncio.sleep(self._prune_interval)

    async def prune(self) -> int:
        """Drop telemetry past the retention window. No-op on TimescaleDB,
        which runs its own retention policy from migration 005."""
        # `probed` and not `timescale`: a failed probe reports None, which must
        # not be read as "plain PostgreSQL, go ahead and delete".
        if not self._db.probed or self._db.timescale or self._retention_days <= 0:
            return 0
        window = timedelta(days=self._retention_days)
        removed = 0
        while True:
            status = await self._db.execute(PRUNE_SQL, window, PRUNE_BATCH)
            # asyncpg returns the tag, e.g. "DELETE 5000".
            count = int(status.rsplit(" ", 1)[-1]) if status.startswith("DELETE") else 0
            removed += count
            if count < PRUNE_BATCH:
                break
        if removed:
            self.pruned += removed
            log.info("pruned %d telemetry rows older than %g days", removed, self._retention_days)
        return removed

    async def _flush(self) -> None:
        if not self._buffer:
            return
        if not self._db.available:
            # Keep the newest samples; a long outage should not eat all memory.
            overflow = len(self._buffer) - self._max_buffer
            if overflow > 0:
                del self._buffer[:overflow]
                self.dropped += overflow
            return
        batch, self._buffer = self._buffer, []
        try:
            await self._db.executemany(INSERT_SQL, batch)
            self.written += len(batch)
        except Exception as exc:  # noqa: BLE001
            log.warning("telemetry flush failed (%d rows requeued): %s", len(batch), exc)
            self._buffer = batch + self._buffer


def _worth_storing(
    cap: Capability, previous: tuple[float, float], value: float, ts: float
) -> bool:
    last_value, last_ts = previous
    if ts - last_ts >= HEARTBEAT_SECONDS:
        return True
    if cap in BOOLEAN_CAPS:
        return value != last_value
    relative = REL_DEADBAND.get(cap)
    if relative is not None:
        return abs(value - last_value) >= max(abs(last_value) * relative, 1.0)
    return abs(value - last_value) >= ABS_DEADBAND.get(cap, 0.0)


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _from_number(cap: Capability, value: float) -> Any:
    if cap in BOOLEAN_CAPS:
        return bool(value)
    if cap in (Capability.BATTERY, Capability.BRIGHTNESS, Capability.COLOR_TEMP):
        return int(round(value))
    return round(value, 2)
