#!/usr/bin/env python
"""Join the M1 hub (or any Matter device) to our fabric -- doc §12.

    python scripts/commission.py 0310-062-0526            # manual pairing code
    python scripts/commission.py MT:Y.K90AFN00KA0648G00   # scanned QR payload

Get the pairing code from the Tuya app: pick the hub, open the Matter /
third-party control menu, and let it generate a code. Multi-admin means the hub
stays paired with Tuya at the same time.

Talks straight to python-matter-server, so it works before the dashboard is up.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402

#: Base-38, the QR payload alphabet: 0-9, A-Z, and "." / "-" -- 38 symbols.
#: They are data, not separators, so a QR payload must never be stripped.
BASE38 = re.compile(r"^[0-9A-Z.-]+$")
#: A manual pairing code is 11 digits, or 21 for a custom-flow device.
MANUAL_LENGTHS = (11, 21)


def die(message: str) -> None:
    raise SystemExit("commission.py: %s" % message)


def normalise(code: str) -> str:
    """Accept what people actually paste, and reject what cannot work.

    Checking here is worth the lines: the app keeps its commissioning window
    open only for a few minutes and invalidates the code after a failed
    attempt, so every malformed try costs a trip back to the phone.
    """
    code = code.strip().replace(" ", "")
    if code[:3].upper() == "MT:":
        body = code[3:].upper()
        if body.isdigit() and len(body) in MANUAL_LENGTHS:
            # "MT:" labels a scanned QR payload, which is ~19 base-38 symbols.
            # A bare 11-digit number is a manual pairing code, and the prefix
            # makes the SDK base-38 decode it instead -- yielding a garbage
            # payload rather than a complaint.
            print('dropping the "MT:" prefix: %d digits is a manual pairing code'
                  % len(body))
            return body
        if not BASE38.match(body):
            die("%r is not a QR payload -- base-38 allows only 0-9, A-Z, . and -"
                % code)
        return "MT:" + body
    digits = code.replace("-", "")
    if not digits.isdigit():
        die("%r is neither a manual pairing code (digits) nor a QR payload (MT:...)"
            % code)
    if len(digits) not in MANUAL_LENGTHS:
        die("a manual pairing code has 11 digits (21 for a custom flow); "
            "%r has %d" % (code, len(digits)))
    return digits


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
                    # matter-server collapses every CHIP failure into one
                    # sentence; the actual reason is only in its own log.
                    print("the reason is in matter-server's log:\n"
                          "    docker logs matter-server --tail 80", file=sys.stderr)
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
    return asyncio.run(commission(args.url, normalise(args.code), args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
