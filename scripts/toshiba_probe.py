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
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

#: Never echo these, whatever the cloud puts in them.
SECRET_KEYS = {"access_token", "token", "password", "sastoken", "sas_token", "primarykey", "secondarykey"}


def _probe_id() -> str:
    path = Path(settings.toshiba_state_path) if settings.toshiba_state_path else None
    data: dict = {}
    if path is not None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    probe = data.get("probe_id")
    if isinstance(probe, str) and probe:
        return probe
    probe = "probe" + secrets.token_hex(6)
    if path is not None:
        data["probe_id"] = probe
        try:
            temp = path.with_name(path.name + ".tmp")
            path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps(data), encoding="utf-8")
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
    for noisy in ("toshiba_ac", "azure.iot.device"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    from toshiba_ac.device_manager import ToshibaAcDeviceManager

    from app.adapters.toshiba_adapter import rate_limited

    manager = ToshibaAcDeviceManager(settings.toshiba_username, settings.toshiba_password, _probe_id())
    try:
        try:
            await manager.connect()
        except Exception as exc:  # noqa: BLE001 - say what to do, not a traceback
            if not rate_limited(exc):
                raise
            print(
                "Toshiba is refusing logins for now (%s): this account logged in too\n"
                "often lately -- every dashboard restart is a login, and so is each run\n"
                "of this probe. Leave it alone for 30 minutes, then run this again.\n"
                "Restarting the dashboard meanwhile only makes the wait longer." % exc,
                file=sys.stderr,
            )
            return 2
        http = manager.http_api
        mapping = await http.request_api(http.AC_MAPPING_PATH, get={"consumerId": http.consumer_id})
        acs = [ac for group in mapping for ac in group.get("ACList", [])]
        names = {ac.get("DeviceUniqueId"): ac.get("Name") for ac in acs}

        print("=" * 72)
        print("1. what the cloud says, per air conditioner")
        print("=" * 72)
        for ac in acs:
            print("\n--- %s  (%s)" % (ac.get("Name"), ac.get("DeviceUniqueId")))
            print("device list entry:")
            print(json.dumps(_scrub({k: v for k, v in ac.items() if k != "ModeValues"}),
                             indent=2, ensure_ascii=False))
            try:
                state = await http.request_api(
                    http.AC_STATE_PATH, get={"ACId": ac.get("Id"), "consumerId": http.consumer_id})
                print("current state record:")
                print(json.dumps(_scrub(state), indent=2, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001 - the answer itself is the finding
                print("current state record: FAILED -- %s" % exc)

        # Hear everything each AC pushes. The library dispatches by command
        # name; wrap its handlers to note the time first. They may be called
        # from the SDK's own thread, so keep the wrapper to appending.
        heard: dict[str, list[tuple[float, str]]] = {uid: [] for uid in names}
        await manager.get_devices()
        amqp = manager.amqp_api
        for command, original in list(amqp.handlers.items()):
            def wrapped(source_id, message_id, target_id, payload, timestamp,
                        _original=original, _command=command):
                heard.setdefault(source_id, []).append((time.time(), _command))
                print("  %s  %-16s %s" % (_stamp(time.time()), _command, names.get(source_id, source_id)),
                      flush=True)
                return _original(source_id, message_id, target_id, payload, timestamp)
            amqp.handlers[command] = wrapped

        print("\n" + "=" * 72)
        print("2. listening for %g minutes -- every message as it arrives" % minutes)
        print("=" * 72)
        start = time.time()
        await asyncio.sleep(minutes * 60)

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
    finally:
        await manager.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--minutes", type=float, default=10.0, help="how long to listen (default 10)")
    sys.exit(asyncio.run(main(parser.parse_args().minutes)))
