"""The seam that keeps the transport swappable.

Anything that can enumerate devices, stream their state and accept a command
can back the whole dashboard: Matter today, Tuya Cloud or Zigbee2MQTT later,
without the API or UI noticing.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Protocol, runtime_checkable

from app.models import Command, Device, StateEvent

StateSink = Callable[[StateEvent], Awaitable[None]]
StatusSink = Callable[[bool], Awaitable[None]]


@runtime_checkable
class DeviceAdapter(Protocol):
    name: str

    async def start(self, on_state: StateSink, on_status: StatusSink) -> None:
        """Connect to the backend and begin streaming state changes."""

    async def stop(self) -> None:
        """Tear down connections and background tasks."""

    async def discover(self) -> list[Device]:
        """Return the devices currently known to this adapter."""

    async def execute(self, command: Command) -> None:
        """Send a command. Raises CommandError if it could not be delivered.

        Returning successfully means *sent*, not *applied* -- confirmation is
        the hub's job, driven by the state echo that follows.
        """
