#!/usr/bin/env bash
# Sample a container's cgroup v2 cpu.stat once a second and print the
# throttling ratio as it moves.
#
# WHAT THIS DEMONSTRATES
#   `docker stats` shows you average CPU, which is structurally incapable
#   of revealing throttling: a container frozen 40% of the time and running
#   flat out the other 60% reports the same average as one loafing along
#   evenly. The delta of nr_throttled between two samples is the number
#   this topic exists to teach you to read.
#
# WHAT TO LOOK FOR IN THE OUTPUT
#   `thr/s` -- periods throttled in the last second, out of ~10. Anything
#   above 0.5 sustained is your latency story. `ratio` is cumulative since
#   container start, so it lags; watch the per-second column during a run
#   and the cumulative one afterwards.
#
# RUN
#   ./observe/watch.sh api            # a compose service name
#   ./observe/watch.sh api 60         # for 60 seconds then stop
set -euo pipefail

SERVICE="${1:-api}"
SECONDS_TO_RUN="${2:-0}"

if ! docker info >/dev/null 2>&1; then
  echo "docker daemon is not running -- start Docker Desktop." >&2
  echo "There is no host-side substitute: macOS has no cgroupfs." >&2
  exit 1
fi

read_stat() {
  docker compose exec -T "$SERVICE" cat /sys/fs/cgroup/cpu.stat 2>/dev/null \
    || docker exec -i "$SERVICE" cat /sys/fs/cgroup/cpu.stat
}

if ! read_stat >/dev/null 2>&1; then
  echo "cannot read /sys/fs/cgroup/cpu.stat inside '$SERVICE'." >&2
  echo "Either the service is not up, or its cgroup is not mounted." >&2
  exit 1
fi

printf 'quota: %s\n' "$(docker compose exec -T "$SERVICE" cat /sys/fs/cgroup/cpu.max 2>/dev/null || echo '?')"
printf '%8s %12s %10s %10s %8s %10s\n' \
  time usage_usec nr_periods nr_throttled thr/s ratio

prev_periods=0
prev_throttled=0
elapsed=0

while :; do
  stat="$(read_stat)"
  usage=$(awk '/^usage_usec/{print $2}' <<<"$stat")
  periods=$(awk '/^nr_periods/{print $2}' <<<"$stat")
  throttled=$(awk '/^nr_throttled/{print $2}' <<<"$stat")

  d_periods=$((periods - prev_periods))
  d_throttled=$((throttled - prev_throttled))
  if [ "$periods" -gt 0 ]; then
    ratio=$(awk -v t="$throttled" -v p="$periods" 'BEGIN{printf "%.3f", t/p}')
  else
    ratio="n/a"
  fi
  if [ "$d_periods" -gt 0 ]; then
    per_sec=$(awk -v t="$d_throttled" -v p="$d_periods" 'BEGIN{printf "%.2f", t/p}')
  else
    per_sec="-"
  fi

  printf '%8s %12s %10s %10s %8s %10s\n' \
    "$(date +%H:%M:%S)" "$usage" "$periods" "$throttled" "$per_sec" "$ratio"

  prev_periods=$periods
  prev_throttled=$throttled
  sleep 1
  elapsed=$((elapsed + 1))
  if [ "$SECONDS_TO_RUN" -gt 0 ] && [ "$elapsed" -ge "$SECONDS_TO_RUN" ]; then
    break
  fi
done
