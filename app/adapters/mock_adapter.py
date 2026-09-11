"""Mock adapter -- a fake Zigbee bridge, so the whole UI can be built and
demoed on a Windows box with no hardware and no matter-server in sight.

It deliberately imitates the awkward parts of real hardware: commands take a
moment to echo, and one in every SIMULATED_LOSS commands is silently dropped so
the pending -> failed path actually gets exercised.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random

from app.adapters.base import StateSink, StatusSink
from app.models import Capability, Command, Device, StateEvent

log = logging.getLogger(__name__)

#: Fraction of commands the fake mesh drops on the floor.
SIMULATED_LOSS = 0.08
#: Seconds between simulated sensor reports.
SENSOR_PERIOD = 5.0


def _devices() -> list[Device]:
    return [
        Device("mock:1", "Ceiling Light", "Living Room", "mock", "1", "light",
               [Capability.SWITCH, Capability.BRIGHTNESS, Capability.COLOR_TEMP]),
        Device("mock:2", "Desk Lamp", "Office", "mock", "2", "light",
               [Capability.SWITCH, Capability.BRIGHTNESS]),
        Device("mock:3", "Air Purifier Plug", "Living Room", "mock", "3", "plug",
               [Capability.SWITCH]),
        Device("mock:4", "Climate Sensor", "Living Room", "mock", "4", "sensor",
               [Capability.TEMPERATURE, Capability.HUMIDITY, Capability.BATTERY]),
        Device("mock:5", "Climate Sensor", "Server Rack", "mock", "5", "sensor",
               [Capability.TEMPERATURE, Capability.HUMIDITY, Capability.BATTERY]),
        Device("mock:6", "Window Contact", "Office", "mock", "6", "sensor",
               [Capability.CONTACT, Capability.BATTERY]),
    ]


class MockAdapter:
    name = "mock"

    def __init__(self) -> None:
        self._devices = _devices()
        self._state: dict[tuple[str, Capability], object] = {
            ("mock:1", Capability.SWITCH): False,
            ("mock:1", Capability.BRIGHTNESS): 60,
            ("mock:1", Capability.COLOR_TEMP): 3000,
            ("mock:2", Capability.SWITCH): True,
            ("mock:2", Capability.BRIGHTNESS): 80,
            ("mock:3", Capability.SWITCH): True,
            ("mock:4", Capability.TEMPERATURE): 27.4,
            ("mock:4", Capability.HUMIDITY): 58.0,
            ("mock:4", Capability.BATTERY): 92,
            ("mock:5", Capability.TEMPERATURE): 21.1,
            ("mock:5", Capability.HUMIDITY): 45.0,
            ("mock:5", Capability.BATTERY): 78,
            ("mock:6", Capability.CONTACT): True,
            ("mock:6", Capability.BATTERY): 64,
        }
        self._on_state: StateSink | None = None
        self._task: asyncio.Task | None = None

    async def start(self, on_state: StateSink, on_status: StatusSink) -> None:
        self._on_state = on_state
        await on_status(True)
        self._task = asyncio.create_task(self._drift())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def discover(self) -> list[Device]:
        # Publish the seeded state as if the devices had just reported in.
        asyncio.create_task(self._replay())
        return list(self._devices)

    async def execute(self, command: Command) -> None:
        asyncio.create_task(self._apply(command))

    async def _apply(self, command: Command) -> None:
        await asyncio.sleep(random.uniform(0.15, 0.6))
        if random.random() < SIMULATED_LOSS:
            log.info("mock mesh dropped %s -> %r", command.device_id, command.value)
            return
        key = (command.device_id, command.capability)
        self._state[key] = command.value
        await self._emit(key)
        # Dimming a light that was off implicitly turns it on, same as real ones.
        if command.capability is Capability.BRIGHTNESS and command.value:
            switch = (command.device_id, Capability.SWITCH)
            if switch in self._state and not self._state[switch]:
                self._state[switch] = True
                await self._emit(switch)

    async def _replay(self) -> None:
        await asyncio.sleep(0.1)
        for key in list(self._state):
            await self._emit(key)

    async def _drift(self) -> None:
        while True:
            await asyncio.sleep(SENSOR_PERIOD)
            for device_id in ("mock:4", "mock:5"):
                for cap, span, lo, hi, digits in (
                    (Capability.TEMPERATURE, 0.3, 15.0, 40.0, 1),
                    (Capability.HUMIDITY, 0.8, 20.0, 90.0, 0),
                ):
                    key = (device_id, cap)
                    current = float(self._state[key])  # type: ignore[arg-type]
                    nudged = min(hi, max(lo, current + random.uniform(-span, span)))
                    self._state[key] = round(nudged, digits)
                    await self._emit(key)

    async def _emit(self, key: tuple[str, Capability]) -> None:
        if self._on_state is None:
            return
        device_id, cap = key
        await self._on_state(StateEvent(device_id, cap, self._state[key]))
