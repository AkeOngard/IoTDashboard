#!/usr/bin/env python
"""Migration runner -- doc §12.

    migrate.py up       apply pending migrations
    migrate.py status   show applied / pending
    migrate.py verify   checksum-verify without applying
    migrate.py new NAME scaffold the next migration file

Properties the doc calls for:
  * idempotent          -- re-running applies nothing
  * checksum verified   -- an edited, already-applied migration is a hard error
  * advisory lock       -- two instances starting at once cannot race
  * auto-wait for DB    -- retries for up to 60s so `migrate` can start
                           alongside the database container
  * -- migrate:no-transaction directive for statements TimescaleDB refuses to
    run inside a transaction block (continuous aggregates)
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import MIGRATIONS_DIR, settings  # noqa: E402

log = logging.getLogger("migrate")

NAME_RE = re.compile(r"^(\d+)_(.+)\.sql$")
NO_TRANSACTION = "-- migrate:no-transaction"

#: Any 64-bit constant works; it just has to be the same in every instance.
ADVISORY_LOCK_KEY = 0x104D1657A7E

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT        NOT NULL,
    checksum    TEXT        NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str
    transactional: bool


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = NAME_RE.match(path.name)
        if not match:
            raise MigrationError("migration filename must be NNN_name.sql: " + path.name)
        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                path=path,
                sql=sql,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                transactional=NO_TRANSACTION not in "\n".join(sql.splitlines()[:3]),
            )
        )
    versions = [m.version for m in migrations]
    duplicates = sorted({v for v in versions if versions.count(v) > 1})
    if duplicates:
        raise MigrationError("duplicate migration versions: " + repr(duplicates))
    return migrations


def split_statements(sql: str) -> list[str]:
    """Split on semicolons that are not inside a string, identifier, comment or
    dollar-quoted block. Deliberately small -- enough for our migrations."""
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end
            continue
        if ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if ch in ("'", '"'):
            end = i + 1
            while end < n:
                if sql[end] == ch:
                    if end + 1 < n and sql[end + 1] == ch:  # escaped by doubling
                        end += 2
                        continue
                    break
                end += 1
            buf.append(sql[i : end + 1])
            i = end + 1
            continue
        if ch == "$":
            match = re.match(r"\$[A-Za-z_]*\$", sql[i:])
            if match:
                tag = match.group(0)
                end = sql.find(tag, i + len(tag))
                end = n if end == -1 else end + len(tag)
                buf.append(sql[i:end])
                i = end
                continue
        if ch == ";":
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


# --------------------------------------------------------------------- runner


async def connect(dsn: str, wait_seconds: float = 60.0) -> asyncpg.Connection:
    """Retry until the database accepts us, so the migrate container can be
    started in parallel with the database itself."""
    deadline = asyncio.get_running_loop().time() + wait_seconds
    delay = 1.0
    while True:
        try:
            return await asyncpg.connect(dsn, timeout=10)
        except Exception as exc:  # noqa: BLE001
            if asyncio.get_running_loop().time() >= deadline:
                raise MigrationError("database not reachable after %.0fs: %s" % (wait_seconds, exc))
            log.info("waiting for database (%s)", exc)
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 5.0)


async def _applied(conn: asyncpg.Connection) -> dict[int, tuple[str, str]]:
    await conn.execute(BOOTSTRAP)
    rows = await conn.fetch("SELECT version, name, checksum FROM schema_migrations")
    return {row["version"]: (row["name"], row["checksum"]) for row in rows}


def _verify(migrations: list[Migration], known: dict[int, tuple[str, str]]) -> list[str]:
    drift: list[str] = []
    for migration in migrations:
        if migration.version in known and known[migration.version][1] != migration.checksum:
            drift.append(migration.path.name)
    return drift


async def up(conn: asyncpg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    migrations = discover(directory)
    # Serialise concurrent starts; released automatically when the session ends.
    await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
    try:
        known = await _applied(conn)
        drift = _verify(migrations, known)
        if drift:
            raise MigrationError(
                "checksum drift in " + ", ".join(drift)
                + " -- an applied migration was edited; add a new one instead"
            )
        applied: list[str] = []
        for migration in migrations:
            if migration.version in known:
                continue
            log.info("applying %s", migration.path.name)
            if migration.transactional:
                async with conn.transaction():
                    await conn.execute(migration.sql)
                    await _record(conn, migration)
            else:
                # A multi-statement execute() uses the simple query protocol,
                # which Postgres wraps in one implicit transaction -- exactly
                # what these migrations must avoid. So: one at a time.
                for statement in split_statements(migration.sql):
                    await conn.execute(statement)
                await _record(conn, migration)
            applied.append(migration.path.name)
        return applied
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)


async def _record(conn: asyncpg.Connection, migration: Migration) -> None:
    await conn.execute(
        "INSERT INTO schema_migrations (version, name, checksum) VALUES ($1, $2, $3)",
        migration.version,
        migration.name,
        migration.checksum,
    )


async def status(conn: asyncpg.Connection, directory: Path = MIGRATIONS_DIR) -> dict:
    migrations = discover(directory)
    known = await _applied(conn)
    pending = [m.path.name for m in migrations if m.version not in known]
    return {
        "applied": sorted(known),
        "pending": pending,
        "drift": _verify(migrations, known),
        "up_to_date": not pending,
    }


# ------------------------------------------------------------------------ cli


def scaffold(name: str, directory: Path = MIGRATIONS_DIR) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "migration"
    version = max((m.version for m in discover(directory)), default=0) + 1
    path = directory / ("%03d_%s.sql" % (version, slug))
    path.write_text(
        "-- %s\n-- created %s\n\n"
        % (path.name, datetime.now(timezone.utc).strftime("%Y-%m-%d")),
        encoding="utf-8",
    )
    return path


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("up", "status", "verify", "new"))
    parser.add_argument("name", nargs="?", help="migration name, for `new`")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    if args.command == "new":
        if not args.name:
            parser.error("`new` needs a name")
        print("created", scaffold(args.name))
        return 0

    if not settings.database_url:
        print("DATABASE_URL is empty -- nothing to do", file=sys.stderr)
        return 1

    conn = await connect(settings.database_url)
    try:
        if args.command == "up":
            applied = await up(conn)
            print("applied: " + ", ".join(applied) if applied else "already up to date")
        elif args.command == "status":
            state = await status(conn)
            print("applied :", ", ".join("%03d" % v for v in state["applied"]) or "none")
            print("pending :", ", ".join(state["pending"]) or "none")
            if state["drift"]:
                print("DRIFT   :", ", ".join(state["drift"]))
                return 1
        elif args.command == "verify":
            state = await status(conn)
            if state["drift"]:
                print("checksum drift: " + ", ".join(state["drift"]), file=sys.stderr)
                return 1
            print("checksums ok (%d applied, %d pending)"
                  % (len(state["applied"]), len(state["pending"])))
    except MigrationError as exc:
        print("error:", exc, file=sys.stderr)
        return 1
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
