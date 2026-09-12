#!/usr/bin/env python
"""Print what the bridge actually exposes -- doc §12 (`make matter-nodes`).

    python scripts/dump_nodes.py            # human-readable endpoint/cluster map
    python scripts/dump_nodes.py --raw      # full attribute dump as JSON

This is the tool to reach for when a device shows up with the wrong capabilities
or not at all: it shows the raw endpoint / cluster / attribute triples straight
from matter-server, before any of our mapping runs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.adapters.matter_adapter import (  # noqa: E402
    ATTR_TO_CAP,
    A_LABEL_LIST,
    A_NODE_LABEL,
    A_PARTS_LIST,
    C_BRIDGED_BASIC,
    C_DESCRIPTOR,
    C_FIXED_LABEL,
    C_USER_LABEL,
    DEVICE_TYPE_KIND,
    _device_type_ids,
    _label_entries,
    _name_for,
    _parent_map,
    _split_path,
)
from app.config import settings  # noqa: E402

CLUSTER_NAMES = {
    6: "OnOff", 8: "LevelControl", 29: "Descriptor", 40: "BasicInformation",
    64: "FixedLabel", 65: "UserLabel",
    47: "PowerSource", 57: "BridgedDeviceBasicInformation", 69: "BooleanState",
    768: "ColorControl", 1024: "IlluminanceMeasurement", 1026: "TemperatureMeasurement",
    1029: "RelativeHumidityMeasurement", 1030: "OccupancySensing",
}


async def dump(url: str, raw: bool) -> int:
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url, heartbeat=30) as ws:
            hello = await ws.receive_json()
            print("matter-server %s · fabric %s\n" % (
                hello.get("sdk_version"), hello.get("fabric_id")))
            await ws.send_json({"message_id": "1", "command": "start_listening", "args": {}})
            while True:
                message = await ws.receive_json()
                if message.get("message_id") == "1":
                    break
            if "error_code" in message:
                print("error: %s" % message.get("details"), file=sys.stderr)
                return 1
            nodes = message.get("result") or []

    if raw:
        print(json.dumps(nodes, indent=2, ensure_ascii=False))
        return 0

    if not nodes:
        print("no nodes commissioned yet -- run scripts/commission.py first")
        return 0

    for node in nodes:
        node_id = node.get("node_id")
        attributes = node.get("attributes") or {}
        print("node %s  available=%s  bridge=%s"
              % (node_id, node.get("available"), node.get("is_bridge")))

        endpoints: dict[int, list[str]] = {}
        for path in attributes:
            endpoint, cluster, attribute = _split_path(path)
            if endpoint is None:
                continue
            endpoints.setdefault(endpoint, []).append(path)

        parents = _parent_map(attributes)
        for endpoint in sorted(endpoints):
            prefix = "%d/" % endpoint
            types = _device_type_ids(attributes.get("%s%d/%d" % (prefix, C_DESCRIPTOR, 0)))
            kinds = ["0x%04X%s" % (t, "/" + DEVICE_TYPE_KIND[t] if t in DEVICE_TYPE_KIND else "")
                     for t in types]
            caps = sorted({
                cap.value for (cl, at), cap in ATTR_TO_CAP.items()
                if "%s%d/%d" % (prefix, cl, at) in attributes
            })
            # What the dashboard would call it, and why.
            resolved = _name_for(attributes, endpoint, parents.get(endpoint))
            print("  endpoint %-3d types=%s" % (endpoint, kinds or "-"))
            print("      NodeLabel: %r" % attributes.get(
                "%s%d/%d" % (prefix, C_BRIDGED_BASIC, A_NODE_LABEL)))
            for cluster, label in ((C_FIXED_LABEL, "FixedLabel"), (C_USER_LABEL, "UserLabel")):
                path = "%s%d/%d" % (prefix, cluster, A_LABEL_LIST)
                if path in attributes:
                    pairs = ["%s=%r" % (k or "?", v) for k, v in _label_entries(attributes[path])]
                    print("      %-10s %s" % (label + ":", ", ".join(pairs) or "(empty)"))
            parts = attributes.get("%s%d/%d" % (prefix, C_DESCRIPTOR, A_PARTS_LIST))
            if parts:
                print("      PartsList: %s" % parts)
            if endpoint in parents:
                print("      part of endpoint %d" % parents[endpoint][0])
            if caps:
                print("      -> device %r  capabilities: %s" % (resolved, ", ".join(caps)))
            else:
                print("      -> not a device (no controllable or readable cluster)")

            clusters: dict[int, list[int]] = {}
            for path in endpoints[endpoint]:
                _, cluster, attribute = _split_path(path)
                clusters.setdefault(cluster, []).append(attribute)
            for cluster in sorted(clusters):
                name = CLUSTER_NAMES.get(cluster, "cluster")
                print("      %-6d %-32s attrs=%s"
                      % (cluster, name, sorted(clusters[cluster])[:12]))
        print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=settings.matter_ws_url)
    parser.add_argument("--raw", action="store_true", help="dump the JSON as received")
    args = parser.parse_args()
    return asyncio.run(dump(args.url, args.raw))


if __name__ == "__main__":
    raise SystemExit(main())
