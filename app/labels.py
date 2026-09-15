"""Local names and rooms for devices.

A Tuya bridge publishes whatever label the hub itself knows, which is rarely
the name the operator typed into the Tuya app -- the M1, for instance, offers
"2 Gang Switch" for a two-gang wall switch and nothing at all for its two
relays. Matter has no way to ask for more, so the dashboard keeps its own
names beside the fabric's and shows those instead.

Kept in a small JSON file rather than the database: the Pi-only deployment
runs with DATABASE_URL empty, and names must survive there too.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Long enough for "โคมไฟห้องนั่งเล่น ดวงซ้าย", short enough to render.
MAX_LENGTH = 60
#: A device that leaves the fabric keeps its name in case it comes back, so the
#: file only ever grows. Cap it so a misbehaving client cannot fill the disk.
MAX_ENTRIES = 1000


def _entry(name: Any, room: Any) -> dict[str, str]:
    """A stored entry holds only the fields that carry an actual override."""
    return {k: v for k, v in (("name", clean(name)), ("room", clean(room))) if v}


def clean(value: Any) -> str:
    """Trim, drop control characters, and cut to a length a card can show."""
    text = "" if value is None else str(value)
    text = "".join(ch for ch in text if ch.isprintable()).strip()
    return text[:MAX_LENGTH]


class LabelStore:
    def __init__(self, path: str | os.PathLike[str] | None) -> None:
        self.path = Path(path) if path else None
        self._entries: dict[str, dict[str, str]] = {}
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entries = raw.get("devices") if isinstance(raw, dict) else None
            if not isinstance(entries, dict):
                raise ValueError("no 'devices' object")
        except (OSError, ValueError) as exc:
            # A corrupt file must not stop the dashboard from starting; the
            # names are a convenience, the devices are the point.
            self.last_error = str(exc)
            log.warning("ignoring unreadable label file %s: %s", self.path, exc)
            return
        for device_id, entry in list(entries.items())[:MAX_ENTRIES]:
            if isinstance(entry, dict):
                self._set(str(device_id), entry.get("name"), entry.get("room"))
        log.info("loaded %d device labels from %s", len(self._entries), self.path)

    def _set(self, device_id: str, name: Any, room: Any) -> dict[str, str]:
        entry = _entry(name, room)
        if entry:
            self._entries[device_id] = entry
        else:
            self._entries.pop(device_id, None)
        return entry

    def get(self, device_id: str) -> dict[str, str]:
        return self._entries.get(device_id, {})

    def apply(self, device: dict[str, Any]) -> dict[str, Any]:
        """Overlay the local name/room onto a serialised device."""
        entry = self._entries.get(str(device.get("id")))
        if not entry:
            return device
        merged = dict(device)
        merged.update(entry)
        # Let the card offer "use the name from the hub" only when there is one.
        merged["label_source"] = "local"
        merged["hub_name"] = device.get("name")
        merged["hub_room"] = device.get("room")
        return merged

    def set(self, device_id: str, name: Any = None, room: Any = None) -> dict[str, str]:
        """Store a name and/or room. An empty value clears that override."""
        current = dict(self._entries.get(device_id, {}))
        if name is not None:
            current["name"] = name
        if room is not None:
            current["room"] = room
        entry = _entry(current.get("name"), current.get("room"))

        # Only an entry that actually lands counts against the cap; clearing an
        # override, or a no-op on an unknown device, must not be refused.
        if entry and device_id not in self._entries and len(self._entries) >= MAX_ENTRIES:
            raise ValueError("too many stored labels (%d)" % MAX_ENTRIES)

        entries = dict(self._entries)
        if entry:
            entries[device_id] = entry
        else:
            entries.pop(device_id, None)
        # Disk first: a rename that never reached the file must not linger in
        # memory, where it would show on every dashboard until the next restart
        # quietly took it away again.
        self.save(entries)
        self._entries = entries
        return entry

    def save(self, entries: dict[str, dict[str, str]] | None = None) -> None:
        if self.path is None:
            return
        payload = json.dumps(
            {"version": 1, "devices": self._entries if entries is None else entries},
            ensure_ascii=False,
            indent=2,
        )
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(payload, encoding="utf-8")
            # Replace, never truncate-in-place: a power cut mid-write on a Pi
            # would otherwise leave an empty file and lose every name.
            os.replace(temp, self.path)
            self.last_error = None
        except OSError as exc:
            self.last_error = str(exc)
            log.warning("could not write %s: %s", self.path, exc)
            raise
