"""Single-user password login.

Kept in a file next to the device labels, not in the database, and deliberately
so: controlling the house is the safety-critical part and history is not, so
losing the database must never lock the operator out of their own lights. The
same reasoning that lets the dashboard run with DATABASE_URL empty applies to
the thing standing in front of it.

No new dependencies. scrypt is in hashlib, HMAC is in hmac, and a session token
that only has to say "this browser proved it knows the password, until <time>"
does not need a JWT library.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: scrypt work factor. 16 MB and ~35 ms on a laptop, ~200 ms on a Pi 3B --
#: slow enough to make guessing expensive, fast enough not to be felt once a
#: month. Stored with each hash so it can be raised later without invalidating
#: the password already set.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
DKLEN = 32

SESSION_COOKIE = "iot_session"
#: A home dashboard is opened from the sofa; being asked to type a password
#: every day teaches the operator to pick a shorter one.
DEFAULT_TTL_HOURS = 30 * 24

MIN_PASSWORD_LENGTH = 10


class AuthError(Exception):
    """Wrong password, locked out, or no password set yet."""


# ------------------------------------------------------------------ hashing


def hash_password(password: str) -> dict[str, Any]:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DKLEN
    )
    return {
        "algorithm": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(derived).decode("ascii"),
    }


def verify_password(record: dict[str, Any], password: str) -> bool:
    if not record or record.get("algorithm") != "scrypt":
        return False
    try:
        salt = base64.b64decode(record["salt"])
        expected = base64.b64decode(record["hash"])
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(record["n"]),
            r=int(record["r"]),
            p=int(record["p"]),
            dklen=len(expected),
        )
    except (KeyError, ValueError, TypeError) as exc:
        log.warning("stored password record is unusable: %s", exc)
        return False
    return hmac.compare_digest(derived, expected)


# ------------------------------------------------------------------- store


class AuthStore:
    """The password, the session secret, and the generation counter that
    invalidates every outstanding session at once."""

    def __init__(self, path: str | os.PathLike[str] | None, secret: str = "") -> None:
        self.path = Path(path) if path else None
        self._record: dict[str, Any] = {}
        self._secret = secret
        self._generation = 1
        #: session id -> the moment its token would have expired anyway. A
        #: logout has to kill the token on the server, not just ask the browser
        #: to forget it: a copied cookie outlives a cleared one. Entries drop
        #: out once they pass their expiry, so this never grows past the
        #: sessions ended within one session lifetime.
        self._revoked: dict[str, int] = {}
        #: mtime of the file as last read. scripts/set_password.py writes from
        #: another process -- the whole point of it being a CLI -- so the
        #: running app has to notice rather than need a restart.
        self._mtime: float | None = None

    @property
    def configured(self) -> bool:
        """Is there a password to check against?"""
        return bool(self._record)

    @property
    def enabled(self) -> bool:
        """Is login being enforced at all?"""
        return self.path is not None

    def load(self) -> None:
        if self.path is None:
            # Running without a door is a legitimate choice for a laptop with
            # the mock adapter, and a serious mistake anywhere else -- so say
            # it every time rather than let it pass unnoticed.
            log.warning(
                "AUTH_PATH is empty: login is DISABLED and anyone who can reach "
                "this port can control the devices"
            )
            return
        data = self._read()
        self._apply(data)
        stored_secret = data.get("session_secret") or ""
        if not self._secret:
            # No SESSION_SECRET in the environment: keep one here so sessions
            # survive a restart without the operator having to configure
            # anything. Written on first use.
            self._secret = stored_secret or secrets.token_hex(32)
            if self._secret != stored_secret:
                self._save(data)
        if self.configured:
            log.info("password login is on (%s)", self.path)
        else:
            log.warning(
                "no password set; the dashboard refuses to serve until "
                "`python scripts/set_password.py` has been run"
            )

    def _read(self) -> dict[str, Any]:
        assert self.path is not None
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = None
            return {}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Refuse to run wide open on a corrupt file: an unreadable password
            # file must fail closed, unlike the labels it sits next to, where
            # the worst case is an ugly name.
            raise AuthError(
                "cannot read %s (%s); fix or delete it and set the password again"
                % (self.path, exc)
            ) from exc
        if not isinstance(loaded, dict):
            raise AuthError("%s does not contain a JSON object" % self.path)
        return loaded

    def reload_if_changed(self) -> None:
        """Pick up a password set or changed by the CLI, without a restart.

        One stat() per call, which is what lets the setup instructions say
        "run this, then reload the page" and have that be true.
        """
        if self.path is None:
            return
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime == self._mtime:
            return
        data = self._read()
        self._apply(data)
        stored = data.get("session_secret") or ""
        # An externally written secret wins: it is the one the file now signs
        # with, and disagreeing would silently reject every session.
        if stored and stored != self._secret:
            self._secret = stored
        log.info("re-read %s (password %s)",
                 self.path, "set" if self._record else "not set")

    def _apply(self, data: dict[str, Any]) -> None:
        self._record = data.get("password") or {}
        self._generation = int(data.get("generation") or 1)
        revoked = data.get("revoked") or {}
        self._revoked = {
            str(sid): int(until) for sid, until in revoked.items()
            if isinstance(until, (int, float))
        } if isinstance(revoked, dict) else {}

    def _save(self, extra: dict[str, Any] | None = None) -> None:
        if self.path is None:
            return
        now = time.time()
        self._revoked = {sid: until for sid, until in self._revoked.items() if until > now}
        payload = dict(extra or {})
        payload.update(
            {
                "version": 1,
                "password": self._record,
                "generation": self._generation,
                "session_secret": self._secret,
                "revoked": self._revoked,
            }
        )
        temp = self.path.with_name(self.path.name + ".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        # Best effort: on Windows there is no mode to speak of, and on Linux
        # the file is inside a volume only this container can reach anyway.
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, self.path)
        # Our own write is not news; do not re-read it on the next request.
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = None

    def set_password(self, password: str) -> None:
        problem = password_problem(password)
        if problem:
            raise AuthError(problem)
        self._record = hash_password(password)
        # Changing the password signs every other browser out, which is the
        # only reason someone changes it in a hurry.
        self._generation += 1
        # Every revoked token belonged to the old generation and is dead anyway.
        self._revoked = {}
        self._save()

    def check_password(self, password: str) -> bool:
        if not self.configured:
            raise AuthError("no password has been set")
        return verify_password(self._record, password)

    # ---------------------------------------------------------- session token

    # Format: <generation>.<expires>.<session id>.<signature>
    #
    # The session id is what makes a single logout possible. Without it, two
    # sign-ins in the same second produced byte-identical tokens, so ending one
    # would have ended both -- and there was nothing to name on a revocation
    # list anyway.

    def issue(self, ttl_seconds: float) -> tuple[str, int]:
        expires = int(time.time() + ttl_seconds)
        payload = "%d.%d.%s" % (self._generation, expires, secrets.token_urlsafe(12))
        return "%s.%s" % (payload, self._sign(payload)), expires

    def _parse(self, token: str | None) -> tuple[int, int, str] | None:
        """The verified (generation, expires, session id), or None.

        Checks only the signature -- whether the session is still usable is
        valid()'s business, and revoke() needs to name a token that valid()
        may already refuse.
        """
        if not token:
            return None
        parts = token.split(".")
        if len(parts) != 4:
            return None
        generation, expires, sid, signature = parts
        payload = "%s.%s.%s" % (generation, expires, sid)
        if not hmac.compare_digest(signature, self._sign(payload)):
            return None
        try:
            return int(generation), int(expires), sid
        except ValueError:
            return None

    def valid(self, token: str | None) -> bool:
        parsed = self._parse(token)
        if parsed is None:
            return False
        generation, expires, sid = parsed
        return (
            generation == self._generation
            and expires >= time.time()
            and sid not in self._revoked
        )

    def revoke(self, token: str | None) -> bool:
        """End one session for good. Returns whether there was one to end."""
        parsed = self._parse(token)
        if parsed is None:
            return False
        generation, expires, sid = parsed
        if generation != self._generation or expires < time.time() or sid in self._revoked:
            return False     # already dead; nothing worth remembering
        self._revoked[sid] = expires
        self._save()
        return True

    def revoke_all(self) -> None:
        """End every session, this one included, without changing the password.

        For "I signed in on a friend's phone and forgot": no need to invent a
        new password to get that browser out.
        """
        self._generation += 1
        self._revoked = {}
        self._save()

    def _sign(self, payload: str) -> str:
        digest = hmac.new(self._secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256)
        return base64.urlsafe_b64encode(digest.digest()).decode("ascii").rstrip("=")


def password_problem(password: str) -> str | None:
    """Why this password is not acceptable, or None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return "password must be at least %d characters" % MIN_PASSWORD_LENGTH
    if password.strip() != password:
        return "password must not start or end with a space"
    return None


# ---------------------------------------------------------------- throttle


class LoginThrottle:
    """Slow down guessing without letting a stranger lock the owner out.

    Per-address, because a global lockout would hand anyone who can reach the
    login page a way to shut the owner out of their own house. Behind Tailscale
    the address is the tailnet peer, which is exactly the granularity wanted.
    """

    def __init__(self, limit: int = 10, window: float = 900.0) -> None:
        self.limit = limit
        self.window = window
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(key, []) if now - t < self.window]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def blocked_for(self, key: str) -> float:
        """Seconds until this address may try again; 0 when it may try now."""
        now = time.time()
        recent = self._recent(key, now)
        if len(recent) < self.limit:
            return 0.0
        return max(0.0, self.window - (now - recent[0]))

    def record_failure(self, key: str) -> None:
        now = time.time()
        self._failures.setdefault(key, []).append(now)
        self._recent(key, now)

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)
