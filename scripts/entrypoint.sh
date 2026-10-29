#!/usr/bin/env sh
# Container entrypoint (doc §12): migrate | serve | dev | shell | psql
set -eu

cmd="${1:-serve}"
shift 2>/dev/null || true

case "$cmd" in
  migrate)
    exec python scripts/migrate.py up
    ;;
  serve)
    # --no-server-header: no "server: uvicorn" in every response, which tells
    # a scanner which software -- and so which known flaws -- to try.
    exec python -m uvicorn app.main:app \
      --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" \
      --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}" \
      --no-server-header
    ;;
  dev)
    exec python -m uvicorn app.main:app \
      --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" --reload --no-server-header
    ;;
  shell)
    exec python
    ;;
  psql)
    # DATABASE_URL is postgresql://... which psql accepts verbatim.
    exec psql "${DATABASE_URL:?DATABASE_URL is not set}"
    ;;
  *)
    exec "$cmd" "$@"
    ;;
esac
