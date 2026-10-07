"""What Toshiba's cloud says about each air conditioner, and how often each
one talks.

    docker exec -it iot-app python scripts/toshiba_probe.py            # 10 minutes
    docker exec -it iot-app python scripts/toshiba_probe.py --minutes 20

Toshiba's cloud never announces that an AC dropped off -- an adapter that
loses power simply goes quiet -- so telling "offline" from "nothing to say"
means knowing what a healthy unit sends and how often. This prints, for every
AC on the account:

  1. the AC's entry in the account's device list and its current-state
     record, every field as the cloud returns it (the library keeps only a
     few, and drops whatever might say "offline");
  2. a timeline of every message each AC pushes -- state changes and
     heartbeats -- and the gaps between them.

Run it while one unit is known to be off its Wi-Fi and the others are fine,
and the difference between them is the answer.

It registers under an id of its own, kept beside the dashboard's in
TOSHIBA_STATE_PATH, so it can run while the dashboard is up without knocking
it off its connection. Nothing secret is printed: no password, no token.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

#: Between this probe's own HTTP requests.
REQUEST_GAP_SECONDS = 2.0

#: Never echo these, whatever the cloud puts in them.
SECRET_KEYS = {"access_token", "token", "password", "sastoken", "sas_token", "primarykey", "secondarykey"}


def _probe_id() -> str:
    """This probe's own id, 16 hex digits like the dashboard's: the library
    sends it as the Device-ID header, without which Toshiba's firewall
    answers every login with 429."""
    path = Path(settings.toshiba_state_path) if settings.toshiba_state_path else None
    data: dict = {}
    if path is not None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    probe = data.get("probe_id")
    if isinstance(probe, str) and re.fullmatch(r"[0-9a-f]{16}", probe):
        return probe
    probe = secrets.token_hex(8)
    if path is not None:
        data["probe_id"] = probe
        try:
            # The file holds the dashboard's login session too: owner only.
            temp = path.with_name(path.name + ".tmp")
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.chmod(temp, 0o600)
            os.replace(temp, path)
        except OSError:
            pass
    return probe


def _scrub(value):
    if isinstance(value, dict):
        return {k: ("<hidden>" if k.lower().replace("_", "") in {s.replace("_", "") for s in SECRET_KEYS}
                    else _scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _stamp(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t))


async def main(minutes: float) -> int:
    if not (settings.toshiba_username and settings.toshiba_password):
        print("TOSHIBA_USERNAME / toshiba_password are not set (see README)", file=sys.stderr)
        return 1
    logging.basicConfig(level=logging.WARNING)
    # The library warns about every push from an AC it was not asked to
    # track -- here, all of them. The timeline below says it better.
    for noisy in ("toshiba_ac", "toshiba_ac.device_manager", "azure.iot.device"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    from toshiba_ac.device_manager import ToshibaAcDeviceManager

    from app.adapters.toshiba_adapter import rate_limited

    manager = ToshibaAcDeviceManager(settings.toshiba_username, settings.toshiba_password, _probe_id())
    try:
        return await _probe(manager, minutes)
    except Exception as exc:  # noqa: BLE001 - say what to do, not a traceback
        if not rate_limited(exc):
            raise
        print(
            "\nToshiba is refusing requests for now (%s): this account talked to it\n"
            "too often lately -- every dashboard restart is a login, and so is each\n"
            "run of this probe. Leave everything alone for 30 minutes, then run this\n"
            "again. Restarting the dashboard meanwhile only makes the wait longer." % exc,
            file=sys.stderr,
        )
        return 2
    finally:
        await manager.shutdown()


async def _probe(manager, minutes: float) -> int:
    # Login, registration and the IoT Hub connection. From here on every AC's
    # pushes reach the library's handlers, so wrap them at once: note the
    # time, then hand on. They may run on the SDK's own thread -- keep the
    # wrapper to an append and a print.
    await manager.connect()
    heard: dict[str, list[tuple[float, str]]] = {}
    names: dict[str, str] = {}
    amqp = manager.amqp_api
    for command, original in list(amqp.handlers.items()):
        def wrapped(source_id, message_id, target_id, payload, timestamp,
                    _original=original, _command=command):
            heard.setdefault(source_id, []).append((time.time(), _command))
            print("  %s  %-16s %s" % (_stamp(time.time()), _command, names.get(source_id, source_id)),
                  flush=True)
            return _original(source_id, message_id, target_id, payload, timestamp)
        amqp.handlers[command] = wrapped
    start = time.time()

    # As few requests as will answer the question, spaced out: Toshiba
    # answers a burst with 429, and that block covers the dashboard and the
    # phone app on this account too. (No get_devices(): it fetches the list
    # again and more per AC, none of which this needs.)
    http = manager.http_api
    mapping = await http.request_api(http.AC_MAPPING_PATH, get={"consumerId": http.consumer_id})
    acs = [ac for group in mapping for ac in group.get("ACList", [])]
    for ac in acs:
        names[ac.get("DeviceUniqueId")] = ac.get("Name")
        heard.setdefault(ac.get("DeviceUniqueId"), [])

    print("=" * 72)
    print("1. what the cloud says, per air conditioner")
    print("=" * 72)
    for ac in acs:
        print("\n--- %s  (%s)" % (ac.get("Name"), ac.get("DeviceUniqueId")))
        print("device list entry:")
        print(json.dumps(_scrub({k: v for k, v in ac.items() if k != "ModeValues"}),
                         indent=2, ensure_ascii=False))
        await asyncio.sleep(REQUEST_GAP_SECONDS)
        state = await http.request_api(
            http.AC_STATE_PATH, get={"ACId": ac.get("Id"), "consumerId": http.consumer_id})
        print("current state record:")
        print(json.dumps(_scrub(state), indent=2, ensure_ascii=False))

    print("\n" + "=" * 72)
    print("2. listening for %g minutes -- every message as it arrives" % minutes)
    print("=" * 72)
    await asyncio.sleep(max(0.0, minutes * 60 - (time.time() - start)))

    print("\n" + "=" * 72)
    print("3. summary")
    print("=" * 72)
    for uid, events in heard.items():
        gaps = [b[0] - a[0] for a, b in zip(events, events[1:])]
        beats = sum(1 for _, c in events if c == "CMD_HEARTBEAT")
        print("%s (%s): %d messages, %d heartbeats; %s; longest silence %s" % (
            names.get(uid, uid), uid, len(events), beats,
            "first at +%ds" % (events[0][0] - start) if events else "NOTHING heard",
            "%ds" % max(gaps) if gaps else "-",
        ))
    print("\nSend this whole output (it has no password or token in it).")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--minutes", type=float, default=10.0, help="how long to listen (default 10)")
    sys.exit(asyncio.run(main(parser.parse_args().minutes)))
