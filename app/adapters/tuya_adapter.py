"""Tuya Cloud adapter -- the hybrid fallback of doc §5.

Used for devices the M1 does not expose over its Matter bridge. Cloud control
is slower (0.3-2s) and rate limited, so this is deliberately the second choice.

Doc §14 constraints honoured here:
  * never poll faster than 10s per device (`tuya_poll_seconds`, floored at 10)
  * a Trial Edition project expires every 30 days -- an auth failure is
    surfaced as a clear status, not a crash loop

Event push via the Pulsar Message Service is the documented improvement over
polling; it is NOT implemented here (it needs a separate Pulsar client and a
subscription on the Tuya side). Until then this polls.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
from typing import Any

import aiohttp

from app.adapters.base import StateSink, StatusSink
from app.config import settings
from app.models import Capability, Command, CommandError, Device, StateEvent

log = logging.getLogger(__name__)

REGION_HOSTS = {
    "cn": "https://openapi.tuyacn.com",
    "us": "https://openapi.tuyaus.com",
    "eu": "https://openapi.tuyaeu.com",
    "in": "https://openapi.tuyain.com",
}

#: Tuya data-point code -> our capability. Codes vary by product; extend as you
#: meet new devices (`scripts/dump_nodes.py --tuya` prints what a device sends).
DP_TO_CAP: dict[str, Capability] = {
    "switch": Capability.SWITCH,
    "switch_1": Capability.SWITCH,
    "switch_led": Capability.SWITCH,
    "bright_value": Capability.BRIGHTNESS,
    "bright_value_v2": Capability.BRIGHTNESS,
    "temp_value": Capability.COLOR_TEMP,
    "temp_value_v2": Capability.COLOR_TEMP,
    "va_temperature": Capability.TEMPERATURE,
    "temp_current": Capability.TEMPERATURE,
    "va_humidity": Capability.HUMIDITY,
    "humidity_value": Capability.HUMIDITY,
    "bright_value_lux": Capability.ILLUMINANCE,
    "battery_percentage": Capability.BATTERY,
    "doorcontact_state": Capability.CONTACT,
    "pir": Capability.OCCUPANCY,
}

CAP_TO_DP: dict[Capability, str] = {
    Capability.SWITCH: "switch_led",
    Capability.BRIGHTNESS: "bright_value_v2",
    Capability.COLOR_TEMP: "temp_value_v2",
}

#: Tuya scales brightness/colour temperature to 10..1000.
TUYA_SCALE_MAX = 1000
#: Doc §14: the platform rate-limits well before this, so never go below it.
MIN_POLL_SECONDS = 10.0


class TuyaAdapter:
    name = "tuya"
    on_devices_changed = None

    def __init__(self) -> None:
        self._host = REGION_HOSTS.get(settings.tuya_api_region, REGION_HOSTS["us"])
        self._client_id = settings.tuya_access_id
        self._secret = settings.tuya_access_secret
        self._uid = settings.tuya_uid
        self._poll = max(MIN_POLL_SECONDS, settings.tuya_poll_seconds)
        self._session: aiohttp.ClientSession | None = None
        self._token: str = ""
        self._token_expires: float = 0.0
        self._devices: dict[str, Device] = {}
        self._native: dict[str, str] = {}
        self._on_state: StateSink | None = None
        self._on_status: StatusSink | None = None
        self._task: asyncio.Task | None = None
        self._connected = False

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._secret and self._uid)

    # ------------------------------------------------------------------ life

    async def start(self, on_state: StateSink, on_status: StatusSink) -> None:
        self._on_state, self._on_status = on_state, on_status
        if not self.configured:
            log.warning("tuya adapter is not configured (TUYA_ACCESS_ID/SECRET/UID); staying idle")
            await on_status(False)
            return
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._poll_forever())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._session:
            await self._session.close()

    async def discover(self) -> list[Device]:
        if not self.configured:
            return []
        if not self._devices:
            with contextlib.suppress(Exception):
                await self._refresh_devices()
        return list(self._devices.values())

    # -------------------------------------------------------------- polling

    async def _poll_forever(self) -> None:
        backoff = 5.0
        while True:
            try:
                await self._refresh_devices()
                await self._poll_status()
                if not self._connected:
                    self._connected = True
                    await self._emit_status(True)
                backoff = 5.0
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("tuya poll failed: %s", exc)
                if self._connected:
                    self._connected = False
                    await self._emit_status(False)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)

    async def _refresh_devices(self) -> None:
        payload = await self._request("GET", "/v1.0/users/%s/devices" % self._uid)
        devices: dict[str, Device] = {}
        for item in payload.get("result", []) or []:
            native_id = item.get("id")
            if not native_id:
                continue
            caps = _capabilities(item.get("status") or [])
            if not caps:
                continue
            device_id = "tuya:" + native_id
            devices[device_id] = Device(
                id=device_id,
                name=item.get("name") or native_id,
                room=item.get("room_name") or "Unassigned",
                adapter=self.name,
                native_id=native_id,
                kind=_kind(caps),
                capabilities=caps,
                online=bool(item.get("online", True)),
            )
            self._native[device_id] = native_id
        self._devices = devices

    async def _poll_status(self) -> None:
        if self._on_state is None or not self._devices:
            return
        ids = ",".join(self._native[d] for d in self._devices)
        payload = await self._request("GET", "/v1.0/devices/status?device_ids=" + ids)
        for entry in payload.get("result", []) or []:
            device_id = "tuya:" + str(entry.get("id"))
            if device_id not in self._devices:
                continue
            for status in entry.get("status", []) or []:
                cap = DP_TO_CAP.get(status.get("code", ""))
                if cap is None:
                    continue
                value = _decode(cap, status.get("value"))
                if value is not None:
                    await self._on_state(StateEvent(device_id, cap, value))

    async def _emit_status(self, connected: bool) -> None:
        if self._on_status is not None:
            await self._on_status(connected)

    # ---------------------------------------------------------------- writes

    async def execute(self, command: Command) -> None:
        native = self._native.get(command.device_id)
        if native is None:
            raise CommandError("device is not known to Tuya Cloud")
        code = CAP_TO_DP.get(command.capability)
        if code is None:
            raise CommandError("capability " + command.capability.value + " is not writable over Tuya")
        body = {"commands": [{"code": code, "value": _encode(command.capability, command.value)}]}
        payload = await self._request("POST", "/v1.0/devices/%s/commands" % native, body)
        if not payload.get("success", False):
            raise CommandError(str(payload.get("msg") or "tuya rejected the command"))

    # ------------------------------------------------------------------ http

    async def _token_value(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        payload = await self._request("GET", "/v1.0/token?grant_type=1", signed_with_token=False)
        result = payload.get("result") or {}
        self._token = result.get("access_token", "")
        self._token_expires = time.time() + float(result.get("expire_time", 7200))
        if not self._token:
            raise CommandError("tuya token request failed: " + str(payload.get("msg")))
        return self._token

    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        signed_with_token: bool = True,
    ) -> dict[str, Any]:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        token = await self._token_value() if signed_with_token else ""
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        timestamp = str(int(time.time() * 1000))

        # Tuya v2 signature: HMAC-SHA256 over
        #   client_id + access_token + t + (method\ncontent-sha256\n\npath)
        content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        string_to_sign = "\n".join([method.upper(), content_hash, "", path])
        message = self._client_id + token + timestamp + string_to_sign
        signature = hmac.new(
            self._secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        ).hexdigest().upper()

        headers = {
            "client_id": self._client_id,
            "sign": signature,
            "t": timestamp,
            "sign_method": "HMAC-SHA256",
            "Content-Type": "application/json",
        }
        if token:
            headers["access_token"] = token

        async with self._session.request(
            method, self._host + path, data=payload or None, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as response:
            data = await response.json(content_type=None)
        if not isinstance(data, dict):
            raise CommandError("unexpected tuya response")
        if not data.get("success", True) and data.get("code") in (1010, 1011, 1004):
            # Token rejected/expired -- drop it so the next call re-authenticates.
            self._token, self._token_expires = "", 0.0
        return data


# --------------------------------------------------------------- conversions


def _capabilities(status: list[dict[str, Any]]) -> list[Capability]:
    caps: list[Capability] = []
    for entry in status:
        cap = DP_TO_CAP.get(entry.get("code", ""))
        if cap is not None and cap not in caps:
            caps.append(cap)
    return caps


def _kind(caps: list[Capability]) -> str:
    if Capability.BRIGHTNESS in caps or Capability.COLOR_TEMP in caps:
        return "light"
    if Capability.SWITCH in caps:
        return "plug"
    return "sensor"


def _decode(cap: Capability, raw: Any) -> Any:
    if raw is None:
        return None
    try:
        if cap in (Capability.TEMPERATURE, Capability.HUMIDITY):
            return round(float(raw) / 10, 1)
        if cap is Capability.BRIGHTNESS:
            return round(float(raw) / TUYA_SCALE_MAX * 100)
        if cap is Capability.COLOR_TEMP:
            # Tuya reports 0..1000 warm->cold; map onto our Kelvin range.
            return round(2700 + float(raw) / TUYA_SCALE_MAX * (6500 - 2700))
        if cap in (Capability.BATTERY, Capability.ILLUMINANCE):
            return round(float(raw))
        if cap in (Capability.SWITCH, Capability.CONTACT, Capability.OCCUPANCY):
            return bool(raw) if not isinstance(raw, str) else raw.lower() in ("true", "1", "pir")
    except (TypeError, ValueError):
        return None
    return raw


def _encode(cap: Capability, value: Any) -> Any:
    if cap is Capability.SWITCH:
        return bool(value)
    if cap is Capability.BRIGHTNESS:
        return max(10, min(TUYA_SCALE_MAX, round(float(value) / 100 * TUYA_SCALE_MAX)))
    if cap is Capability.COLOR_TEMP:
        span = 6500 - 2700
        return max(0, min(TUYA_SCALE_MAX, round((float(value) - 2700) / span * TUYA_SCALE_MAX)))
    return value
