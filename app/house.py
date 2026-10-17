"""The house as a plan: floors, rooms, doors -- and where each device sits.

The 3D page draws a dollhouse from this; nothing here is 3D. A room is a
rectangle on its floor in metres, x across the front of the house from left
to right as seen from the street, z from the front wall (0) towards the back.
The front yard has negative z. A room inside another (the bathroom inside the
kitchen) is simply a smaller rectangle on top; the walls are worked out in the
browser from where the rectangles meet, so the plan never lists a wall.

A floor also lists its lamps: the light fittings in the ceiling (or on the
fence), each a point on the plan. A lamp lights up while the device that
switches it is on. Which device that is gets guessed in the browser -- a
"light" whose room name matches the lamp's room -- unless the operator ties
the lamp to a device here, in the lamp bindings.

Kept in a JSON file beside the labels and groups, for the same reason: the
Pi-only deployment has no database. Until the operator saves a plan of their
own the page shows DEFAULT, worded in the viewer's language, and device
placements are guessed from each device's room name.
"""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Any

from app.i18n import translate
from app.labels import clean

log = logging.getLogger(__name__)

#: Caps so a misbehaving client cannot fill the disk or stall a phone.
MAX_FLOORS = 4
MAX_ROOMS = 40
MAX_PAIRS = 80
MAX_EXTRAS = 10
MAX_PLACEMENTS = 1000
MAX_LAMPS = 60
#: Metres. A plot bigger than this is not a house.
EXTENT = 60.0

INDOOR = ("living", "bed", "bath", "kitchen", "hall", "room")
OUTDOOR = ("yard", "carport")
KINDS = INDOOR + OUTDOOR
#: Which way a staircase climbs, as an axis direction.
CLIMB = ("+x", "-x", "+z", "-z")

#: Words that say what kind of room a device's room name means, for placing
#: a device whose room name matches no room on the plan outright: "ห้องนอน
#: ชั้นบน" lands in an upstairs bedroom. First match wins, so the more
#: particular kinds come first. Sent to the browser with the plan; matching
#: runs there. Lower case, no spaces: names are compared that way.
ROOM_WORDS: list[tuple[str, list[str]]] = [
    ("carport", ["จอดรถ", "โรงรถ", "garage", "carport", "parking"]),
    ("yard", ["หน้าบ้าน", "รั้ว", "สวน", "ประตูบ้าน", "นอกบ้าน",
              "front", "yard", "garden", "gate", "fence", "outdoor", "outside", "porch"]),
    ("bath", ["ห้องน้ำ", "ส้วม", "bath", "toilet", "wc", "restroom", "shower"]),
    ("kitchen", ["ครัว", "kitchen"]),
    ("bed", ["นอน", "bed"]),
    ("living", ["นั่งเล่น", "รับแขก", "living", "lounge", "family"]),
    ("hall", ["บันได", "โถง", "ทางเดิน", "hall", "stair", "corridor"]),
]
#: Words that put a device on the upper floors, or on the ground floor.
UPPER_WORDS = ["ชั้นบน", "ชั้น2", "upstairs", "2ndfloor", "secondfloor"]
LOWER_WORDS = ["ชั้นล่าง", "ชั้น1", "downstairs", "groundfloor"]


def default_layout(lang: str) -> dict[str, Any]:
    """The house as the operator described it, sizes guessed: a two-storey
    detached house 8 m wide and 10.5 m deep with a carport in front.

    Ground floor: two living rooms one behind the other, the whole width of
    the house, with no wall between them; the kitchen across the back with a
    bathroom in its corner; stairs along the back of the second living room.
    Upper floor: a bedroom front and back, and between them a bathroom with
    a door into each, beside the stair hall.
    """
    return {
        "floors": [
            {
                "id": "f1",
                "name": translate(lang, "ชั้น 1"),
                "height": 2.7,
                "rooms": [
                    {"id": "living1", "name": translate(lang, "ห้องนั่งเล่น 1"), "kind": "living",
                     "x": 0, "z": 0, "w": 8.0, "d": 3.5},
                    {"id": "living2", "name": translate(lang, "ห้องนั่งเล่น 2"), "kind": "living",
                     "x": 0, "z": 3.5, "w": 8.0, "d": 3.5},
                    {"id": "kitchen", "name": translate(lang, "ห้องครัว"), "kind": "kitchen",
                     "x": 0, "z": 7.0, "w": 8.0, "d": 3.5},
                    {"id": "bath1", "name": translate(lang, "ห้องน้ำชั้นล่าง"), "kind": "bath",
                     "x": 5.9, "z": 8.4, "w": 2.1, "d": 2.1},
                    {"id": "yard", "name": translate(lang, "หน้าบ้าน"), "kind": "yard",
                     "x": 0, "z": -6.5, "w": 8.0, "d": 6.5},
                    {"id": "carport", "name": translate(lang, "ที่จอดรถ"), "kind": "carport",
                     "x": 0.3, "z": -6.0, "w": 3.4, "d": 5.6},
                ],
                "open": [["living1", "living2"]],
                # A door goes in the middle of the wall two rooms share: the
                # front door just right of the carport, the kitchen door just
                # beside the foot of the stairs.
                "doors": [["living1", "yard"], ["living2", "kitchen"], ["kitchen", "bath1"]],
                "stairs": [{"x": 4.6, "z": 6.0, "w": 3.4, "d": 1.0, "up": "+x"}],
                "fences": [{"x0": 0, "x1": 8.0, "z": -6.5, "gates": [[0.3, 3.7]]}],
                # One on the fence by the gate, one per living room, two in the
                # kitchen and one in its bathroom.
                "lamps": [
                    {"id": "fence", "x": 3.7, "z": -6.5},
                    {"id": "living1", "x": 4.0, "z": 1.75},
                    {"id": "living2", "x": 4.0, "z": 5.0},
                    {"id": "kitchen-a", "x": 2.0, "z": 8.75},
                    {"id": "kitchen-b", "x": 4.4, "z": 8.75},
                    {"id": "bath1", "x": 6.95, "z": 9.45},
                ],
            },
            {
                "id": "f2",
                "name": translate(lang, "ชั้น 2"),
                "height": 2.7,
                "rooms": [
                    {"id": "bed1", "name": translate(lang, "ห้องนอน 1"), "kind": "bed",
                     "x": 0, "z": 0, "w": 8.0, "d": 4.8},
                    {"id": "bath2", "name": translate(lang, "ห้องน้ำชั้นบน"), "kind": "bath",
                     "x": 0, "z": 4.8, "w": 3.0, "d": 2.2},
                    {"id": "hall", "name": translate(lang, "โถงบันได"), "kind": "hall",
                     "x": 3.0, "z": 4.8, "w": 5.0, "d": 2.2},
                    {"id": "bed2", "name": translate(lang, "ห้องนอน 2"), "kind": "bed",
                     "x": 0, "z": 7.0, "w": 8.0, "d": 3.5},
                ],
                "open": [],
                "doors": [["bath2", "bed1"], ["bath2", "bed2"], ["hall", "bed1"], ["hall", "bed2"]],
                "stairs": [],
                "fences": [],
                # Two in each bedroom, one in the bathroom between them.
                "lamps": [
                    {"id": "bed1-a", "x": 2.7, "z": 2.4},
                    {"id": "bed1-b", "x": 5.3, "z": 2.4},
                    {"id": "bath2", "x": 1.5, "z": 5.9},
                    {"id": "bed2-a", "x": 2.7, "z": 8.75},
                    {"id": "bed2-b", "x": 5.3, "z": 8.75},
                ],
            },
        ]
    }


# ------------------------------------------------------------- validation


def _number(value: Any, what: str, lo: float = -EXTENT, hi: float = EXTENT) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("%s must be a number" % what)
    if not lo <= value <= hi:
        raise ValueError("%s must be between %g and %g" % (what, lo, hi))
    # Centimetres are as fine as a plan drawn by hand gets.
    return round(float(value), 2)


def _ident(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 40:
        raise ValueError("%s needs an id of 1-40 characters" % what)
    if not all(ch.isalnum() or ch in "-_" for ch in value):
        raise ValueError("%s id may only hold letters, digits, - and _" % what)
    return value


def _name(value: Any, what: str) -> str:
    name = clean(value)
    if not name:
        raise ValueError("%s needs a name" % what)
    return name


def _list(value: Any, what: str, limit: int) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("%s must be a list" % what)
    if len(value) > limit:
        raise ValueError("at most %d %s" % (limit, what))
    return value


def _room(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("a room must be an object")
    room_id = _ident(raw.get("id"), "a room")
    # Messages name the room as the operator does; the id means nothing to them.
    label = '"%s"' % (clean(raw.get("name")) or room_id)
    kind = raw.get("kind", "room")
    if kind not in KINDS:
        raise ValueError("room %s: kind must be one of %s" % (label, ", ".join(KINDS)))
    return {
        "id": room_id,
        "name": _name(raw.get("name"), "room " + room_id),
        "kind": kind,
        "x": _number(raw.get("x"), "room %s x" % label),
        "z": _number(raw.get("z"), "room %s z" % label),
        "w": _number(raw.get("w"), "room %s width" % label, 0.5, EXTENT),
        "d": _number(raw.get("d"), "room %s depth" % label, 0.5, EXTENT),
    }


def _pairs(value: Any, what: str, rooms: set[str]) -> list[list[str]]:
    out: list[list[str]] = []
    seen: set[frozenset[str]] = set()
    for pair in _list(value, what, MAX_PAIRS):
        if (not isinstance(pair, list) or len(pair) != 2 or pair[0] == pair[1]
                or not all(isinstance(r, str) and r in rooms for r in pair)):
            raise ValueError("each of %s must name two different rooms of its floor" % what)
        key = frozenset(pair)
        if key not in seen:
            seen.add(key)
            out.append([pair[0], pair[1]])
    return out


def _stairs(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("a staircase must be an object")
    up = raw.get("up", "+z")
    if up not in CLIMB:
        raise ValueError("a staircase climbs one of %s" % ", ".join(CLIMB))
    return {
        "x": _number(raw.get("x"), "stairs x"),
        "z": _number(raw.get("z"), "stairs z"),
        "w": _number(raw.get("w"), "stairs width", 0.5, 20),
        "d": _number(raw.get("d"), "stairs depth", 0.5, 20),
        "up": up,
    }


def _fence(raw: Any) -> dict[str, Any]:
    """A straight fence along x at depth z, with gaps (gates) along it."""
    if not isinstance(raw, dict):
        raise ValueError("a fence must be an object")
    x0 = _number(raw.get("x0"), "fence x0")
    x1 = _number(raw.get("x1"), "fence x1")
    if x1 <= x0:
        raise ValueError("a fence's x1 must be past its x0")
    gates = []
    for gate in _list(raw.get("gates"), "gates", MAX_EXTRAS):
        if not isinstance(gate, list) or len(gate) != 2:
            raise ValueError("a gate is [from, to]")
        a, b = _number(gate[0], "gate start"), _number(gate[1], "gate end")
        if not x0 <= a < b <= x1:
            raise ValueError("a gate must lie along its fence")
        gates.append([a, b])
    return {"x0": x0, "x1": x1, "z": _number(raw.get("z"), "fence z"), "gates": gates}


def _lamps(value: Any) -> list[dict[str, Any]]:
    lamps, ids = [], set()
    for raw in _list(value, "lamps", MAX_LAMPS):
        if not isinstance(raw, dict):
            raise ValueError("a lamp must be an object")
        lamp_id = _ident(raw.get("id"), "a lamp")
        if lamp_id in ids:
            raise ValueError("lamp id %s is used twice" % lamp_id)
        ids.add(lamp_id)
        lamps.append({"id": lamp_id, "x": _number(raw.get("x"), "lamp x"), "z": _number(raw.get("z"), "lamp z")})
    return lamps


def validate_layout(raw: Any) -> dict[str, Any]:
    """A clean copy of a plan, or ValueError saying what is wrong with it."""
    if not isinstance(raw, dict):
        raise ValueError("the layout must be an object")
    floors_raw = _list(raw.get("floors"), "floors", MAX_FLOORS)
    if not floors_raw:
        raise ValueError("the layout needs at least one floor")
    floors, floor_ids = [], set()
    for raw_floor in floors_raw:
        if not isinstance(raw_floor, dict):
            raise ValueError("a floor must be an object")
        floor_id = _ident(raw_floor.get("id"), "a floor")
        if floor_id in floor_ids:
            raise ValueError("floor id %s is used twice" % floor_id)
        floor_ids.add(floor_id)
        rooms = [_room(r) for r in _list(raw_floor.get("rooms"), "rooms", MAX_ROOMS)]
        room_ids = [r["id"] for r in rooms]
        if len(set(room_ids)) != len(room_ids):
            raise ValueError("floor %s has two rooms with the same id" % floor_id)
        floors.append({
            "id": floor_id,
            "name": _name(raw_floor.get("name"), "floor " + floor_id),
            "height": _number(raw_floor.get("height", 2.7), "floor height", 2.0, 6.0),
            "rooms": rooms,
            "open": _pairs(raw_floor.get("open"), "open", set(room_ids)),
            "doors": _pairs(raw_floor.get("doors"), "doors", set(room_ids)),
            "stairs": [_stairs(s) for s in _list(raw_floor.get("stairs"), "stairs", MAX_EXTRAS)],
            "fences": [_fence(f) for f in _list(raw_floor.get("fences"), "fences", MAX_EXTRAS)],
            "lamps": _lamps(raw_floor.get("lamps")),
        })
    return {"floors": floors}


def lamp_key(value: Any) -> str:
    """"<floor id>:<lamp id>", the name a lamp's binding is kept under."""
    floor, sep, lamp = str(value).partition(":")
    if not sep:
        raise ValueError("a lamp is named <floor>:<lamp>")
    return _ident(floor, "a lamp's floor") + ":" + _ident(lamp, "a lamp")


def validate_binding(value: Any) -> str | None:
    """A device id, or None for "no device: never light up"."""
    if value is None:
        return None
    if not isinstance(value, str) or not 0 < len(value) <= 200:
        raise ValueError("'device' must be a device id or null")
    return value


def validate_placement(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("a placement is {floor, x, z}")
    return {
        "floor": _ident(raw.get("floor"), "a placement's floor"),
        "x": _number(raw.get("x"), "x"),
        "z": _number(raw.get("z"), "z"),
    }


# ------------------------------------------------------------------ store


class HouseStore:
    def __init__(self, path: str | os.PathLike[str] | None) -> None:
        self.path = Path(path) if path else None
        #: None until the operator saves a plan: DEFAULT is shown meanwhile.
        self._layout: dict[str, Any] | None = None
        self._placements: dict[str, dict[str, Any]] = {}
        #: "<floor>:<lamp>" -> device id, or None for a lamp tied to nothing.
        #: A lamp missing here follows whichever device the browser guesses.
        self._lamps: dict[str, str | None] = {}
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("not an object")
        except (OSError, ValueError) as exc:
            # A corrupt file must not stop the dashboard from starting.
            self.last_error = str(exc)
            log.warning("ignoring unreadable house file %s: %s", self.path, exc)
            return
        if raw.get("layout") is not None:
            try:
                self._layout = validate_layout(raw["layout"])
            except ValueError as exc:
                log.warning("ignoring the stored house plan: %s", exc)
        placements = raw.get("placements")
        for device_id, item in (placements.items() if isinstance(placements, dict) else []):
            try:
                if isinstance(device_id, str) and 0 < len(device_id) <= 200:
                    self._placements[device_id] = validate_placement(item)
            except ValueError as exc:
                log.warning("skipping the stored place of %s: %s", device_id, exc)
            if len(self._placements) >= MAX_PLACEMENTS:
                break
        lamps = raw.get("lamps")
        for key, device in (lamps.items() if isinstance(lamps, dict) else []):
            try:
                self._lamps[lamp_key(key)] = validate_binding(device)
            except ValueError as exc:
                log.warning("skipping the stored binding of lamp %s: %s", key, exc)
            if len(self._lamps) >= MAX_PLACEMENTS:
                break
        log.info("loaded the house plan (%s) and %d device places from %s",
                 "custom" if self._layout else "default", len(self._placements), self.path)

    def view(self, lang: str) -> dict[str, Any]:
        return {
            "layout": self._layout or default_layout(lang),
            "custom": self._layout is not None,
            "placements": {k: dict(v) for k, v in self._placements.items()},
            "lamps": dict(self._lamps),
            "editable": self.enabled,
            "words": {"kinds": ROOM_WORDS, "upper": UPPER_WORDS, "lower": LOWER_WORDS},
        }

    def set_layout(self, raw: Any) -> None:
        layout = validate_layout(raw)
        self._commit(layout, self._placements, self._lamps)

    def reset_layout(self) -> None:
        self._commit(None, self._placements, self._lamps)

    def bind_lamp(self, key: Any, device: Any) -> None:
        key, device = lamp_key(key), validate_binding(device)
        if key not in self._lamps and len(self._lamps) >= MAX_PLACEMENTS:
            raise ValueError("at most %d lamp bindings" % MAX_PLACEMENTS)
        self._commit(self._layout, self._placements, {**self._lamps, key: device})

    def unbind_lamp(self, key: Any) -> None:
        """Back to the browser's guess."""
        key = lamp_key(key)
        if key not in self._lamps:
            return
        lamps = dict(self._lamps)
        del lamps[key]
        self._commit(self._layout, self._placements, lamps)

    def place(self, device_id: str, raw: Any) -> dict[str, Any]:
        if not device_id or len(device_id) > 200:
            raise ValueError("unknown device")
        placement = validate_placement(raw)
        if device_id not in self._placements and len(self._placements) >= MAX_PLACEMENTS:
            raise ValueError("at most %d placed devices" % MAX_PLACEMENTS)
        self._commit(self._layout, {**self._placements, device_id: placement}, self._lamps)
        return placement

    def unplace(self, device_id: str) -> None:
        if device_id not in self._placements:
            return
        placements = dict(self._placements)
        del placements[device_id]
        self._commit(self._layout, placements, self._lamps)

    def _commit(self, layout: dict[str, Any] | None, placements: dict[str, dict[str, Any]],
                lamps: dict[str, str | None]) -> None:
        # Disk first: a change that never reached the file must not linger in
        # memory, where it would show everywhere until a restart undid it.
        self._save(layout, placements, lamps)
        self._layout, self._placements, self._lamps = layout, placements, lamps

    def _save(self, layout: dict[str, Any] | None, placements: dict[str, dict[str, Any]],
              lamps: dict[str, str | None]) -> None:
        if self.path is None:
            raise OSError("house storage is disabled")
        payload = json.dumps(
            {"version": 1, "layout": layout, "placements": placements, "lamps": lamps},
            ensure_ascii=False, indent=2,
        )
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(payload, encoding="utf-8")
            # Replace, never truncate-in-place: a power cut mid-write on a Pi
            # would otherwise leave an empty file and lose the plan.
            os.replace(temp, self.path)
            self.last_error = None
        except OSError as exc:
            self.last_error = str(exc)
            log.warning("could not write %s: %s", self.path, exc)
            raise
