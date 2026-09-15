#!/usr/bin/env bash
# Full logical backup + retention (doc §12, §17).
#
# One pg_dump of the whole database, on purpose. TimescaleDB keeps hypertable
# rows in chunk tables under _timescaledb_internal, so dumping by table name
# (`pg_dump -t telemetry`) captures an empty parent and silently loses every
# reading. Restore with scripts/restore.sh, which runs TimescaleDB's
# pre/post-restore hooks.
#
#   bash scripts/backup.sh                                  # host pg_dump
#   PG_EXEC="docker exec -i iot-db" bash scripts/backup.sh  # server's own pg_dump
#
# Prefer PG_EXEC: pg_dump refuses to dump a server newer than itself, and
# Raspberry Pi OS ships client 15 against our PostgreSQL 16.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -z "${DATABASE_URL:-}" ] && [ -f .env ]; then
  set -a; . ./.env; set +a
fi
: "${DATABASE_URL:?DATABASE_URL is not set (and no .env to read it from)}"

BACKUP_DIR="${BACKUP_DIR:-ops/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
PG_EXEC="${PG_EXEC:-}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"
out="$BACKUP_DIR/iot-$STAMP.dump"
partial="$out.partial"

# pg_dump refuses a server newer than itself, and the failure message says
# nothing about what to do. Managed Postgres moves ahead of whatever client the
# host has -- Raspberry Pi OS ships 15 -- so check first and name the fix.
server_num=$($PG_EXEC psql "$DATABASE_URL" -tAc "SHOW server_version_num" 2>/dev/null | tr -dc '0-9' || true)
client_major=$($PG_EXEC pg_dump --version 2>/dev/null | sed -n 's/.*PostgreSQL) \([0-9][0-9]*\).*/\1/p' || true)
if [ -n "$server_num" ] && [ -n "$client_major" ] &&
   [ "$(( server_num / 10000 ))" -gt "$client_major" ]; then
  server_major=$(( server_num / 10000 ))
  echo "pg_dump is version $client_major but the server is $server_major;" >&2
  echo "pg_dump will not dump a server newer than itself. Run a matching one:" >&2
  echo >&2
  echo "  PG_EXEC=\"docker run --rm -i postgres:$server_major\" bash scripts/backup.sh" >&2
  echo >&2
  exit 1
fi

echo "==> pg_dump -> $out"
# Stream to stdout so the file lands on the host even when pg_dump runs inside
# the database container. Write to .partial first: a dump cut short by a full
# disk must never sit in the backup folder looking like a good one.
# shellcheck disable=SC2086  # PG_EXEC is a command prefix and must word-split
$PG_EXEC pg_dump "$DATABASE_URL" --format=custom --no-owner --no-acl > "$partial"
mv "$partial" "$out"

echo "==> pruning backups older than ${RETENTION_DAYS} days"
find "$BACKUP_DIR" -name 'iot-*.dump' -type f -mtime "+${RETENTION_DAYS}" -print -delete
find "$BACKUP_DIR" -name '*.partial' -type f -mtime +1 -print -delete

echo "==> done"
ls -lh "$BACKUP_DIR" | tail -n +2
