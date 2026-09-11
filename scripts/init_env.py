#!/usr/bin/env python
"""Fill the placeholders in a freshly copied .env with real random secrets.

Called by `make init`. Refuses to touch a .env that has no placeholders left,
so re-running it can never rotate a password out from under a live database.
"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: placeholder -> generator. The DB password placeholder appears twice
#: (POSTGRES_PASSWORD and inside DATABASE_URL) and must get the same value
#: in both, which a single replace of the whole file gives us for free.
PLACEHOLDERS = {
    "CHANGE_ME_RUN_MAKE_INIT": lambda: secrets.token_hex(32),
    # hex only: the value has to survive being pasted into a URL
    "CHANGE_ME_TOO": lambda: secrets.token_hex(16),
}


def main() -> int:
    env = ROOT / ".env"
    if not env.exists():
        print("no .env to initialise (copy .env.example first)", file=sys.stderr)
        return 1

    text = env.read_text(encoding="utf-8")
    filled = []
    for placeholder, generate in PLACEHOLDERS.items():
        if placeholder in text:
            text = text.replace(placeholder, generate())
            filled.append(placeholder)

    if not filled:
        print(".env has no placeholders left -- nothing to do")
        return 0

    env.write_text(text, encoding="utf-8")
    try:
        env.chmod(0o600)
    except OSError:
        pass  # Windows / non-POSIX filesystems
    print("generated secrets for: " + ", ".join(filled))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
