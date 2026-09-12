"""Matter adapter -- talks to python-matter-server over its WebSocket API.

The Zgmismart M1 joins our fabric as a Matter *bridge*: one Matter node whose
endpoints are the Zigbee sub-devices. So a device here is a (node_id, endpoint)
pair, and discovery is a walk over each node's attribute map.

Attribute paths arrive from the server as "<endpoint>/<cluster>/<attribute>".
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import aiohttp

from app.adapters.base import StateSink, StatusSink
from app.config import settings
from app.models import Capability, Command, CommandError, Device, StateEvent

log = logging.getLogger(__name__)

# --- Matter cluster / attribute ids we care about --------------------------
C_BASIC = 40
C_POWER_SOURCE = 47
C_ON_OFF = 6
C_LEVEL = 8
C_DESCRIPTOR = 29
C_BRIDGED_BASIC = 57
C_BOOLEAN_STATE = 69
C_COLOR_CONTROL = 768
C_FIXED_LABEL = 64
C_USER_LABEL = 65
C_ILLUMINANCE = 1024
C_TEMPERATURE = 1026
C_HUMIDITY = 1029
C_OCCUPANCY = 1030

A_ON_OFF = 0
A_CURRENT_LEVEL = 0
A_COLOR_TEMP_MIREDS = 7
A_MEASURED_VALUE = 0
A_OCCUPANCY = 0
A_STATE_VALUE = 0
A_BAT_PERCENT = 12
A_DEVICE_TYPE_LIST = 0
A_PARTS_LIST = 3
A_LABEL_LIST = 0
A_NODE_LABEL = 5
A_REACHABLE = 17

#: (cluster, attribute) -> capability. Presence of the pair in an endpoint's
#: attribute map is also how we decide the endpoint has that capability.
ATTR_TO_CAP: dict[tuple[int, int], Capability] = {
    (C_ON_OFF, A_ON_OFF): Capability.SWITCH,
    (C_LEVEL, A_CURRENT_LEVEL): Capability.BRIGHTNESS,
    (C_COLOR_CONTROL, A_COLOR_TEMP_MIREDS): Capability.COLOR_TEMP,
    (C_TEMPERATURE, A_MEASURED_VALUE): Capability.TEMPERATURE,
    (C_HUMIDITY, A_MEASURED_VALUE): Capability.HUMIDITY,
    (C_ILLUMINANCE, A_MEASURED_VALUE): Capability.ILLUMINANCE,
    (C_OCCUPANCY, A_OCCUPANCY): Capability.OCCUPANCY,
    (C_BOOLEAN_STATE, A_STATE_VALUE): Capability.CONTACT,
    (C_POWER_SOURCE, A_BAT_PERCENT): Capability.BATTERY,
}

#: Matter device type id -> our coarse UI category.
DEVICE_TYPE_KIND: dict[int, str] = {
    0x0100: "light",   # On/Off Light
    0x0101: "light",   # Dimmable Light
    0x010C: "light",   # Color Temperature Light
    0x010D: "light",   # Extended Color Light
    0x010A: "plug",    # On/Off Plug-in Unit
    0x010B: "plug",    # Dimmable Plug-in Unit
    0x0302: "sensor",  # Temperature Sensor
    0x0307: "sensor",  # Humidity Sensor
    0x0106: "sensor",  # Light Sensor
    0x0107: "sensor",  # Occupancy Sensor
    0x0015: "sensor",  # Contact Sensor
    0x000F: "switch",  # Generic Switch
    0x0103: "switch",  # On/Off Light Switch
}

#: Endpoints that only describe the bridge itself, never a real device.
INFRA_DEVICE_TYPES = {0x000E, 0x0013, 0x0016}  # Aggregator, Bridged Node, Root

#: The endpoint that carries a bridged device's identity. For a *composed*
#: device (a 2-gang switch, say) it holds the name while its PartsList children
#: hold the clusters you actually control -- so the name lives one level up
#: from the endpoint that becomes a card.
BRIDGED_NODE_TYPE = 0x0013


class MatterAdapter:
    name = "matter"

    def __init__(self, url: str | None = None) -> None:
        self._url = url or settings.matter_ws_url
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task | None = None
        self._calls: dict[str, asyncio.Future] = {}
        self._call_seq = 0
        self._nodes: dict[int, dict[str, Any]] = {}
        self._devices: dict[str, Device] = {}
        self._route: dict[str, tuple[int, int]] = {}
        #: (node, endpoint carrying Reachable) -> devices it speaks for. A
        #: composed device reports Reachable once, on its parent endpoint.
        self._reach: dict[tuple[int, int], list[str]] = {}
        self._on_state: StateSink | None = None
        self._on_status: StatusSink | None = None
        self._ready = asyncio.Event()
        self._closing = False

    # ------------------------------------------------------------------ life

    async def start(self, on_state: StateSink, on_status: StatusSink) -> None:
        self._on_state = on_state
        self._on_status = on_status
        self._closing = False
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._run_forever())
        # Give the first connection a moment so the initial discover() is warm,
        # but never block startup on unreachable hardware.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout=10)

    async def stop(self) -> None:
        self._closing = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session:
            await self._session.close()

    async def _run_forever(self) -> None:
        backoff = 1.0
        while not self._closing:
            try:
                await self._connect_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure means retry
                log.warning("matter-server connection lost (%s); retrying in %.0fs", exc, backoff)
            finally:
                self._ready.clear()
                await self._emit_status(False)
            if self._closing:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _connect_once(self) -> None:
        assert self._session is not None
        log.info("connecting to matter-server at %s", self._url)
        async with self._session.ws_connect(self._url, heartbeat=30) as ws:
            self._ws = ws
            hello = await ws.receive_json()
            log.info(
                "matter-server ready (sdk=%s schema=%s fabric=%s)",
                hello.get("sdk_version"),
                hello.get("schema_version"),
                hello.get("fabric_id"),
            )
            # The reader has to be running *before* the first command: replies
            # are matched by message_id inside _handle, so calling and awaiting
            # a reply with no reader yet would simply time out every time.
            reader = asyncio.create_task(self._ingest(ws))
            try:
                nodes = await self._call("start_listening")
                self._ingest_nodes(nodes or [])
                await self._replay_state()
                self._ready.set()
                await self._emit_status(True)
                await reader          # returns when the socket closes
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
        raise ConnectionError("websocket closed")

    async def _ingest(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type is aiohttp.WSMsgType.TEXT:
                await self._handle(msg.json())
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break

    # -------------------------------------------------------------- protocol

    async def _call(self, command: str, **args: Any) -> Any:
        if self._ws is None or self._ws.closed:
            raise CommandError("matter-server is not connected")
        self._call_seq += 1
        message_id = str(self._call_seq)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._calls[message_id] = future
        await self._ws.send_json({"message_id": message_id, "command": command, "args": args})
        try:
            return await asyncio.wait_for(future, timeout=settings.matter_rpc_timeout)
        except asyncio.TimeoutError as exc:
            raise CommandError("matter-server did not answer " + command) from exc
        finally:
            self._calls.pop(message_id, None)

    async def _handle(self, msg: dict[str, Any]) -> None:
        message_id = msg.get("message_id")
        if message_id is not None:
            future = self._calls.get(message_id)
            if future and not future.done():
                if "error_code" in msg:
                    future.set_exception(
                        CommandError(str(msg.get("details") or msg["error_code"]))
                    )
                else:
                    future.set_result(msg.get("result"))
            return

        event = msg.get("event")
        data = msg.get("data")
        if event == "attribute_updated" and isinstance(data, (list, tuple)) and len(data) == 3:
            await self._on_attribute(int(data[0]), str(data[1]), data[2])
        elif event in ("node_added", "node_updated"):
            if isinstance(data, dict):
                self._ingest_nodes([data])
                await self._replay_state()
        elif event == "node_removed":
            self._nodes.pop(int(data), None)
            self._rebuild()

    # ------------------------------------------------------------- discovery

    def _ingest_nodes(self, nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            node_id = node.get("node_id")
            if node_id is None:
                continue
            self._nodes[int(node_id)] = node
        self._rebuild()

    def _rebuild(self) -> None:
        devices: dict[str, Device] = {}
        route: dict[str, tuple[int, int]] = {}
        reach: dict[tuple[int, int], list[str]] = {}
        for node_id, node in self._nodes.items():
            attributes: dict[str, Any] = node.get("attributes") or {}
            node_name = attributes.get("0/%d/%d" % (C_BASIC, A_NODE_LABEL)) or ("Node %d" % node_id)
            parents = _parent_map(attributes)
            for endpoint in sorted({_endpoint_of(path) for path in attributes}):
                if endpoint is None or endpoint == 0:
                    continue
                device = self._build_device(
                    node_id, endpoint, attributes, str(node_name), parents
                )
                if device is not None:
                    devices[device.id] = device
                    route[device.id] = (node_id, endpoint)
                    source = endpoint
                    if endpoint in parents and "%d/%d/%d" % (
                        endpoint, C_BRIDGED_BASIC, A_REACHABLE
                    ) not in attributes:
                        source = parents[endpoint][0]
                    reach.setdefault((node_id, source), []).append(device.id)
        self._devices = devices
        self._route = route
        self._reach = reach

    def _build_device(
        self,
        node_id: int,
        endpoint: int,
        attributes: dict[str, Any],
        node_name: str,
        parents: dict[int, tuple[int, list[int]]],
    ) -> Device | None:
        prefix = "%d/" % endpoint
        capabilities: list[Capability] = []
        for (cluster, attr), cap in ATTR_TO_CAP.items():
            if "%s%d/%d" % (prefix, cluster, attr) in attributes:
                capabilities.append(cap)
        if not capabilities:
            return None

        type_ids = _device_type_ids(attributes.get("%s%d/%d" % (prefix, C_DESCRIPTOR, A_DEVICE_TYPE_LIST)))
        real_types = [t for t in type_ids if t not in INFRA_DEVICE_TYPES]
        if type_ids and not real_types:
            return None

        kind = next((DEVICE_TYPE_KIND[t] for t in real_types if t in DEVICE_TYPE_KIND), None)
        if kind is None:
            kind = "light" if Capability.SWITCH in capabilities else "sensor"

        parent = parents.get(endpoint)
        name = _name_for(attributes, endpoint, parent) or "%s ep%d" % (node_name, endpoint)
        reachable = _reachable(attributes, endpoint, parent)

        return Device(
            id="matter:%d:%d" % (node_id, endpoint),
            name=name,
            room=_room_from_name(name),
            adapter=self.name,
            native_id="%d/%d" % (node_id, endpoint),
            kind=kind,
            capabilities=capabilities,
            online=True if reachable is None else bool(reachable),
        )

    async def discover(self) -> list[Device]:
        return list(self._devices.values())

    async def _replay_state(self) -> None:
        """Publish the values that arrived with the node dump.

        start_listening returns the current attribute map, but after that the
        server only sends *changes*. Without replaying it, every reading shows
        as blank until the device happens to report again -- which for a quiet
        sensor can be many minutes.
        """
        if self._on_state is None:
            return
        for node_id, node in self._nodes.items():
            for path, raw in (node.get("attributes") or {}).items():
                endpoint, cluster, attribute = _split_path(path)
                if endpoint is None:
                    continue
                cap = ATTR_TO_CAP.get((cluster, attribute))
                device_id = "matter:%d:%d" % (node_id, endpoint)
                if cap is None or device_id not in self._devices:
                    continue
                value = _decode(cap, raw)
                if value is not None:
                    await self._on_state(StateEvent(device_id, cap, value))

    async def commission(self, code: str) -> dict[str, Any]:
        """Join a device to our fabric with its 11-digit pairing code.

        `network_only` skips Bluetooth: the M1 is already on the IP network, so
        matter-server can find it over mDNS. Multi-admin means the hub stays
        paired with the Tuya app at the same time.
        """
        node = await self._call("commission_with_code", code=code, network_only=True)
        if isinstance(node, dict):
            self._ingest_nodes([node])
        return node if isinstance(node, dict) else {"result": node}

    # ------------------------------------------------------------ state flow

    async def _on_attribute(self, node_id: int, path: str, raw: Any) -> None:
        node = self._nodes.get(node_id)
        if node is not None:
            node.setdefault("attributes", {})[path] = raw

        endpoint, cluster, attribute = _split_path(path)
        if endpoint is None:
            return
        device_id = "matter:%d:%d" % (node_id, endpoint)

        if (cluster, attribute) == (C_BRIDGED_BASIC, A_REACHABLE):
            for target in self._reach.get((node_id, endpoint), [device_id]):
                device = self._devices.get(target)
                if device is not None:
                    device.online = bool(raw)
            return

        cap = ATTR_TO_CAP.get((cluster, attribute))
        if cap is None or device_id not in self._devices:
            return
        value = _decode(cap, raw)
        if value is None or self._on_state is None:
            return
        await self._on_state(StateEvent(device_id, cap, value))

    async def _emit_status(self, connected: bool) -> None:
        if self._on_status is not None:
            await self._on_status(connected)

    # ---------------------------------------------------------------- writes

    async def execute(self, command: Command) -> None:
        target = self._route.get(command.device_id)
        if target is None:
            raise CommandError("device is not present on the Matter fabric")
        node_id, endpoint = target
        cluster, name, payload = _encode(command)
        await self._call(
            "device_command",
            node_id=node_id,
            endpoint_id=endpoint,
            cluster_id=cluster,
            command_name=name,
            payload=payload,
        )


# --------------------------------------------------------------- conversions


def _split_path(path: str) -> tuple[int | None, int, int]:
    parts = path.split("/")
    if len(parts) != 3:
        return None, -1, -1
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None, -1, -1


def _endpoint_of(path: str) -> int | None:
    return _split_path(path)[0]


def _device_type_ids(raw: Any) -> list[int]:
    """DeviceTypeList is a list of structs; the server may serialise the struct
    fields either by name or by their numeric field ids."""
    if not isinstance(raw, list):
        return []
    ids: list[int] = []
    for entry in raw:
        if isinstance(entry, dict):
            value = entry.get("deviceType", entry.get("0"))
        else:
            value = entry
        if isinstance(value, int):
            ids.append(value)
    return ids


def _parent_map(attributes: dict[str, Any]) -> dict[int, tuple[int, list[int]]]:
    """child endpoint -> (bridged-node endpoint, all of its children).

    Only Bridged Node endpoints count as parents. The root and the aggregator
    also list endpoints in their PartsList -- the root lists every endpoint on
    the node -- so trusting PartsList alone would make endpoint 0 the parent of
    everything.
    """
    parents: dict[int, tuple[int, list[int]]] = {}
    for endpoint in {_endpoint_of(path) for path in attributes}:
        if endpoint is None:
            continue
        types = _device_type_ids(
            attributes.get("%d/%d/%d" % (endpoint, C_DESCRIPTOR, A_DEVICE_TYPE_LIST))
        )
        if BRIDGED_NODE_TYPE not in types:
            continue
        parts = attributes.get("%d/%d/%d" % (endpoint, C_DESCRIPTOR, A_PARTS_LIST))
        if not isinstance(parts, list):
            continue
        children = [int(c) for c in parts if isinstance(c, int) and c != endpoint]
        for child in children:
            parents[child] = (endpoint, children)
    return parents


def _label_list(raw: Any) -> list[str]:
    """FixedLabel / UserLabel LabelList -> the values worth showing."""
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        value = str(entry.get("value", entry.get("1", ""))).strip()
        if value:
            values.append(value)
    return values


def _node_label(attributes: dict[str, Any], endpoint: int) -> str:
    label = attributes.get("%d/%d/%d" % (endpoint, C_BRIDGED_BASIC, A_NODE_LABEL))
    return str(label).strip() if label else ""


def _endpoint_label(attributes: dict[str, Any], endpoint: int) -> str:
    """Every name an endpoint offers for itself, best first."""
    own = _node_label(attributes, endpoint)
    if own:
        return own
    for cluster in (C_USER_LABEL, C_FIXED_LABEL):
        values = _label_list(attributes.get("%d/%d/%d" % (endpoint, cluster, A_LABEL_LIST)))
        if values:
            return " ".join(values)
    return ""


def _name_for(
    attributes: dict[str, Any], endpoint: int, parent: tuple[int, list[int]] | None
) -> str:
    own = _endpoint_label(attributes, endpoint)
    if own or parent is None:
        return own
    # A composed device keeps its name on the bridged-node endpoint, so borrow
    # it and number the parts: "2 Gang Switch 1", "2 Gang Switch 2".
    parent_endpoint, siblings = parent
    base = _endpoint_label(attributes, parent_endpoint)
    if not base or len(siblings) < 2:
        return base
    return "%s %d" % (base, siblings.index(endpoint) + 1)


def _reachable(
    attributes: dict[str, Any], endpoint: int, parent: tuple[int, list[int]] | None
) -> Any:
    """Reachable lives on the bridged-node endpoint, not on its parts."""
    own = attributes.get("%d/%d/%d" % (endpoint, C_BRIDGED_BASIC, A_REACHABLE))
    if own is not None or parent is None:
        return own
    return attributes.get("%d/%d/%d" % (parent[0], C_BRIDGED_BASIC, A_REACHABLE))


def _decode(cap: Capability, raw: Any) -> Any:
    if raw is None:
        return None
    try:
        if cap in (Capability.TEMPERATURE, Capability.HUMIDITY):
            return round(int(raw) / 100, 1)
        if cap is Capability.ILLUMINANCE:
            # MeasuredValue = 10000 * log10(lux) + 1
            return 0 if int(raw) <= 0 else round(10 ** ((int(raw) - 1) / 10000), 1)
        if cap is Capability.BRIGHTNESS:
            return round(int(raw) / 254 * 100)
        if cap is Capability.COLOR_TEMP:
            mireds = int(raw)
            return round(1_000_000 / mireds) if mireds else None
        if cap is Capability.BATTERY:
            return round(int(raw) / 2)
        if cap is Capability.OCCUPANCY:
            return bool(int(raw) & 0x01)
        if cap in (Capability.SWITCH, Capability.CONTACT):
            return bool(raw)
    except (TypeError, ValueError):
        return None
    return raw


def _encode(command: Command) -> tuple[int, str, dict[str, Any]]:
    cap, value = command.capability, command.value
    if cap is Capability.SWITCH:
        return C_ON_OFF, ("On" if value else "Off"), {}
    if cap is Capability.BRIGHTNESS:
        return (
            C_LEVEL,
            "MoveToLevelWithOnOff",
            {
                "level": max(1, min(254, round(float(value) / 100 * 254))),
                "transitionTime": 0,
                "optionsMask": 0,
                "optionsOverride": 0,
            },
        )
    if cap is Capability.COLOR_TEMP:
        return (
            C_COLOR_CONTROL,
            "MoveToColorTemperature",
            {
                "colorTemperatureMireds": round(1_000_000 / max(1, int(value))),
                "transitionTime": 0,
                "optionsMask": 0,
                "optionsOverride": 0,
            },
        )
    raise CommandError("capability " + cap.value + " is not writable over Matter")


def _room_from_name(name: str) -> str:
    """Bridged labels usually read "Living Room Light"; treat a leading
    "<room> - <device>" or "<room>/<device>" as an explicit room hint."""
    for sep in (" - ", " / ", "/"):
        if sep in name:
            return name.split(sep, 1)[0].strip()
    return "Unassigned"
