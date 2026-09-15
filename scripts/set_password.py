#!/usr/bin/env python
"""Set (or change) the dashboard password.

    docker exec -it iot-app python scripts/set_password.py
    python scripts/set_password.py --stdin < password.txt     # scripted

Deliberately not a web page. Letting whoever loads the dashboard first choose
the password is not a lock, so the first one is set from a shell on the host --
the same reasoning that keeps commissioning off the HTTP API by default.

Changing it signs every browser out, including the one you are reading this on.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.auth import MIN_PASSWORD_LENGTH, AuthStore, password_problem  # noqa: E402
from app.config import settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stdin", action="store_true",
        help="read the password from stdin instead of prompting (no confirmation)",
    )
    parser.add_argument("--path", default=settings.auth_path, help="auth file to write")
    args = parser.parse_args()

    if not args.path:
        print("AUTH_PATH is empty, so login is disabled; nothing to set.", file=sys.stderr)
        return 1

    store = AuthStore(args.path, settings.session_secret)
    store.load()
    changing = store.configured

    if args.stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        if changing:
            print("A password is already set. Changing it signs out every browser.")
        print("Password must be at least %d characters." % MIN_PASSWORD_LENGTH)
        password = getpass.getpass("New password: ")
        if password != getpass.getpass("Repeat: "):
            print("they do not match", file=sys.stderr)
            return 1

    problem = password_problem(password)
    if problem:
        print(problem, file=sys.stderr)
        return 1

    try:
        store.set_password(password)
    except OSError as exc:
        print("could not write %s: %s" % (args.path, exc), file=sys.stderr)
        return 1

    print("password %s in %s" % ("changed" if changing else "set", args.path))
    if changing:
        print("every signed-in browser has been signed out.")
    else:
        print("open the dashboard and sign in.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
