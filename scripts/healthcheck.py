#!/usr/bin/env python
"""Docker HEALTHCHECK (doc §12).

Exit 0 while the container can still do its job. `degraded` is deliberately
NOT a failure: if the database or the hub is temporarily unreachable, killing
and restarting the app fixes nothing and only takes the dashboard away from
whoever is looking at it. Only an unresponsive process is unhealthy.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:%s/healthz" % os.getenv("PORT", "8000")


def main() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=5) as response:
            if response.status != 200:
                print("healthz returned HTTP %s" % response.status, file=sys.stderr)
                return 1
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print("healthz unreachable: %s" % exc, file=sys.stderr)
        return 1

    status = payload.get("status", "unknown")
    if status == "degraded":
        # Visible in `docker inspect`, but not a restart trigger.
        print("degraded: adapter=%s db=%s" % (
            payload.get("adapter_connected"),
            (payload.get("database") or {}).get("connected"),
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
