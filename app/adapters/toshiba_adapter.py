"""Toshiba Home AC Control adapter -- air conditioners through Toshiba's cloud.

Also the way into Carrier units sold in Thailand with built-in Wi-Fi (the
"Carrier In The Air" app): they run on the same service, and the same login
works in Toshiba's app.

How it talks, via the `toshiba-ac` library: log in with the phone app's
account, register this dashboard as one more "mobile device" on it, and hold
an Azure IoT Hub connection that the AC pushes its state over and takes
commands from. No polling -- a change made on the remote or in the phone app
arrives here within a second or so, and the phone app keeps working exactly
as before.

Not an official API. It is what the phone app speaks, worked out from the
outside, so Toshiba can change it under us; and it needs the internet, so
unlike Matter it stops when the connection to the house does. Both are why
this is a separate adapter beside Matter rather than a replacement for it.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from toshiba_ac.device import ToshibaAcDevice, ToshibaAcDeviceError
from toshiba_ac.device.properties import (
    ToshibaAcFanMode,
    ToshibaAcMode,
    ToshibaAcStatus,
    ToshibaAcSwingMode,
)
from toshiba_ac.device_manager import ToshibaAcDeviceManager
from toshiba_ac.utils.http_api import ToshibaAcHttpApiAuthError, ToshibaAcHttpApiRateLimitError

from app.adapters.base import StateSink, StatusSink
from app.config import settings
from app.models import Capability, Command, CommandError, Device, StateEvent

log = logging.getLogger(__name__)

# The library logs every state change at INFO and the Azure SDK narrates its
# connection; on a Pi writing to an SD card neither is worth keeping.
for _noisy in ("toshiba_ac", "azure.iot.device"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

CAPABILITIES = [
    Capability.SWITCH,
    Capability.HVAC_MODE,
    Capability.TARGET_TEMPERATURE,
    Capability.FAN_MODE,
    Capability.SWING_MODE,
    Capability.TEMPERATURE,
    Capability.OUTDOOR_TEMPERATURE,
]

#: Library enum member name -> the word the rest of the app uses.
MODE_WORDS = {"AUTO": "auto", "COOL": "cool", "HEAT": "heat", "DRY": "dry", "FAN": "fan"}
FAN_WORDS = {
    "AUTO": "auto", "QUIET": "quiet", "LOW": "low", "MEDIUM_LOW": "medium_low",
    "MEDIUM": "medium", "MEDIUM_HIGH": "medium_high", "HIGH": "high",
}
SWING_WORDS = {
    "OFF": "off", "SWING_VERTICAL": "vertical", "SWING_HORIZONTAL": "horizontal",
    "SWING_VERTICAL_AND_HORIZONTAL": "both",
    "FIXED_1": "fixed_1", "FIXED_2": "fixed_2", "FIXED_3": "fixed_3",
    "FIXED_4": "fixed_4", "FIXED_5": "fixed_5",
}

#: How often to look at the cloud connection. The SDK reconnects by itself;
#: this only mirrors its state onto the dashboard.
WATCH_SECONDS = 30.0
#: Down this long, and the session is rebuilt from a fresh login -- the
#: cloud may have forgotten our registration, or the token may be stale.
REBUILD_AFTER_SECONDS = 600.0
#: A wrong password retried every few seconds is how an account gets locked.
AUTH_RETRY_SECONDS = 900.0
#: Toshiba answers too many logins with 429 (or 403). Every retry counts as
#: another login, so trying again soon only keeps the block going -- and it
#: blocks the phone app's logins on the same account too.
RATE_LIMIT_RETRY_SECONDS = 900.0
#: ...and doubled for each 429 in a row, up to this. Toshiba does not say how
#: long its block lasts; if it counts logins over a window, a steady retry
#: every 15 minutes -- three logins each, with the library's own retries --
#: can keep it in place for good.
RATE_LIMIT_MAX_SECONDS = 4 * 3600.0
MAX_BACKOFF_SECONDS = 600.0


class ToshibaAdapter:
    name = "toshiba"
    on_devices_changed = None

    def __init__(self) -> None:
        self._username = settings.toshiba_username
        self._password = settings.toshiba_password
        self._state_path = Path(settings.toshiba_state_path) if settings.toshiba_state_path else None
        self._manager: ToshibaAcDeviceManager | None = None
        self._acs: dict[str, ToshibaAcDevice] = {}
        self._devices: dict[str, Device] = {}
        self._on_state: StateSink | None = None
        self._on_status: StatusSink | None = None
        self._task: asyncio.Task | None = None
        self._connected = False
        #: device id -> when that AC last said anything to the cloud (epoch).
        #: Toshiba never announces an adapter dropping off -- it just goes
        #: quiet -- so silence is the only sign there is.
        self._last_heard: dict[str, float] = {}
        self._offline_after = max(5.0, settings.toshiba_offline_minutes) * 60

    @property
    def configured(self) -> bool:
        return bool(self._username and self._password)

    # ------------------------------------------------------------------ life

    async def start(self, on_state: StateSink, on_status: StatusSink) -> None:
        self._on_state, self._on_status = on_state, on_status
        if not self.configured:
            log.warning("toshiba adapter is not configured (TOSHIBA_USERNAME/PASSWORD); staying idle")
            await on_status(False)
            return
        # Connecting takes a login, a registration and an IoT Hub handshake --
        # seconds over the internet. Never hold up the dashboard's start for it.
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._teardown()

    async def discover(self) -> list[Device]:
        return list(self._devices.values())

    # ------------------------------------------------------------ connection

    async def _run(self) -> None:
        backoff = 10.0
        limited = 0     # 429s in a row
        while True:
            wait = backoff
            try:
                await self._connect()
                backoff, limited = 10.0, 0
                await self._watch()
                log.warning("toshiba cloud connection stayed down; logging in again")
            except asyncio.CancelledError:
                raise
            except ToshibaAcHttpApiAuthError as exc:
                log.error("toshiba login refused (%s); check TOSHIBA_USERNAME/PASSWORD", exc)
                self._forget_token()
                wait = AUTH_RETRY_SECONDS
            except Exception as exc:  # noqa: BLE001 - any failure means "try again later"
                if rate_limited(exc):
                    limited += 1
                    wait = min(RATE_LIMIT_RETRY_SECONDS * 2 ** (limited - 1), RATE_LIMIT_MAX_SECONDS)
                    log.warning(
                        "toshiba is limiting logins (%s, %d in a row); next try in %d minutes, at %s",
                        exc, limited, wait // 60, time.strftime("%H:%M", time.localtime(time.time() + wait)),
                    )
                else:
                    log.warning("toshiba cloud unavailable: %s", exc)
                    backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            await self._teardown()
            await self._set_connected(False)
            await asyncio.sleep(wait)

    async def _connect(self) -> None:
        # A saved session skips /api/Consumer/Login, the endpoint Toshiba
        # limits hardest: a restart then costs no login at all. A stale token
        # gets a 401, and the library logs in again by itself.
        saved = self._read_state()
        same_account = saved.get("token_user") == self._username
        self._manager = ToshibaAcDeviceManager(
            self._username,
            self._password,
            self._client_id(),
            access_token=saved.get("access_token") if same_account else None,
            access_token_type=saved.get("access_token_type") if same_account else None,
            consumer_id=saved.get("consumer_id") if same_account else None,
        )
        self._manager.on_access_token_updated_callback.add(self._token_updated)
        await self._manager.connect()
        acs = await self._manager.get_devices()

        self._acs, self._devices = {}, {}
        for ac in acs:
            device_id = "toshiba:" + ac.ac_unique_id
            self._acs[device_id] = ac
            self._devices[device_id] = Device(
                id=device_id,
                name=ac.name or "Air conditioner",
                room="Unassigned",
                adapter=self.name,
                native_id=ac.ac_unique_id,
                kind="ac",
                capabilities=list(CAPABILITIES),
                choices={
                    Capability.HVAC_MODE: _words(ac.supported.ac_mode, MODE_WORDS),
                    Capability.FAN_MODE: _words(ac.supported.ac_fan_mode, FAN_WORDS),
                    Capability.SWING_MODE: _words(ac.supported.ac_swing_mode, SWING_WORDS),
                },
            )
            ac.on_state_changed_callback.add(self._state_changed)
        self._listen_for_messages()
        await self._read_last_contact(acs)
        log.info("toshiba cloud connected: %d air conditioner(s)", len(acs))

        await self._set_connected(True)
        for ac in acs:
            await self._state_changed(ac)

    def _listen_for_messages(self) -> None:
        """Note the time of everything each AC pushes -- state and heartbeats
        alike -- before the library handles it. A heartbeat that changes
        nothing never reaches _state_changed, yet it is proof of life.

        The SDK may call these from its own thread: keep it to one dict write.
        """
        amqp = getattr(self._manager, "amqp_api", None)
        handlers = getattr(amqp, "handlers", None)
        if not isinstance(handlers, dict):
            return
        for command, original in list(handlers.items()):
            def heard(source_id, message_id, target_id, payload, timestamp, _original=original):
                self._last_heard["toshiba:" + str(source_id)] = time.time()
                return _original(source_id, message_id, target_id, payload, timestamp)
            handlers[command] = heard

    async def _read_last_contact(self, acs: list[ToshibaAcDevice]) -> None:
        """When each AC last talked to the cloud, as the cloud has it.

        This is what tells a unit unplugged weeks ago from a quiet one at
        start-up, before there has been time to notice any silence. One
        request per AC, once per connection. Any doubt counts as "just
        heard": better to show a lost AC late than a working one as lost.
        """
        http = getattr(self._manager, "http_api", None)
        now = time.time()
        for ac in acs:
            device_id = "toshiba:" + ac.ac_unique_id
            self._last_heard[device_id] = now
            try:
                record = await http.request_api(
                    http.AC_STATE_PATH, get={"ACId": ac.ac_id, "consumerId": http.consumer_id})
                last = _epoch(record.get("LastConnectionTime"))
            except Exception as exc:  # noqa: BLE001 - liveness is best effort
                log.warning("could not read when %s last connected: %s", ac.name, exc)
                continue
            if last is not None:
                self._last_heard[device_id] = min(now, last)
                if now - last > self._offline_after:
                    log.warning("%s last reached Toshiba's cloud at %s; showing it as offline",
                                ac.name, record.get("LastConnectionTime"))

    def _reachable(self, device_id: str) -> bool:
        heard = self._last_heard.get(device_id)
        return heard is None or time.time() - heard <= self._offline_after

    async def _watch(self) -> None:
        """Mirror the SDK's connection state until it has been down too long."""
        down_since: float | None = None
        while True:
            await asyncio.sleep(WATCH_SECONDS)
            client = getattr(getattr(self._manager, "amqp_api", None), "device", None)
            up = bool(client is not None and client.connected)
            await self._set_connected(up)
            await self._refresh_online()
            if up:
                down_since = None
            elif down_since is None:
                down_since = time.monotonic()
            elif time.monotonic() - down_since > REBUILD_AFTER_SECONDS:
                return

    async def _teardown(self) -> None:
        manager, self._manager = self._manager, None
        for ac in self._acs.values():
            ac.on_state_changed_callback.remove(self._state_changed)
        if manager is not None:
            with contextlib.suppress(Exception):
                await manager.shutdown()

    async def _set_connected(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        if self._on_status is not None:
            await self._on_status(connected)
        await self._refresh_online(force=True)

    async def _refresh_online(self, force: bool = False) -> None:
        """An AC is online while the cloud connection is up and the AC itself
        has been heard from lately. Tell the hub only when that changed."""
        changed = force
        for device_id, device in self._devices.items():
            online = self._connected and self._reachable(device_id)
            if online != device.online:
                device.online = online
                changed = True
                log.info("%s is %s", device.name, "back online" if online else "offline (no word from it)")
        if changed and self.on_devices_changed is not None:
            await self.on_devices_changed()

    def _client_id(self) -> str:
        """Our own id among the account's "mobile devices", kept across restarts.

        The library falls back to one fixed id for everyone, so this dashboard
        and, say, a Home Assistant on the same account would keep knocking
        each other off the connection.
        """
        stored = self._read_state().get("client_id")
        if isinstance(stored, str) and stored:
            return stored
        client_id = secrets.token_hex(8)
        if self._state_path is None:
            log.warning("TOSHIBA_STATE_PATH is empty; registering under a new id every start")
            return client_id
        self._write_state(client_id=client_id)
        return client_id

    async def _token_updated(self, values: tuple[str, str, str]) -> None:
        """A login succeeded: keep the session for the next start."""
        access_token, token_type, consumer_id = values
        self._write_state(
            access_token=access_token, access_token_type=token_type,
            consumer_id=consumer_id, token_user=self._username,
        )

    def _forget_token(self) -> None:
        self._write_state(access_token=None, access_token_type=None, consumer_id=None, token_user=None)

    def _write_state(self, **changes: Any) -> None:
        """Merge `changes` into the state file. It now holds a login session,
        so it is written readable by the app's user alone -- as auth.json is."""
        if self._state_path is None:
            return
        data = {**self._read_state(), "version": 1, **changes}
        data = {k: v for k, v in data.items() if v is not None}
        temp = self._state_path.with_name(self._state_path.name + ".tmp")
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.chmod(temp, 0o600)
            os.replace(temp, self._state_path)
        except OSError as exc:
            log.warning("could not save %s: %s", self._state_path, exc)

    def _read_state(self) -> dict[str, Any]:
        if self._state_path is None:
            return {}
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable %s: %s", self._state_path, exc)
            return {}
        return data if isinstance(data, dict) else {}

    # ----------------------------------------------------------------- state

    async def _state_changed(self, ac: ToshibaAcDevice) -> None:
        """Publish everything the AC reports. The hub drops repeats."""
        if self._on_state is None:
            return
        device_id = "toshiba:" + ac.ac_unique_id
        if device_id not in self._devices:
            return
        status = ac.ac_status
        readings: list[tuple[Capability, Any]] = [
            (Capability.SWITCH, None if status is ToshibaAcStatus.NONE else status is ToshibaAcStatus.ON),
            (Capability.HVAC_MODE, MODE_WORDS.get(ac.ac_mode.name)),
            (Capability.TARGET_TEMPERATURE, ac.ac_temperature),
            (Capability.FAN_MODE, FAN_WORDS.get(ac.ac_fan_mode.name)),
            (Capability.SWING_MODE, SWING_WORDS.get(ac.ac_swing_mode.name)),
            (Capability.TEMPERATURE, ac.ac_indoor_temperature),
            (Capability.OUTDOOR_TEMPERATURE, ac.ac_outdoor_temperature),
        ]
        for cap, value in readings:
            if value is not None:
                await self._on_state(StateEvent(device_id, cap, value))

    # ---------------------------------------------------------------- writes

    async def execute(self, command: Command) -> None:
        ac = self._acs.get(command.device_id)
        if ac is None:
            raise CommandError("air conditioner is not known to Toshiba's cloud")
        if not self._connected:
            raise CommandError("not connected to Toshiba's cloud")
        if not self._reachable(command.device_id):
            # The cloud would take it and the AC would never hear it; say so
            # now rather than after the confirmation times out.
            raise CommandError("%s has not reached Toshiba's cloud lately (power or Wi-Fi off?)" % ac.name)
        cap, value = command.capability, command.value
        try:
            if cap is Capability.SWITCH:
                await ac.set_ac_status(ToshibaAcStatus.ON if value else ToshibaAcStatus.OFF)
            elif cap is Capability.HVAC_MODE:
                await ac.set_ac_mode(_member(ToshibaAcMode, MODE_WORDS, value))
            elif cap is Capability.TARGET_TEMPERATURE:
                await ac.set_ac_temperature(int(value))
            elif cap is Capability.FAN_MODE:
                await ac.set_ac_fan_mode(_member(ToshibaAcFanMode, FAN_WORDS, value))
            elif cap is Capability.SWING_MODE:
                await ac.set_ac_swing_mode(_member(ToshibaAcSwingMode, SWING_WORDS, value))
            else:
                raise CommandError("capability " + cap.value + " is not writable over Toshiba")
        except CommandError:
            raise
        except (ToshibaAcDeviceError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - a dropped connection, a timeout
            raise CommandError("Toshiba's cloud did not take the command: %s" % exc) from exc


def rate_limited(exc: BaseException) -> bool:
    """Is this Toshiba saying "too many requests"? The library raises its
    rate-limit error for 403 only; a 429 comes as a plain API error."""
    return isinstance(exc, ToshibaAcHttpApiRateLimitError) or "HTTP 429" in str(exc)


def _epoch(stamp: Any) -> float | None:
    """'2026-10-07T15:28:48.967Z' -> seconds since the epoch, or None."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo else None


def _words(members: list[Enum], words: dict[str, str]) -> list[str]:
    """The app's words for the members a model supports, in the app's order."""
    names = {m.name for m in members}
    return [word for name, word in words.items() if name in names]


def _member(enum: type[Enum], words: dict[str, str], word: Any) -> Enum:
    for name, w in words.items():
        if w == word:
            return enum[name]
    raise ValueError("unknown value %r" % word)
