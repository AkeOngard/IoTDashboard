#!/usr/bin/env bash
# Restore a dump made by scripts/backup.sh into an EMPTY database.
#
#   PG_EXEC="docker exec -i iot-db" bash scripts/restore.sh ops/backups/iot-<stamp>.dump
#
# Procedure (see DEPLOY-CLOUD.md / DEPLOY-PI.md):
#   1. start only the database on a fresh volume   (docker compose up -d db)
#   2. run this script
#   3. start the rest                              (make prod-up)
# The dump carries schema_migrations with its checksums, so the migrate step
# in (3) finds nothing pending and the app comes up on the restored data.
#
# TimescaleDB requires timescaledb_pre_restore() / timescaledb_post_restore()
# around pg_restore; without them its catalog and background jobs end up
# inconsistent with the restored chunks. On a plain PostgreSQL target -- a
# managed free tier, which is where history lives when there is no VM -- those
# hooks do not exist and asking for them aborts the restore before it starts.
set -euo pipefail

cd "$(dirname "$0")/.."

dump="${1:?usage: restore.sh <file.dump>}"
[ -f "$dump" ] || { echo "no such file: $dump" >&2; exit 1; }

# The env file sits wherever the deployment does: the repo root for the full
# compose stack, ops/pi/.env for the Pi-only one that keeps history in a
# managed database.
if [ -z "${DATABASE_URL:-}" ]; then
  for env_file in .env ops/pi/.env; do
    [ -f "$env_file" ] || continue
    set -a; . "./$env_file"; set +a
    if [ -n "${DATABASE_URL:-}" ]; then break; fi
  done
fi
: "${DATABASE_URL:?DATABASE_URL is not set, and neither .env nor ops/pi/.env defines it}"
PG_EXEC="${PG_EXEC:-}"

# Nothing to dump with? Say so usefully. Raspberry Pi OS does not install the
# PostgreSQL client, and the deployment that needs this most -- Pi plus a
# managed database -- is exactly the one without it. Docker is already there.
missing_client() {
  echo "$1 is not installed on this host." >&2
  echo >&2
  echo "Run one from a container instead, matching your server's major version:" >&2
  echo >&2
  echo "  PG_EXEC=\"docker run --rm -i postgres:17\" bash $2" >&2
  echo >&2
  echo "Check the version with:" >&2
  echo "  docker exec iot-app psql \"\$DATABASE_URL\" -tAc \"SHOW server_version\"" >&2
  exit 1
}

if [ -z "$PG_EXEC" ] && ! command -v pg_restore >/dev/null 2>&1; then
  missing_client pg_restore "scripts/restore.sh $dump"
fi

# shellcheck disable=SC2086
psql_() { $PG_EXEC psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -qAt "$@"; }

tables=$(psql_ -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
if [ "$tables" != "0" ]; then
  echo "refusing to restore: database already has $tables tables in public." >&2
  echo "restore into a fresh volume (docker compose up -d db on an empty volume)." >&2
  exit 1
fi

# What can the target actually do, and what does the dump need?
target_ts=$(psql_ -c "SELECT count(*) FROM pg_available_extensions WHERE name = 'timescaledb'")
# shellcheck disable=SC2086
dump_ts=$($PG_EXEC pg_restore -l < "$dump" 2>/dev/null | grep -ci 'EXTENSION - timescaledb' || true)

if [ "$dump_ts" != "0" ] && [ "$target_ts" = "0" ]; then
  echo "refusing to restore: this dump was taken from a TimescaleDB server," >&2
  echo "but the target has no timescaledb extension available. Its hypertable" >&2
  echo "chunks would have nowhere to land. Restore onto TimescaleDB instead." >&2
  exit 1
fi

if [ "$target_ts" != "0" ]; then
  echo "==> preparing TimescaleDB for restore"
  psql_ -c "CREATE EXTENSION IF NOT EXISTS timescaledb"
  psql_ -c "SELECT timescaledb_pre_restore()" >/dev/null
else
  echo "==> plain PostgreSQL target; no TimescaleDB hooks needed"
fi

echo "==> pg_restore $dump"
# Not --exit-on-error: a TimescaleDB dump re-issues CREATE EXTENSION
# timescaledb, which fails harmlessly because the hook above needed the
# extension first.
set +e
# shellcheck disable=SC2086
$PG_EXEC pg_restore --dbname="$DATABASE_URL" --no-owner --no-acl < "$dump"
status=$?
set -e

if [ "$target_ts" != "0" ]; then
  echo "==> finishing TimescaleDB restore"
  psql_ -c "SELECT timescaledb_post_restore()" >/dev/null
fi

rows=$(psql_ -c "SELECT count(*) FROM telemetry" 2>/dev/null || echo "?")
echo "==> done (pg_restore exit $status; telemetry rows now: $rows)"
if [ "$status" -ne 0 ]; then
  if [ "$target_ts" != "0" ]; then
    echo "    a non-zero exit is expected from the duplicate CREATE EXTENSION;" >&2
  fi
  echo "    check the output above for any error other than 'already exists'." >&2
fi
