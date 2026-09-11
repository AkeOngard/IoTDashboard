"""DeviceRouter -- the hybrid pattern of doc §5.

Matter is primary (local, 20-80ms); Tuya Cloud picks up whatever the M1 does
not bridge out. The router is itself a DeviceAdapter, so the hub upstream never
learns that there is more than one backend behind it.

Routing is by device id prefix, which every adapter already stamps
("matter:1:7", "tuya:bf1a2c..."), plus an explicit override map for the cases
where a device is visible to both and you want to pin which path it uses.
"""
from __future__ import annotations

import asyncio
import logging

from app.adapters.base import DeviceAdapter, StateSink, StatusSink
from app.models import Command, CommandError, Device

log = logging.getLogger(__name__)


class DeviceRouter:
    name = "hybrid"

    def __init__(self, adapters: list[DeviceAdapter], overrides: dict[str, str] | None = None) -> None:
        if not adapters:
            raise ValueError("DeviceRouter needs at least one adapter")
        self._adapters = {a.name: a for a in adapters}
        self._primary = adapters[0]
        self._overrides = overrides or {}
        self._connected: dict[str, bool] = {a.name: False for a in adapters}
        self._on_status: StatusSink | None = None

    # ------------------------------------------------------------------ life

    async def start(self, on_state: StateSink, on_status: StatusSink) -> None:
        self._on_status = on_status
        for adapter in self._adapters.values():
            await adapter.start(on_state, self._make_status_sink(adapter.name))

    async def stop(self) -> None:
        await asyncio.gather(
            *(a.stop() for a in self._adapters.values()), return_exceptions=True
        )

    def _make_status_sink(self, name: str) -> StatusSink:
        async def sink(connected: bool) -> None:
            self._connected[name] = connected
            if self._on_status is not None:
                # The router is up as long as any backend is: losing the cloud
                # must not make a locally-reachable light look unavailable.
                await self._on_status(any(self._connected.values()))

        return sink

    # ------------------------------------------------------------- delegation

    async def discover(self) -> list[Device]:
        results = await asyncio.gather(
            *(a.discover() for a in self._adapters.values()), return_exceptions=True
        )
        devices: list[Device] = []
        seen: set[str] = set()
        for adapter, result in zip(self._adapters.values(), results):
            if isinstance(result, BaseException):
                log.warning("discover failed on %s: %s", adapter.name, result)
                continue
            for device in result:
                if device.id in seen:
                    continue
                seen.add(device.id)
                devices.append(device)
        return devices

    async def execute(self, command: Command) -> None:
        await self.route(command.device_id).execute(command)

    def route(self, device_id: str) -> DeviceAdapter:
        name = self._overrides.get(device_id) or device_id.split(":", 1)[0]
        adapter = self._adapters.get(name)
        if adapter is None:
            raise CommandError("no adapter can reach " + device_id)
        return adapter

    @property
    def backends(self) -> dict[str, bool]:
        return dict(self._connected)
