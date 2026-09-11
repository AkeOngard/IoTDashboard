"""Canonical device model. Every adapter normalises into these shapes, so the
API and the UI never learn which transport a device actually speaks."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Capability(str, Enum):
    SWITCH = "switch"
    BRIGHTNESS = "brightness"
    COLOR_TEMP = "color_temp"
    TEMPERATURE = "temperature"
    HUMIDITY = "humidity"
    ILLUMINANCE = "illuminance"
    OCCUPANCY = "occupancy"
    CONTACT = "contact"
    BATTERY = "battery"


#: Capabilities a user can write to. Everything else is read-only telemetry.
WRITABLE: set[Capability] = {
    Capability.SWITCH,
    Capability.BRIGHTNESS,
    Capability.COLOR_TEMP,
}

UNITS: dict[Capability, str] = {
    Capability.TEMPERATURE: "°C",
    Capability.HUMIDITY: "%",
    Capability.ILLUMINANCE: "lx",
    Capability.BRIGHTNESS: "%",
    Capability.COLOR_TEMP: "K",
    Capability.BATTERY: "%",
}

#: How close an echoed value must be to the requested one to count as a
#: confirmation. Dimmers quantise 0-100% onto 0-254 steps, hence the slack.
CONFIRM_TOLERANCE: dict[Capability, float] = {
    Capability.BRIGHTNESS: 3.0,
    Capability.COLOR_TEMP: 150.0,
}


@dataclass
class Device:
    id: str
    name: str
    room: str
    adapter: str
    native_id: str
    kind: str
    capabilities: list[Capability]
    online: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "room": self.room,
            "adapter": self.adapter,
            "native_id": self.native_id,
            "kind": self.kind,
            "capabilities": [c.value for c in self.capabilities],
            "writable": [c.value for c in self.capabilities if c in WRITABLE],
            "online": self.online,
        }


@dataclass
class StateEvent:
    device_id: str
    capability: Capability
    value: Any
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "capability": self.capability.value,
            "value": self.value,
            "unit": UNITS.get(self.capability, ""),
            "ts": self.ts,
        }


@dataclass
class Command:
    device_id: str
    capability: Capability
    value: Any


class CommandError(Exception):
    """Adapter could not deliver the command at all (as opposed to the device
    accepting it but never echoing the new state back)."""
