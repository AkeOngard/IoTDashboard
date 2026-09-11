#!/usr/bin/env bash
# Measure whether this Pi can carry matter-server + the dashboard.
#
#   bash memwatch.sh                 # sample every 60 s for 24 h (Ctrl+C = stop early)
#   bash memwatch.sh 30 6            # every 30 s for 6 h
#   bash memwatch.sh --report FILE   # re-print the verdict for an earlier run
#
# One `docker stats` right after boot proves nothing: matter-server grows once
# the hub is commissioned and its subscriptions are live. Run this for a day
# with the dashboard open now and then, and let the minimum of MemAvailable
# decide.
set -euo pipefail

# Verdict thresholds on the lowest MemAvailable seen (MB).
COMFORTABLE_MB=200
TIGHT_MB=100

PROC_ROOT="${PROC_ROOT:-/proc}"   # overridable for tests only

container_mb() {  # $1 = container name -> resident memory (MB), or empty if not running
  # Summed VmRSS of every process in the container, found through its cgroup
  # path. Deliberately not `docker stats`: Raspberry Pi OS ships with the
  # memory cgroup controller disabled, and then docker stats reports 0 B.
  local id f pid rss total=0
  [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ] || { echo ""; return; }
  id=$(docker inspect -f '{{.Id}}' "$1" 2>/dev/null) || { echo ""; return; }
  for f in "$PROC_ROOT"/[0-9]*/cgroup; do
    grep -q "$id" "$f" 2>/dev/null || continue
    pid=${f%/cgroup}; pid=${pid##*/}
    rss=$(awk '$1 == "VmRSS:" { print $2 }' "$PROC_ROOT/$pid/status" 2>/dev/null)
    total=$(( total + ${rss:-0} ))
  done
  echo $(( (total + 512) / 1024 ))
}

memcg_enabled() {
  grep -qw memory /sys/fs/cgroup/cgroup.controllers 2>/dev/null \
    || awk '$1 == "memory" && $4 == 1 { found = 1 } END { exit !found }' /proc/cgroups 2>/dev/null
}

meminfo_mb() {  # $1 = /proc/meminfo field -> MB
  awk -v k="$1:" '$1 == k { printf "%d", $2 / 1024 }' /proc/meminfo
}

oom_events() {
  { journalctl -k --no-pager 2>/dev/null || sudo -n dmesg 2>/dev/null || dmesg 2>/dev/null || true; } \
    | grep -ciE 'out of memory|oom-kill|killed process' || true
}

report() {
  local csv="$1"
  [ -s "$csv" ] || { echo "no samples in $csv" >&2; exit 1; }
  awk -F, -v ok="$COMFORTABLE_MB" -v tight="$TIGHT_MB" -v oom="${2:-}" '
    NR == 1 { next }
    {
      n++
      if (min_avail == "" || $3 < min_avail) { min_avail = $3; at = $1 }
      if ($5 > max_swap) max_swap = $5
      if ($6 != "" && $6 > max_matter) max_matter = $6
      if ($7 != "" && $7 > max_app) max_app = $7
      total = $2; first = (first == "" ? $1 : first); last = $1
    }
    END {
      printf "\n== memwatch: %d samples, %s -> %s ==\n", n, first, last
      printf "  RAM total                 : %d MB\n", total
      printf "  lowest MemAvailable       : %d MB   (at %s)\n", min_avail, at
      printf "  peak swap used            : %d MB\n", max_swap
      printf "  peak matter-server        : %s\n", (max_matter == "" ? "not running" : max_matter " MB")
      printf "  peak iot-app              : %s\n", (max_app == "" ? "not running" : max_app " MB")
      if (oom != "") printf "  kernel OOM events (boot)  : %s\n", oom
      printf "\n  verdict: "
      if (oom + 0 > 0 || min_avail < tight)
        printf "NOT VIABLE -- move the dashboard to the cloud (DEPLOY-CLOUD.md section 3)\n"
      else if (min_avail < ok)
        printf "TIGHT -- runs, but little headroom; watch it before adding history\n"
      else
        printf "COMFORTABLE -- this Pi can host the dashboard (history via Supabase is next)\n"
    }' "$csv"
}

if [ "${1:-}" = "--report" ]; then
  report "${2:?usage: memwatch.sh --report FILE}"
  exit 0
fi

interval="${1:-60}"
hours="${2:-24}"
samples=$(( hours * 3600 / interval ))
csv="memwatch-$(date +%Y%m%d-%H%M%S).csv"
oom_before=$(oom_events)

echo "time,mem_total_mb,mem_available_mb,swap_total_mb,swap_used_mb,matter_server_mb,iot_app_mb" > "$csv"
finish() {
  local oom_now; oom_now=$(oom_events)
  report "$csv" "$(( oom_now - oom_before ))"
  echo "  samples saved to $csv"
}
trap finish EXIT
trap 'exit 0' INT TERM

echo "sampling every ${interval}s for ${hours}h -> $csv   (Ctrl+C to stop early)"
if ! memcg_enabled; then
  echo "note: memory cgroup is off -- the 256m cap on iot-app is not enforced (see DEPLOY-CLOUD.md 2.5)."
  echo "      Measurements below are unaffected: they read /proc directly."
fi
for (( i = 0; i < samples; i++ )); do
  total=$(meminfo_mb MemTotal)
  avail=$(meminfo_mb MemAvailable)
  swap_total=$(meminfo_mb SwapTotal)
  swap_used=$(( swap_total - $(meminfo_mb SwapFree) ))
  matter=$(container_mb matter-server)
  app=$(container_mb iot-app)
  now=$(date +%F\ %T)
  echo "$now,$total,$avail,$swap_total,$swap_used,$matter,$app" >> "$csv"
  printf '%s  available %4s MB  swap %3s MB  matter-server %4s MB  app %4s MB\n' \
    "$now" "$avail" "$swap_used" "${matter:--}" "${app:--}"
  sleep "$interval"
done
