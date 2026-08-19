#!/usr/bin/env bash
# 7.1 on the real thing: three container configs, two host states, six cells.
#
# WHAT THIS DEMONSTRATES
#   The same 2x3 table as ../python/three_knobs.py, but with the kernel
#   doing the accounting instead of a userspace model -- and with the one
#   column Darwin cannot produce: what cpu.weight does to the split when
#   two cgroups want the CPU at the same instant.
#
#     (a) cpu_shares: 512  -> cpu.weight (a ratio)      costs nothing at
#                             idle, decides the split only under contention.
#     (b) cpus: 1.0        -> cpu.max "100000 100000"   an absolute ceiling.
#                             Bites at idle. Bites identically when busy.
#     (c) cpuset: "0"      -> cpuset.cpus "0"           narrows which CPUs,
#                             never freezes: nr_throttled stays 0.
#
#   The lettering matches the README's table. WORKERS defaults to 4 here and
#   not to the harness's 1 for a reason worth stating: one uvicorn process is
#   one runnable thread, and one thread cannot consume more than 100ms of CPU
#   in a 100ms period, so it can NEVER exhaust a 1.0-CPU quota. Run row (b)
#   with WORKERS=1 and nr_throttled is 0 by arithmetic, not by good fortune,
#   and the row silently stops demonstrating anything.
#
#   All six cells are driven by this script. Nothing here asks you to edit
#   docker-compose.yml between runs -- the (b) and (c) rows arrive through
#   generated Compose override files, because a knob you have to remember to
#   hand-edit is a knob that ends up set twice.
#
# WHAT TO LOOK FOR IN THE OUTPUT
#   For each config it prints what Docker ACTUALLY wrote into the cgroup
#   before it prints any measurement. Do that first, always. If `cpu.max`
#   says "max 100000" under config (a), Compose never applied the limit and
#   every number after it is meaningless -- which is the failure this script
#   checks for explicitly rather than leaving you to notice.
#
#   Then read the three rows against each other:
#     - (b) should look the SAME idle and contended. That is what a quota is.
#     - (a) should look fine idle and get worse contended, with nr_throttled
#           still 0 -- a weight cannot freeze you, it can only lose a race.
#     - (c) should show nr_throttled 0 in both states. If it does not, a
#           quota is still set and you are measuring two knobs at once.
#
# RUN
#   ./run_7_1.sh                 # needs a running Docker daemon
#   RATE=60 DURATION=20s ./run_7_1.sh
set -euo pipefail

# Resolve this script's own directory BEFORE changing directory: after the
# cd below a relative $0 no longer resolves.
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../../00-harness"

if ! docker info >/dev/null 2>&1; then
  cat >&2 <<'MSG'
docker daemon is not running.
There is no host-side substitute on macOS: Darwin has no cgroupfs, so
cpu.weight, cpu.max and cpuset.cpus do not exist to be read or set.
Start Docker Desktop and re-run. For the parts that CAN be measured on
this host, run ../python/three_knobs.py instead.
MSG
  exit 1
fi

# RATE has to land in a window, and the window is narrow. /cpu costs ~15ms of
# CPU, so RATE requests/second is RATE*0.015 CPUs of offered demand. Too low
# (RATE 40 -> 0.6 CPU) and the 1.0-CPU quota row never throttles at all, so
# the table has nothing in it. Too high and even the no-ceiling row saturates
# and every cell reports the same queue-explosion p99 instead of the knob.
# 60 -> ~0.9 CPU offered: comfortable for the two unlimited configs on this
# 4-CPU VM, right up against the ceiling for the quota one. Re-derive it for
# your own machine; do not copy the number.
RATE="${RATE:-60}"
DURATION="${DURATION:-30s}"
ENDPOINT="${ENDPOINT:-/cpu}"
WORKERS="${WORKERS:-4}"
export WORKERS

TMPDIR_7_1="$(mktemp -d)"
cleanup() {
  docker compose --profile contend stop hog >/dev/null 2>&1 || true
  docker compose --profile contend rm -f hog >/dev/null 2>&1 || true
  rm -rf "$TMPDIR_7_1"
}
trap cleanup EXIT

# (b) and (c) need `cpus:` gone, not merely different: Compose will set all
# three knobs at once without complaint, and the resulting table tells you
# nothing. API_CPUS=0 is the Compose spelling of "no quota".
cat > "$TMPDIR_7_1/weight.yml" <<'YML'
services:
  api:
    cpu_shares: 512
YML
cat > "$TMPDIR_7_1/cpuset.yml" <<'YML'
services:
  api:
    cpuset: "0"
YML

# python:3.13-slim has no `ps` (no procps package), so `ps ax | grep -c uvicorn`
# silently reports 0 for every configuration -- a column that is always wrong
# is worse than no column. Count uvicorn's forked workers through /proc, which
# is always there. WORKERS=1 means uvicorn serves in the master process and
# forks nothing at all, so that case is named rather than printed as 0.
worker_count() {
  docker compose exec -T api sh -c '
    n=0
    for d in /proc/[0-9]*; do
      tr "\0" "\n" < "$d/cmdline" 2>/dev/null | grep -q -- "--multiprocessing-fork" && n=$((n+1))
    done
    if [ "$n" -eq 0 ]; then echo "1 (uvicorn serving in-process, no forks)"; else echo "$n forked workers"; fi
  ' 2>/dev/null || echo "?"
}

show_cgroup() {
  echo "  --- what Docker actually wrote ---"
  for f in cpu.weight cpu.max cpuset.cpus.effective; do
    printf '    %-24s %s\n' "$f" \
      "$(docker compose exec -T api cat /sys/fs/cgroup/$f 2>/dev/null || echo '<unreadable>')"
  done
}

# want_cpu_max is the exact string cpu.max must hold. "max 100000" is the
# no-quota spelling; refusing to measure anything else is the point.
assert_cpu_max() {
  local want="$1" got
  got="$(docker compose exec -T api cat /sys/fs/cgroup/cpu.max | tr -d '\r')"
  if [ "$got" != "$want" ]; then
    echo "BROKEN: cpu.max is '$got', expected '$want'." >&2
    echo "Compose did not apply what this cell meant, or you restarted" >&2
    echo "instead of recreating. Everything downstream is meaningless." >&2
    exit 2
  fi
}

# A recreated container has cold workers: main.py calibrates /cpu with 32
# sha256 rounds at import, once per uvicorn worker, and under `cpuset: "0"`
# five workers do that calibration on one CPU. A fixed `sleep 4` after the
# recreate is not enough, and the leftover warm-up lands in the first
# seconds of the measurement as a multi-second p99 that has nothing to do
# with the knob under test. Wait for the port, then spend real requests.
warmup() {
  local i
  for i in $(seq 1 60); do
    curl -fsS -m 2 http://localhost:8000/healthz >/dev/null 2>&1 && break
    sleep 1
  done
  for i in $(seq 1 40); do curl -fsS -m 15 http://localhost:8000/cpu >/dev/null 2>&1 || true; done
}

# A recreate, not a restart: a restart reuses the old cgroup and you measure
# the previous config while believing you changed it.
recreate() {
  local extra="$1"
  if [ -n "$extra" ]; then
    docker compose -f docker-compose.yml -f "$extra" up -d --force-recreate api >/dev/null
  else
    docker compose up -d --force-recreate api >/dev/null
  fi
  warmup
}

run_cell() {
  local label="$1" want_cpu_max="$2"
  echo
  echo "=== $label ==="
  show_cgroup
  assert_cpu_max "$want_cpu_max"
  printf '    %-24s %s\n' "workers running" "$(worker_count)"
  before="$(docker compose exec -T api cat /sys/fs/cgroup/cpu.stat)"
  docker compose --profile load run --rm --no-deps \
    -e RATE="$RATE" -e DURATION="$DURATION" -e ENDPOINT="$ENDPOINT" \
    k6 run /scripts/steady.js 2>&1 \
    | grep -E '^(endpoint|offered|completed|p50|p99|max|dropped_iterations)' || true
  after="$(docker compose exec -T api cat /sys/fs/cgroup/cpu.stat)"
  echo "  cpu.stat delta over the run:"
  join <(sort <<<"$before") <(sort <<<"$after") \
    | awk '{printf "    %-16s %s\n", $1, $3-$2}'
  join <(sort <<<"$before") <(sort <<<"$after") | awk -v dur="$DURATION" '
    $1=="nr_periods"   {p=$3-$2}
    $1=="nr_throttled" {t=$3-$2}
    $1=="usage_usec"   {u=$3-$2}
    END {
      if (p>0) printf "    %-16s %.3f\n", "throttle ratio", t/p;
      if (p>0) printf "    %-16s %.2f CPU  (usage_usec / periods*100ms)\n", "avg CPU used", u/(p*100000);
    }'
}

cat <<'MSG'
7.1 -- the three knobs are not the same knob, against the harness.

Only ONE CPU knob is active in any cell below. Docker will happily set all
three at once, and the resulting table tells you nothing.
MSG
echo "  endpoint $ENDPOINT at $RATE req/s for $DURATION per cell, six cells"
echo "  api runs WORKERS=$WORKERS uvicorn processes (see the note in the header)"

for state in idle contended; do
  if [ "$state" = contended ]; then
    echo
    echo "############ starting the hog: every core busy, cpu_shares 2 ############"
    docker compose --profile contend up -d hog >/dev/null
    sleep 5
    echo "  hog CPU right now (proof it is actually burning, not just Up):"
    docker stats --no-stream --format '    {{.Name}} {{.CPUPerc}}' \
      "$(docker compose ps -q hog)" 2>/dev/null || true
  fi

  API_CPUS=0 recreate "$TMPDIR_7_1/weight.yml"
  run_cell "$state / (a) cpu_shares: 512  -- cpu.weight, a ratio" "max 100000"

  API_CPUS=1.0 recreate ""
  run_cell "$state / (b) cpus: 1.0        -- cpu.max, an absolute ceiling" "100000 100000"

  API_CPUS=0 recreate "$TMPDIR_7_1/cpuset.yml"
  run_cell "$state / (c) cpuset: \"0\"      -- one CPU, never a freeze" "max 100000"
done

echo
echo "Reset the stack when you are done:"
echo "  WORKERS=1 API_CPUS=1.0 docker compose up -d --force-recreate api"
