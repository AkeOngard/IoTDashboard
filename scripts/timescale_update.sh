#!/usr/bin/env sh
# Bring the TimescaleDB extension up to the version in the running image.
#
#   make db-upgrade        (pipes this into the db container)
#
# A newer timescale/timescaledb image keeps serving an old database as it
# is: it ships the old extension's library too. The new code only runs once
# each database is told, with ALTER EXTENSION ... UPDATE -- which must be
# the first statement of a fresh session (psql -X, one connection per
# database), or it fails with "extension ... already loaded". template1
# holds the extension as well, so databases made later start current.
#
# Runs inside the db container, where POSTGRES_USER is set. Safe to repeat:
# an extension already current is left as it is.
set -eu

user="${POSTGRES_USER:?POSTGRES_USER is not set -- run this in the db container}"
for db in $(psql -X -At -U "$user" -d postgres -c "SELECT datname FROM pg_database WHERE datallowconn ORDER BY 1"); do
  has=$(psql -X -At -U "$user" -d "$db" -c "SELECT count(*) FROM pg_extension WHERE extname = 'timescaledb'")
  [ "$has" = 1 ] || continue
  before=$(psql -X -At -U "$user" -d "$db" -c "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
  psql -X -q -U "$user" -d "$db" -c "ALTER EXTENSION timescaledb UPDATE;" 2>&1 | grep -v "job history\|already installed" || true
  after=$(psql -X -At -U "$user" -d "$db" -c "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
  echo "$db: timescaledb $before -> $after"
done
