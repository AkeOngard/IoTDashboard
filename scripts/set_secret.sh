#!/usr/bin/env sh
# Keep one setting in a file of its own instead of in .env.
#
#   sudo sh scripts/set_secret.sh toshiba_password                 # Pi: ops/pi/secrets/
#   sudo sh scripts/set_secret.sh database_url secrets             # full stack: ./secrets/
#
# The value is asked for without being shown, so it lands neither on the
# screen nor in the shell's history. The file goes to the container's user
# (uid 10001, see the Dockerfile) and nobody else: mode 400 inside a 700
# directory. Reading it back needs sudo -- which is the point.
#
# The app reads /run/secrets/<name> before anything in .env or the
# environment (app/config.py). Restart it afterwards: docker restart iot-app
set -eu

APP_UID=10001

name="${1:?usage: sudo sh scripts/set_secret.sh <setting_name> [directory]}"
dir="${2:-$(dirname "$0")/../ops/pi/secrets}"

case "$name" in
  ''|*[!a-z0-9_]*)
    echo "the name is a setting in lower case, e.g. toshiba_password or database_url" >&2
    exit 1 ;;
esac
if [ "$(id -u)" != 0 ]; then
  echo "run with sudo: the file is handed to the container's user (uid $APP_UID)" >&2
  exit 1
fi

# This runs as root and hands things to another user, so never follow a
# symlink: a link planted at the directory would have chown/chmod land on
# whatever it points to (/etc, say).
if [ -L "$dir" ]; then
  echo "$dir is a symlink; refusing (make it a plain directory)" >&2
  exit 1
fi
mkdir -p "$dir"
chown -h "$APP_UID:$APP_UID" "$dir"
chmod 700 "$dir"

# Put the echo back however this ends, Ctrl-C included.
if [ -t 0 ]; then
  trap 'stty echo 2>/dev/null; echo >&2' EXIT INT TERM
  stty -echo
fi
printf 'value for %s (not shown): ' "$name" >&2
IFS= read -r value || true
if [ -t 0 ]; then stty echo; echo >&2; fi

if [ -z "$value" ]; then
  echo "empty; nothing written" >&2
  exit 1
fi

umask 077
# mktemp creates the file itself (O_EXCL), so no link planted under a
# guessable name can redirect the write.
temp="$(mktemp "$dir/.$name.XXXXXX")"
printf '%s' "$value" > "$temp"
chown "$APP_UID:$APP_UID" "$temp"
chmod 400 "$temp"
# rename() replaces whatever is at the name, a link included, without
# following it.
mv -f "$temp" "$dir/$name"
echo "wrote $dir/$name -- restart the app to use it: docker restart iot-app" >&2
printf 'then delete the %s line from .env, if there is one\n' "$(echo "$name" | tr '[:lower:]' '[:upper:]')" >&2
