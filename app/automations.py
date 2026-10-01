"""Automations: when -> only if -> then, run inside the dashboard process.

Built to cost a Pi 3B next to nothing:

- No scheduler library and no extra process. One asyncio task wakes at the top
  of each minute to see whether a time rule is due -- 1440 wake-ups a day,
  each a loop over a handful of rules.
- Device rules ride on the state events the hub already receives; a report
  that no rule watches costs one dictionary lookup.
- Rules live in a small JSON file (AUTOMATIONS_PATH), written only when one
  is changed. The run log and run counts are kept in memory: writing the SD
  card on every run would wear it for a convenience.

A device rule fires on the *transition* into its condition -- the window
opening, not every report that it is still open -- so a sensor's heartbeat
does not re-run it, and a rule whose action flips the very state it watches
cannot chase its own tail. Rules triggering each other in a cycle are caught
by a per-rule rate limit.

Time rules hold off until the clock is plausible: a Pi 3B has no real-time
clock, and after a power cut it can boot believing it is 1970 until NTP
answers.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import secrets
import time
from collections import deque
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from app.labels import clean
from app.models import WRITABLE, Capability, CommandError, StateEvent

log = logging.getLogger(__name__)

MAX_RULES = 50
MAX_CONDITIONS = 5
MAX_ACTIONS = 10
#: Longest "wait" an action list may hold, in seconds.
MAX_DELAY = 3600
#: A rule that fires more often than this is caught in a loop with another.
RATE_RUNS, RATE_WINDOW = 6, 60.0
LOG_SIZE = 100
#: Before this the clock has not been set (2025-01-01T00:00Z).
CLOCK_FLOOR = 1735689600

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
OPS = ("eq", "ne", "gt", "lt")


# ------------------------------------------------------------------ validation


def _hhmm(value: Any, field: str) -> str:
    if not isinstance(value, str) or not TIME_RE.match(value):
        raise ValueError("%s must be a time like 22:30" % field)
    return value


def _days(value: Any) -> list[int]:
    """Weekdays, Monday = 0. Empty means every day."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(d, int) and 0 <= d <= 6 for d in value):
        raise ValueError("days must be a list of weekdays 0 (Monday) to 6 (Sunday)")
    return sorted(set(value))


def _device_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValueError("a device id is needed")
    return value


def _number_or_bool(value: Any) -> bool | float:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    raise ValueError("a value must be true, false or a number")


def _test(body: Any, field: str) -> dict[str, Any]:
    """A device test: {device, capability, op, value}."""
    if not isinstance(body, dict):
        raise ValueError(field + " must be an object")
    try:
        cap = Capability(body.get("capability"))
    except ValueError:
        raise ValueError("%s has an unknown capability %r" % (field, body.get("capability"))) from None
    op = body.get("op", "eq")
    if op not in OPS:
        raise ValueError("%s op must be one of %s" % (field, ", ".join(OPS)))
    value = _number_or_bool(body.get("value"))
    if op in ("gt", "lt") and isinstance(value, bool):
        raise ValueError(field + " compares above/below a number, not true/false")
    return {"device": _device_id(body.get("device")), "capability": cap.value, "op": op, "value": value}


def _trigger(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("a rule needs a trigger")
    kind = body.get("type")
    if kind == "time":
        return {"type": "time", "at": _hhmm(body.get("at"), "trigger time"), "days": _days(body.get("days"))}
    if kind == "device":
        return {"type": "device", **_test(body, "trigger")}
    raise ValueError("trigger type must be 'time' or 'device'")


def _condition(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("each condition must be an object")
    kind = body.get("type")
    if kind == "time_between":
        return {"type": kind, "from": _hhmm(body.get("from"), "from"), "to": _hhmm(body.get("to"), "to")}
    if kind == "weekday":
        days = _days(body.get("days"))
        if not days:
            raise ValueError("a weekday condition needs at least one day")
        return {"type": kind, "days": days}
    if kind == "device":
        return {"type": kind, **_test(body, "condition")}
    raise ValueError("condition type must be 'time_between', 'weekday' or 'device'")


def _action(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("each action must be an object")
    kind = body.get("type")
    if kind == "device":
        try:
            cap = Capability(body.get("capability"))
        except ValueError:
            raise ValueError("unknown capability %r" % body.get("capability")) from None
        if cap not in WRITABLE:
            raise ValueError("%s cannot be set" % cap.value)
        value = _number_or_bool(body.get("value"))
        if (cap is Capability.SWITCH) != isinstance(value, bool):
            raise ValueError("switch takes true/false; brightness and colour temperature take a number")
        return {"type": kind, "device": _device_id(body.get("device")), "capability": cap.value, "value": value}
    if kind == "group":
        group = body.get("group")
        if not isinstance(group, str) or not group or len(group) > 200:
            raise ValueError("a group action needs a group")
        if not isinstance(body.get("value"), bool):
            raise ValueError("a group action takes value true or false")
        return {"type": kind, "group": group, "value": body["value"]}
    if kind == "delay":
        seconds = body.get("seconds")
        if not isinstance(seconds, int) or isinstance(seconds, bool) or not 1 <= seconds <= MAX_DELAY:
            raise ValueError("a wait is 1 to %d seconds" % MAX_DELAY)
        return {"type": kind, "seconds": seconds}
    raise ValueError("action type must be 'device', 'group' or 'delay'")


def _list(value: Any, item, limit: int, what: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(what + " must be a list")
    if len(value) > limit:
        raise ValueError("at most %d %s" % (limit, what))
    return [item(v) for v in value]


def normalise(body: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    """A rule as stored, from a request body (whole, or a patch on `current`)."""
    merged = {**(current or {}), **body}
    name = clean(merged.get("name"))
    if not name:
        raise ValueError("a rule needs a name")
    actions = _list(merged.get("actions"), _action, MAX_ACTIONS, "actions")
    if not any(a["type"] != "delay" for a in actions):
        raise ValueError("a rule needs at least one thing to do")
    return {
        "id": merged.get("id") or "r-" + secrets.token_hex(4),
        "name": name,
        "enabled": bool(merged.get("enabled", True)),
        "trigger": _trigger(merged.get("trigger")),
        "conditions": _list(merged.get("conditions"), _condition, MAX_CONDITIONS, "conditions"),
        "actions": actions,
    }


def compare(op: str, actual: Any, expected: Any) -> bool:
    if actual is None:
        return False
    if op in ("gt", "lt"):
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return False
        return actual > expected if op == "gt" else actual < expected
    same = actual == expected if isinstance(expected, bool) else (
        isinstance(actual, (int, float)) and not isinstance(actual, bool) and abs(actual - expected) < 1e-9)
    return same if op == "eq" else not same


# ----------------------------------------------------------------------- store


class AutomationStore:
    def __init__(self, path: str | os.PathLike[str] | None) -> None:
        self.path = Path(path) if path else None
        self._rules: dict[str, dict[str, Any]] = {}

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            rules = raw.get("rules") if isinstance(raw, dict) else None
            if not isinstance(rules, list):
                raise ValueError("no 'rules' list")
        except (OSError, ValueError) as exc:
            # A corrupt file must not stop the dashboard from starting.
            log.warning("ignoring unreadable automation file %s: %s", self.path, exc)
            return
        for item in rules[:MAX_RULES]:
            try:
                rule = normalise(item)
                self._rules[rule["id"]] = rule
            except (TypeError, ValueError) as exc:
                log.warning("skipping a stored rule: %s", exc)
        log.info("loaded %d automation rules from %s", len(self._rules), self.path)

    def list(self) -> list[dict[str, Any]]:
        return [json.loads(json.dumps(r)) for r in self._rules.values()]

    def live(self) -> list[dict[str, Any]]:
        """The stored rules themselves, for the engine to read on every state
        event without copying them. Never modified in place: a change builds
        a new dict and swaps it in."""
        return list(self._rules.values())

    def get(self, rule_id: str) -> dict[str, Any]:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise LookupError("unknown rule " + repr(rule_id))
        return json.loads(json.dumps(rule))

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        if len(self._rules) >= MAX_RULES:
            raise ValueError("at most %d rules" % MAX_RULES)
        rule = normalise({k: v for k, v in body.items() if k != "id"})
        self._commit({**self._rules, rule["id"]: rule})
        return self.get(rule["id"])

    def update(self, rule_id: str, body: dict[str, Any]) -> dict[str, Any]:
        rule = normalise({k: v for k, v in body.items() if k != "id"}, self.get(rule_id))
        self._commit({**self._rules, rule_id: rule})
        return self.get(rule_id)

    def delete(self, rule_id: str) -> None:
        self.get(rule_id)
        rules = dict(self._rules)
        del rules[rule_id]
        self._commit(rules)

    def _commit(self, rules: dict[str, dict[str, Any]]) -> None:
        # Disk first, as for labels and groups.
        if self.path is None:
            raise OSError("automations are disabled")
        payload = json.dumps({"version": 1, "rules": list(rules.values())}, ensure_ascii=False, indent=2)
        temp = self.path.with_name(self.path.name + ".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, self.path)
        self._rules = rules


# ---------------------------------------------------------------------- engine


class AutomationEngine:
    def __init__(self, hub: Any, store: AutomationStore, tz: tzinfo | None) -> None:
        self.hub = hub
        self.store = store
        self.tz = tz
        self._timer: asyncio.Task | None = None
        self._running: dict[str, set[asyncio.Task]] = {}
        self._recent: dict[str, deque[float]] = {}
        self._fired_at: dict[str, str] = {}
        self._log: deque[dict[str, Any]] = deque(maxlen=LOG_SIZE)
        self._stats: dict[str, dict[str, Any]] = {}
        self._clock_warned = False

    # ------------------------------------------------------------- life

    async def start(self) -> None:
        self.hub.add_listener(self.on_state)
        self._timer = asyncio.create_task(self._tick())

    async def stop(self) -> None:
        tasks = [self._timer, *(t for ts in self._running.values() for t in ts)]
        for task in tasks:
            if task is not None:
                task.cancel()
        for task in tasks:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def now(self) -> datetime:
        return datetime.now(self.tz) if self.tz else datetime.now().astimezone()

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.store.enabled,
            "rules": self.store.list(),
            "log": list(self._log),
            "stats": self._stats,
        }

    async def rules_changed(self, removed: str | None = None, changed: str | None = None) -> None:
        """A rule was saved or deleted: stop any run of it still waiting on a
        delay, so an edited rule never finishes with its old actions."""
        for rule_id in {removed, changed} - {None}:
            for task in self._running.pop(rule_id, set()):
                task.cancel()
        if removed:
            self._stats.pop(removed, None)
        await self.hub.publish({"type": "automations", **self.snapshot()})

    # ---------------------------------------------------------- triggers

    def on_state(self, event: StateEvent, previous: StateEvent | None) -> None:
        """Hub listener: a device reported. Fire device rules whose condition
        has just become true. A report with no earlier value (start-up, a
        device returning) is not a transition anyone did."""
        if previous is None:
            return
        for rule in self.store.live():
            trigger = rule["trigger"]
            if (not rule["enabled"] or trigger["type"] != "device"
                    or trigger["device"] != event.device_id
                    or trigger["capability"] != event.capability.value):
                continue
            now_true = compare(trigger["op"], event.value, trigger["value"])
            was_true = compare(trigger["op"], previous.value, trigger["value"])
            if now_true and not was_true:
                self.fire(rule, "device")

    async def _tick(self) -> None:
        """Wake at the top of every minute and run the time rules due now."""
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                await asyncio.sleep(60.5 - time.time() % 60)
                try:
                    self.due(self.now())
                except Exception as exc:  # noqa: BLE001 - the clock must keep ticking
                    log.warning("automation tick failed: %s", exc)

    def due(self, now: datetime) -> None:
        if now.timestamp() < CLOCK_FLOOR:
            if not self._clock_warned:
                log.warning("clock not set yet (%s): time rules wait for NTP", now.isoformat())
                self._clock_warned = True
            return
        stamp = now.strftime("%Y-%m-%d %H:%M")
        hhmm = now.strftime("%H:%M")
        for rule in self.store.live():
            trigger = rule["trigger"]
            if not rule["enabled"] or trigger["type"] != "time" or trigger["at"] != hhmm:
                continue
            if trigger["days"] and now.weekday() not in trigger["days"]:
                continue
            if self._fired_at.get(rule["id"]) == stamp:
                continue    # a late tick must not run it twice in one minute
            self._fired_at[rule["id"]] = stamp
            self.fire(rule, "time")

    # ------------------------------------------------------------- runs

    def fire(self, rule: dict[str, Any], why: str) -> bool:
        """Start a run of `rule`. False when the rate limit holds it back."""
        recent = self._recent.setdefault(rule["id"], deque())
        now = time.time()
        while recent and now - recent[0] > RATE_WINDOW:
            recent.popleft()
        if len(recent) >= RATE_RUNS:
            self._record(rule, why, "limited", "ran %d times in a minute; held back" % RATE_RUNS)
            return False
        recent.append(now)
        task = asyncio.create_task(self._run(rule, why))
        tasks = self._running.setdefault(rule["id"], set())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return True

    async def _run(self, rule: dict[str, Any], why: str) -> None:
        if why != "test":
            failed = self._failed_condition(rule)
            if failed:
                self._record(rule, why, "skipped", failed)
                return
        problems = []
        try:
            for action in rule["actions"]:
                if action["type"] == "delay":
                    await asyncio.sleep(action["seconds"])
                elif action["type"] == "device":
                    try:
                        await self.hub.execute(action["device"], action["capability"], action["value"])
                    except (LookupError, ValueError, CommandError) as exc:
                        problems.append(str(exc))
                else:
                    ids = self._group_members(action["group"])
                    if ids is None:
                        problems.append("unknown group " + action["group"])
                    else:
                        self.hub.switch_many(ids, action["value"])
        except asyncio.CancelledError:
            self._record(rule, why, "cancelled", "rule changed while it waited")
            raise
        self._record(rule, why, "failed" if problems else "ran", "; ".join(problems))

    def _failed_condition(self, rule: dict[str, Any]) -> str:
        """The first condition that does not hold, as its type, or ''."""
        now = self.now()
        for cond in rule["conditions"]:
            if cond["type"] == "time_between":
                hhmm = now.strftime("%H:%M")
                start, end = cond["from"], cond["to"]
                # A window can run past midnight: 22:00-06:00.
                inside = start <= hhmm < end if start <= end else (hhmm >= start or hhmm < end)
                if not inside:
                    return "time_between"
            elif cond["type"] == "weekday":
                if now.weekday() not in cond["days"]:
                    return "weekday"
            elif cond["type"] == "device":
                actual = self.hub.state_value(cond["device"], cond["capability"])
                if not compare(cond["op"], actual, cond["value"]):
                    return "device"
        return ""

    def _group_members(self, group: str) -> list[str] | None:
        """Device ids behind a group target: a made group's id, 'room:<name>',
        or 'all'."""
        devices = self.hub.described()
        if group == "all":
            return [d["id"] for d in devices]
        if group.startswith("room:"):
            room = group[len("room:"):]
            return [d["id"] for d in devices if d.get("room") == room]
        try:
            return self.hub.groups.get(group)["devices"]
        except LookupError:
            return None

    def test(self, rule_id: str) -> None:
        """Run a rule's actions now, skipping its trigger and conditions."""
        self.fire(self.store.get(rule_id), "test")

    def _record(self, rule: dict[str, Any], why: str, result: str, detail: str) -> None:
        entry = {"ts": time.time(), "rule": rule["id"], "name": rule["name"],
                 "why": why, "result": result, "detail": detail}
        self._log.appendleft(entry)
        if result in ("ran", "failed"):
            stats = self._stats.setdefault(rule["id"], {"runs": 0, "last": None})
            stats["runs"] += 1
            stats["last"] = entry["ts"]
        log.info("automation %r (%s): %s %s", rule["name"], why, result, detail)
        asyncio.get_running_loop().create_task(
            self.hub.publish({"type": "automation_run", "entry": entry, "stats": self._stats})
        )
