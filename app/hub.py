"""Device registry, live state store and command lifecycle.

The interesting part is the command lifecycle. A dashboard that flips a toggle
and assumes success lies to the operator whenever the mesh drops a packet, so
every write goes through pending -> confirmed | failed, where "confirmed" means
the device itself echoed the new value back.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from typing import Any

from app.adapters.base import DeviceAdapter
from app.labels import LabelStore
from app.models import (
    CONFIRM_TOLERANCE,
    WRITABLE,
    Capability,
    Command,
    CommandError,
    Device,
    StateEvent,
)

log = logging.getLogger(__name__)

#: Dropped by the broadcaster when a subscriber stops draining its queue.
QUEUE_LIMIT = 256


class _Pending:
    __slots__ = ("target", "started", "task")

    def __init__(self, target: Any, started: float, task: asyncio.Task | None = None):
        self.target = target
        self.started = started
        self.task = task


class Hub:
    def __init__(
        self,
        adapter: DeviceAdapter,
        command_timeout: float = 5.0,
        recorder: Any | None = None,
        labels: LabelStore | None = None,
    ) -> None:
        self._adapter = adapter
        self._timeout = command_timeout
        self._recorder = recorder
        self._labels = labels or LabelStore(None)
        self._devices: dict[str, Device] = {}
        self._states: dict[tuple[str, Capability], StateEvent] = {}
        self._pending: dict[tuple[str, Capability], _Pending] = {}
        self._subscribers: set[asyncio.Queue[dict]] = set()
        self._adapter_connected = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ life

    async def start(self) -> None:
        await self._adapter.start(self._on_state, self._on_status)
        await self.refresh()

    async def stop(self) -> None:
        for pending in self._pending.values():
            if pending.task:
                pending.task.cancel()
        self._pending.clear()
        await self._adapter.stop()

    async def refresh(self) -> None:
        """Re-read the device list from the adapter and publish it."""
        devices = await self._adapter.discover()
        async with self._lock:
            self._devices = {d.id: d for d in devices}
        if self._recorder is not None:
            # The inventory table should read like the dashboard does, so the
            # local names go in -- but on copies: the adapter owns these
            # objects and mutates `online` on them.
            await self._recorder.sync_devices([self._renamed(d) for d in devices])
        await self._broadcast(self.snapshot())

    def _renamed(self, device: Device) -> Device:
        entry = self._labels.get(device.id)
        if not entry:
            return device
        return dataclasses.replace(
            device, name=entry.get("name", device.name), room=entry.get("room", device.room)
        )

    async def relabel(self, device_id: str, name: Any = None, room: Any = None) -> dict[str, Any]:
        """Rename a device locally. Matter only ever offers the hub's own label,
        which is usually not the name the operator gave it."""
        if device_id not in self._devices:
            raise LookupError("unknown device " + repr(device_id))
        self._labels.set(device_id, name=name, room=room)
        device = self._describe(self._devices[device_id])
        await self._broadcast(
            {"type": "devices", "devices": [self._describe(d) for d in self._devices.values()]}
        )
        return device

    def seed_states(self, events: list[StateEvent]) -> None:
        """Pre-load cached readings (from the DB) so the dashboard is populated
        before the first live report arrives. Never overwrites live data."""
        for event in events:
            self._states.setdefault((event.device_id, event.capability), event)

    # ------------------------------------------------------------- read side

    @property
    def devices(self) -> list[Device]:
        return list(self._devices.values())

    @property
    def adapter(self) -> DeviceAdapter:
        return self._adapter

    def _describe(self, device: Device) -> dict[str, Any]:
        return self._labels.apply(device.to_dict())

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "adapter": self._adapter.name,
            "connected": self._adapter_connected,
            "devices": [self._describe(d) for d in self._devices.values()],
            "states": [s.to_dict() for s in self._states.values()],
            "pending": [
                {"device_id": did, "capability": cap.value, "value": p.target}
                for (did, cap), p in self._pending.items()
            ],
        }

    def subscribe(self) -> asyncio.Queue[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict]) -> None:
        self._subscribers.discard(queue)

    # ------------------------------------------------------------ write side

    async def execute(self, device_id: str, capability: str, value: Any) -> dict[str, Any]:
        device = self._devices.get(device_id)
        if device is None:
            raise LookupError("unknown device " + repr(device_id))
        try:
            cap = Capability(capability)
        except ValueError as exc:
            raise LookupError("unknown capability " + repr(capability)) from exc
        if cap not in device.capabilities or cap not in WRITABLE:
            raise LookupError(device_id + " does not accept writes to " + capability)

        value = _coerce(cap, value)
        key = (device_id, cap)

        # A newer command supersedes whatever was still in flight for this key.
        previous = self._pending.pop(key, None)
        if previous and previous.task:
            previous.task.cancel()

        pending = _Pending(target=value, started=time.time())
        self._pending[key] = pending
        await self._broadcast(
            {
                "type": "command",
                "status": "pending",
                "device_id": device_id,
                "capability": cap.value,
                "value": value,
            }
        )

        try:
            await self._adapter.execute(Command(device_id, cap, value))
        except CommandError as exc:
            self._pending.pop(key, None)
            await self._broadcast(
                {
                    "type": "command",
                    "status": "failed",
                    "device_id": device_id,
                    "capability": cap.value,
                    "value": value,
                    "reason": str(exc),
                }
            )
            raise

        pending.task = asyncio.create_task(self._expire(key, value))
        return {
            "status": "pending",
            "device_id": device_id,
            "capability": cap.value,
            "value": value,
        }

    async def _expire(self, key: tuple[str, Capability], value: Any) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(self._timeout)
            if self._pending.pop(key, None) is None:
                return
            device_id, cap = key
            log.warning("command timed out: %s %s -> %r", device_id, cap.value, value)
            await self._broadcast(
                {
                    "type": "command",
                    "status": "failed",
                    "device_id": device_id,
                    "capability": cap.value,
                    "value": value,
                    "reason": "no state echo within %gs" % self._timeout,
                }
            )

    # ----------------------------------------------------- adapter callbacks

    async def _on_state(self, event: StateEvent) -> None:
        key = (event.device_id, event.capability)
        previous = self._states.get(key)
        self._states[key] = event

        if self._recorder is not None:
            await self._recorder.record(event)

        pending = self._pending.get(key)
        if pending is not None and _matches(event.capability, pending.target, event.value):
            self._pending.pop(key, None)
            if pending.task:
                pending.task.cancel()
            await self._broadcast(
                {
                    "type": "command",
                    "status": "confirmed",
                    "device_id": event.device_id,
                    "capability": event.capability.value,
                    "value": event.value,
                    "latency_ms": round((time.time() - pending.started) * 1000),
                }
            )

        # Suppress duplicate telemetry so idle sensors do not spam every client.
        if previous is not None and previous.value == event.value and pending is None:
            return
        await self._broadcast({"type": "state", **event.to_dict()})

    async def _on_status(self, connected: bool) -> None:
        if connected == self._adapter_connected:
            return
        self._adapter_connected = connected
        await self._broadcast(
            {"type": "adapter", "connected": connected, "adapter": self._adapter.name}
        )
        if connected:
            await self.refresh()

    # --------------------------------------------------------------- fan-out

    async def _broadcast(self, message: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Slow client: drop it rather than let it stall the event loop.
                log.warning("dropping slow websocket subscriber")
                self._subscribers.discard(queue)


def _coerce(cap: Capability, value: Any) -> Any:
    if cap is Capability.SWITCH:
        if isinstance(value, str):
            return value.lower() in {"1", "true", "on", "yes"}
        return bool(value)
    if cap is Capability.BRIGHTNESS:
        return max(0, min(100, int(round(float(value)))))
    if cap is Capability.COLOR_TEMP:
        return max(1700, min(6500, int(round(float(value)))))
    return value


def _matches(cap: Capability, target: Any, actual: Any) -> bool:
    tolerance = CONFIRM_TOLERANCE.get(cap)
    if tolerance is None:
        return target == actual
    try:
        return abs(float(target) - float(actual)) <= tolerance
    except (TypeError, ValueError):
        return target == actual
