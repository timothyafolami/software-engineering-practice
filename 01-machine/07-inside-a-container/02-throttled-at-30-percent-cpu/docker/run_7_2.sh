#!/usr/bin/env bash
# 7.2 on the real thing: the baseline failure, then the four fixes, one
# variable at a time, with the kernel doing the accounting.
#
# WHAT THIS DEMONSTRATES
#   The headline experiment of this topic against the harness's FastAPI
#   service: 4 uvicorn workers under a 1.0 CPU quota, driven at a rate low
#   enough that average CPU looks fine, then the four fixes from the
#   README applied individually:
#
#     1. drop to 1 worker at the same quota
#     2. raise the quota to 2.0 at 4 workers
#     3. halve the period (cpu.max "50000 50000") -- same 1.0 CPU
#     4. grant burst (cpu.max.burst) -- watch nr_bursts stop being zero
#
#   Fixes 3 and 4 have no Compose key at all, so they go through
#   ./write_cgroup.sh, which writes the cgroup file directly from a
#   privileged sidecar.
#
# WHAT TO LOOK FOR IN THE OUTPUT
#   Every cell prints the ENFORCED cpu.max before it prints a measurement,
#   and refuses to measure if the kernel did not get what Compose meant.
#   Then it prints the cpu.stat delta across the run -- nr_throttled over
#   nr_periods -- next to k6's p95. The baseline row should show a modest
#   average CPU and a nonzero throttle ratio at the same time. That
#   divergence is the entire topic.
#
#   Watch k6's `dropped_iterations` too. If it is not near zero, k6 ran out
#   of VUs and every latency below it is understated -- you would be
#   measuring the load generator.
#
# RUN
#   ./run_7_2.sh                  # needs a running Docker daemon
#   RATE=60 ./run_7_2.sh          # push harder
set -euo pipefail

# Resolve this script's own directory BEFORE changing directory: after the
# cd below, a relative $0 no longer resolves.
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../../00-harness"

if ! docker info >/dev/null 2>&1; then
  cat >&2 <<'MSG'
docker daemon is not running.
Everything in 7.2 requires a real cgroup: there is no cpu.max to set and no
cpu.stat to read on Darwin. Start Docker Desktop and re-run.

For a userspace MODEL of the same accounting rule on this host, run the
per-language versions in ../python/ ../nodejs/ ../golang/ ../rust/ ../cpp/
../java/ -- each prints a FALLBACK banner saying it is not the kernel.
MSG
  exit 1
fi

# RATE is the offered request rate; BURST is how many of them arrive at the
# same instant. Both matter, and the second one is the reason this experiment
# works at all -- see the long note at the top of ../../00-harness/load/steady.js.
# 20 req/s on a ~15ms handler is ~0.3 of a CPU: the "30% average CPU" of the
# title. Delivering it as 2 clumps of 10 is what makes 150ms of CPU demand land
# inside a 100ms bucket.
RATE="${RATE:-20}"
BURST="${BURST:-10}"
DURATION="${DURATION:-45s}"
ENDPOINT="${ENDPOINT:-/mixed}"

read_stat()  { docker compose exec -T api cat /sys/fs/cgroup/cpu.stat; }
read_quota() { docker compose exec -T api cat /sys/fs/cgroup/cpu.max; }

# Refuse to measure a container whose cgroup does not hold what we asked
# for. A restart reuses the old cgroup, and measuring the previous config
# while believing you changed it is the single most common way this topic
# produces confident nonsense.
assert_cpu_max() {
  local want="$1" got
  got="$(read_quota | tr -d '\r')"
  if [ "$got" != "$want" ]; then
    echo "BROKEN: cpu.max is '$got', expected '$want'." >&2
    echo "Compose did not apply the limit, or you restarted instead of" >&2
    echo "recreating. Everything downstream of this reading is meaningless." >&2
    exit 2
  fi
  echo "  cpu.max enforced: $got"
}

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

cell() {
  local label="$1" want_cpu_max="$2"
  echo
  echo "=== $label ==="
  assert_cpu_max "$want_cpu_max"
  echo "  workers running:  $(worker_count)"

  local before after
  before="$(read_stat)"
  docker compose --profile load run --rm --no-deps \
    -e RATE="$RATE" -e DURATION="$DURATION" -e ENDPOINT="$ENDPOINT" \
    -e BURST="$BURST" \
    k6 run /scripts/steady.js 2>&1 | grep -E \
    '^(endpoint|offered|completed|p50|p99|max|dropped_iterations)' || true
  after="$(read_stat)"

  echo "  cpu.stat delta over the run:"
  join <(sort <<<"$before") <(sort <<<"$after") \
    | awk '{printf "    %-16s %s\n", $1, $3-$2}'
  # Average CPU next to the ratio, because the whole claim is that the two
  # point in opposite directions. usage_usec is CPU time; nr_periods*period
  # is the wall time it accrued over; their quotient is CPUs consumed, and
  # dividing by the quota gives the percentage a dashboard would show.
  # qmax MUST be captured before the pipeline, not inside it. read_quota runs
  # `docker compose exec -T`, which reads stdin -- and inside a pipeline its
  # stdin is the join output awk is about to consume. Written as
  # `... | awk -v qmax="$(read_quota)"` the exec silently eats the entire
  # cpu.stat delta and awk prints nothing at all, which is how the throttle
  # ratio -- the one number this experiment exists to produce -- went missing
  # from every cell without any error being raised.
  local qmax; qmax="$(read_quota)"
  join <(sort <<<"$before") <(sort <<<"$after") | awk -v qmax="$qmax" '
    $1=="nr_periods"   {p=$3-$2}
    $1=="nr_throttled" {t=$3-$2}
    $1=="usage_usec"   {u=$3-$2}
    $1=="throttled_usec" {tu=$3-$2}
    END {
      split(qmax, q, " ");
      period = q[2]; quota = (q[1]=="max" ? 0 : q[1]);
      if (p>0) {
        printf "    %-16s %.3f   <- THE NUMBER\n", "throttle ratio", t/p;
        printf "    %-16s %.2f CPU", "avg CPU used", u/(p*period);
        if (quota>0) printf "  = %.0f%% of the %.1f CPU quota", 100*u/(p*quota), quota/period;
        printf "\n";
        printf "    %-16s %.1f ms per throttled period\n", "frozen for", (t>0 ? tu/1000.0/t : 0);
      }
    }'
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

# Export rather than prefix. `WORKERS=4 recreate` sets the variable for the
# recreate and nothing else, so every later compose command in the cell --
# including anything that might re-resolve the api service -- would see the
# harness defaults instead of the configuration under test.
recreate() {
  export WORKERS="$1" API_CPUS="$2"
  docker compose up -d --force-recreate api >/dev/null
  warmup
}

echo "7.2 -- throttled at 30% CPU, against the harness"
echo "  endpoint $ENDPOINT at $RATE req/s for $DURATION, $BURST requests per arrival"
echo "  Sample throttling live in another terminal: ./observe/watch.sh api"

# Pin the workload before sweeping the variable. main.py calibrates /cpu to
# ~15ms at startup, per worker; four workers calibrating simultaneously inside
# a 1.0-CPU cgroup each measure a cost inflated by the other three and settle
# on a cheaper handler than one worker does. Left alone, the "4 workers" row
# and the "1 worker" row are not running the same test. Calibrate once with a
# single worker, then hold that number fixed for every cell.
pin_cpu_rounds() {
  export WORKERS=1 API_CPUS=1.0
  unset CPU_ROUNDS
  docker compose up -d --force-recreate api >/dev/null
  warmup
  local rounds
  rounds="$(curl -fsS -m 10 http://localhost:8000/stat 2>/dev/null \
            | sed -n 's/.*"cpu_rounds":[ ]*\([0-9]*\).*/\1/p')"
  if [ -z "$rounds" ]; then
    echo "could not read cpu_rounds from /stat -- is the api up on :8000?" >&2
    exit 1
  fi
  export CPU_ROUNDS="$rounds"
  echo "  /cpu pinned to $CPU_ROUNDS hash rounds for every cell below"
}
pin_cpu_rounds

# --- baseline: the failure -------------------------------------------------
recreate 4 1.0
cell "baseline: 4 workers, 1.0 CPU" "100000 100000"

# --- fix 1: fewer runnable threads, same bucket ----------------------------
recreate 1 1.0
cell "fix 1: 1 worker, 1.0 CPU" "100000 100000"

# --- fix 2: a bigger bucket ------------------------------------------------
recreate 4 2.0
cell "fix 2: 4 workers, 2.0 CPU" "200000 100000"

# --- fix 3: the same allowance, finer granularity --------------------------
# No Compose key exists for the period length, so this goes through the
# cgroup file directly -- and AFTER the recreate, which would wipe it.
recreate 4 1.0
"$HERE/write_cgroup.sh" api "50000 50000" >/dev/null
cell "fix 3: 4 workers, 1.0 CPU in 50ms periods" "50000 50000"

# --- fix 4: bank the unused quota ------------------------------------------
recreate 4 1.0
"$HERE/write_cgroup.sh" api --burst 100000 >/dev/null || \
  echo "  (cpu.max.burst unavailable -- needs Linux 5.14+; skipping fix 4)"
cell "fix 4: 4 workers, 1.0 CPU, + burst" "100000 100000"
echo "  nr_bursts above should be nonzero. It is zero in essentially every"
echo "  production cluster on earth, because neither Docker nor Kubernetes"
echo "  has a key for this file."

echo
echo "Reset the stack when you are done:"
echo "  WORKERS=1 API_CPUS=1.0 docker compose up -d --force-recreate api"
