"""Groups the operator makes: "ไฟชั้นล่าง", "ปิดก่อนนอน" -- devices from any
room, switched together.

Rooms are groups already and need nothing stored; these are the ones a room
cannot express. Kept in a small JSON file beside the device labels, for the
same reason: the Pi-only deployment runs with no database, and groups must
survive there too.

A group remembers device ids, not devices. A member that drops off the fabric
stays listed and simply is not switched until it comes back.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from app.labels import clean

log = logging.getLogger(__name__)

#: Enough for any house; a cap so a misbehaving client cannot fill the disk.
MAX_GROUPS = 50
MAX_MEMBERS = 100


def _members(value: Any) -> list[str]:
    """Distinct device ids, in the order given."""
    if not isinstance(value, list):
        raise ValueError("'devices' must be a list of device ids")
    seen: dict[str, None] = {}
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 200:
            raise ValueError("device ids must be non-empty strings")
        seen.setdefault(item, None)
    if len(seen) > MAX_MEMBERS:
        raise ValueError("a group holds at most %d devices" % MAX_MEMBERS)
    return list(seen)


def _name(value: Any) -> str:
    name = clean(value)
    if not name:
        raise ValueError("a group needs a name")
    return name


class GroupStore:
    def __init__(self, path: str | os.PathLike[str] | None) -> None:
        self.path = Path(path) if path else None
        # Insertion order is display order.
        self._groups: dict[str, dict[str, Any]] = {}
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            groups = raw.get("groups") if isinstance(raw, dict) else None
            if not isinstance(groups, list):
                raise ValueError("no 'groups' list")
        except (OSError, ValueError) as exc:
            # A corrupt file must not stop the dashboard from starting.
            self.last_error = str(exc)
            log.warning("ignoring unreadable group file %s: %s", self.path, exc)
            return
        for item in groups[:MAX_GROUPS]:
            try:
                group_id = str(item["id"])
                self._groups[group_id] = {
                    "id": group_id,
                    "name": _name(item.get("name")),
                    "devices": _members(item.get("devices", [])),
                }
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("skipping a stored group: %s", exc)
        log.info("loaded %d groups from %s", len(self._groups), self.path)

    def list(self) -> list[dict[str, Any]]:
        return [dict(g, devices=list(g["devices"])) for g in self._groups.values()]

    def get(self, group_id: str) -> dict[str, Any]:
        group = self._groups.get(group_id)
        if group is None:
            raise LookupError("unknown group " + repr(group_id))
        return dict(group, devices=list(group["devices"]))

    def create(self, name: Any, devices: Any) -> dict[str, Any]:
        if len(self._groups) >= MAX_GROUPS:
            raise ValueError("at most %d groups" % MAX_GROUPS)
        group_id = "g-" + secrets.token_hex(4)
        group = {"id": group_id, "name": _name(name), "devices": _members(devices)}
        self._commit({**self._groups, group_id: group})
        return self.get(group_id)

    def update(self, group_id: str, name: Any = None, devices: Any = None) -> dict[str, Any]:
        current = self.get(group_id)
        if name is not None:
            current["name"] = _name(name)
        if devices is not None:
            current["devices"] = _members(devices)
        self._commit({**self._groups, group_id: current})
        return self.get(group_id)

    def delete(self, group_id: str) -> None:
        self.get(group_id)
        groups = dict(self._groups)
        del groups[group_id]
        self._commit(groups)

    def _commit(self, groups: dict[str, dict[str, Any]]) -> None:
        # Disk first: a change that never reached the file must not linger in
        # memory, where it would show everywhere until a restart undid it.
        self._save(groups)
        self._groups = groups

    def _save(self, groups: dict[str, dict[str, Any]]) -> None:
        if self.path is None:
            raise OSError("group storage is disabled")
        payload = json.dumps(
            {"version": 1, "groups": list(groups.values())}, ensure_ascii=False, indent=2
        )
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(payload, encoding="utf-8")
            # Replace, never truncate-in-place: a power cut mid-write on a Pi
            # would otherwise leave an empty file and lose every group.
            os.replace(temp, self.path)
            self.last_error = None
        except OSError as exc:
            self.last_error = str(exc)
            log.warning("could not write %s: %s", self.path, exc)
            raise
