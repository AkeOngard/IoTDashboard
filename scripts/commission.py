#!/usr/bin/env python
"""Join the M1 hub (or any Matter device) to our fabric -- doc §12.

    python scripts/commission.py MT:Y.K90AFN00KA0648G00

Get the pairing code from the Tuya app: pick the hub, open the Matter /
third-party control menu, and let it generate a code. Multi-admin means the hub
stays paired with Tuya at the same time.

Talks straight to python-matter-server, so it works before the dashboard is up.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402


async def commission(url: str, code: str, timeout: float) -> int:
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url, heartbeat=30) as ws:
            hello = await ws.receive_json()
            print("matter-server %s (schema %s), fabric %s" % (
                hello.get("sdk_version"), hello.get("schema_version"), hello.get("fabric_id")))

            await ws.send_json({
                "message_id": "1",
                "command": "commission_with_code",
                # The hub is already on the IP network, so mDNS discovery is
                # enough and we never need Bluetooth.
                "args": {"code": code, "network_only": True},
            })
            print("commissioning… (this can take up to a minute)")

            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    print("timed out waiting for matter-server", file=sys.stderr)
                    return 1
                message = await asyncio.wait_for(ws.receive_json(), timeout=remaining)
                if message.get("message_id") != "1":
                    continue
                if "error_code" in message:
                    print("failed: %s" % (message.get("details") or message["error_code"]),
                          file=sys.stderr)
                    return 1
                node = message.get("result") or {}
                print("commissioned node_id=%s" % node.get("node_id"))
                endpoints = sorted({
                    int(path.split("/")[0]) for path in (node.get("attributes") or {})
                })
                print("endpoints: %s" % endpoints)
                print(json.dumps({"node_id": node.get("node_id"), "endpoints": endpoints}, indent=2))
                return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("code", help="11-digit pairing code, or the MT:... string")
    parser.add_argument("--url", default=settings.matter_ws_url)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    return asyncio.run(commission(args.url, args.code.strip(), args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
