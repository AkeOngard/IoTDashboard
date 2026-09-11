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
# inconsistent with the restored chunks.
set -euo pipefail

cd "$(dirname "$0")/.."

dump="${1:?usage: restore.sh <file.dump>}"
[ -f "$dump" ] || { echo "no such file: $dump" >&2; exit 1; }

if [ -z "${DATABASE_URL:-}" ] && [ -f .env ]; then
  set -a; . ./.env; set +a
fi
: "${DATABASE_URL:?DATABASE_URL is not set (and no .env to read it from)}"
PG_EXEC="${PG_EXEC:-}"

# shellcheck disable=SC2086
psql_() { $PG_EXEC psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -qAt "$@"; }

tables=$(psql_ -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
if [ "$tables" != "0" ]; then
  echo "refusing to restore: database already has $tables tables in public." >&2
  echo "restore into a fresh volume (docker compose up -d db on an empty volume)." >&2
  exit 1
fi

echo "==> preparing TimescaleDB for restore"
psql_ -c "CREATE EXTENSION IF NOT EXISTS timescaledb"
psql_ -c "SELECT timescaledb_pre_restore()" >/dev/null

echo "==> pg_restore $dump"
# Not --exit-on-error: the dump re-issues CREATE EXTENSION timescaledb, which
# fails harmlessly because the hook above needed the extension first.
set +e
# shellcheck disable=SC2086
$PG_EXEC pg_restore --dbname="$DATABASE_URL" --no-owner --no-acl < "$dump"
status=$?
set -e

echo "==> finishing TimescaleDB restore"
psql_ -c "SELECT timescaledb_post_restore()" >/dev/null

rows=$(psql_ -c "SELECT count(*) FROM telemetry" 2>/dev/null || echo "?")
echo "==> done (pg_restore exit $status; telemetry rows now: $rows)"
if [ "$status" -ne 0 ]; then
  echo "    a non-zero exit is expected from the duplicate CREATE EXTENSION;" >&2
  echo "    check the output above for any error other than 'already exists'." >&2
fi
