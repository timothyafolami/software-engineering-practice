#!/usr/bin/env bash
# 7.7 -- both interpreter builds, same everything else, with the GIL
# assertion and the cpu.stat reading paired to every row.
#
# WHAT THIS DEMONSTRATES
#   Three configurations of the harness at a fixed cpus: "2.0":
#
#     A  python:3.14-slim,  WORKERS=4   the GIL build, as deployed today
#     B  python:3.14t-slim, WORKERS=4   free-threaded, same worker count
#     C  python:3.14t-slim, WORKERS=1   ONE process with a thread pool
#
#   Configuration C is the one the whole argument is actually about. B is
#   the row people benchmark and A/B is the comparison people publish; the
#   real case for free-threading is that C collapses process count, which is
#   the variable every ceiling in 7.4 gets multiplied by.
#
#   Both sides are pinned to 3.14 so the comparison differs in exactly ONE
#   variable -- the interpreter build, not the minor version. The harness's
#   PYTHON_IMAGE defaults to 3.13-slim, which is not free-threaded; comparing
#   3.13 against 3.14t would be measuring two changes at once.
#
#   Every row asserts sys._is_gil_enabled() before it measures anything and
#   refuses to report a number if the assertion is wrong for that build. A
#   free-threading benchmark that shows no difference is far more often a
#   silently re-enabled GIL than a real null result, and the assertion is the
#   only thing standing between you and publishing one.
#
# WHAT TO LOOK FOR IN THE OUTPUT
#   1. The GIL assertion line for each row. If 3.14t reports True, an
#      extension in requirements.txt re-enabled it -- and in this lab's
#      stack, psycopg2 is the one to suspect first.
#   2. /cpu improving from A to B, /db NOT improving. The wait was never a
#      GIL wait.
#   3. RSS and the PG connection count from A to C. That is where the
#      process-count argument is won.
#   4. The throttle ratio, which can go the wrong way. More runnable threads
#      in one cgroup drain the same bucket faster (7.2), so at a fixed cpus:
#      the free-threaded build can show a HIGHER ratio for the same work --
#      and, because freezes are quantised to the period, a worse tail while
#      doing identical throughput.
#
# RUN
#   ./run_7_7.sh
#   RATE=90 ./run_7_7.sh
set -euo pipefail

cd "$(dirname "$0")/../../00-harness"

if ! docker info >/dev/null 2>&1; then
  cat >&2 <<'MSG'
docker daemon is not running.

Everything in this script has a `cpus:` or a throttle ratio in it, so it
must run inside the Linux container. There is no host-side version.

The one part of 7.7 that IS useful on this Mac -- which interpreter you are
on, whether the GIL is enabled, whether an import turned it back on, and
whether your workload is one the GIL was ever in the way of:

  python3 ../07-free-threaded-python-honestly-in-2026/python/gil_check.py
MSG
  exit 1
fi

RATE="${RATE:-60}"
DB_RATE="${DB_RATE:-120}"
DURATION="${DURATION:-45s}"
API_CPUS="${API_CPUS:-2.0}"

# Read the GIL state from inside the container and check it against what the
# build should give. Refusing to measure is the whole point: a wrong
# assertion here invalidates every number after it.
assert_gil() {
  local image="$1" expect="$2" state
  state="$(docker compose exec -T api python -c \
    'import sys; print(sys._is_gil_enabled())' 2>/dev/null || echo "?")"
  printf '  sys._is_gil_enabled() = %-6s (expected %s for %s)\n' "$state" "$expect" "$image"
  if [ "$state" != "$expect" ]; then
    echo "  BROKEN: the GIL is not in the state this build implies." >&2
    if [ "$expect" = "False" ]; then
      echo "  A C extension re-enabled it at import. Find it with:" >&2
      echo "    docker compose exec api python -X importtime -c 'import main' 2>&1 | tail -40" >&2
      echo "  In this lab's stack, psycopg2 is the first thing to suspect." >&2
      echo "  Every number below would be a GIL build wearing a 't'." >&2
    fi
    return 1
  fi
}

row() {
  local label="$1" image="$2" workers="$3" expect_gil="$4"

  echo
  echo "=================================================================="
  echo "  $label"
  echo "    PYTHON_IMAGE=$image  WORKERS=$workers  API_CPUS=$API_CPUS"
  echo "=================================================================="

  # Separate "the image does not exist" from "the build failed". They are
  # completely different findings and the second one is the interesting one;
  # reporting a missing tag as "an extension has no free-threaded wheel" is a
  # diagnosis of something that never happened.
  if ! docker manifest inspect "$image" >/dev/null 2>&1 \
     && ! docker image inspect "$image" >/dev/null 2>&1; then
    echo
    echo "  the image $image does not exist in the registry."
    echo "  This is not a wheel problem and not a code problem: Docker Hub's"
    echo "  official \`python\` repository publishes no free-threaded tag at"
    echo "  all -- there is no 3.14t, no *t-slim, no 'freethreaded' variant --"
    echo "  and python:3.14-slim ships a GIL-enabled interpreter"
    echo "  (sysconfig Py_GIL_DISABLED = 0, and no python3.14t binary)."
    echo
    echo "  To run this row you need a free-threaded image of your own:"
    echo "    FROM python:3.14-slim AS build   # then configure --disable-gil"
    echo "  or an image from a publisher that ships one. Point PYTHON_IMAGE at"
    echo "  it; nothing else in this script changes."
    return 0
  fi

  if ! PYTHON_IMAGE="$image" WORKERS="$workers" API_CPUS="$API_CPUS" \
      docker compose up -d --build --force-recreate api; then
    echo
    echo "  the container would not start on $image."
    echo "  That is a RESULT, not a failure: an extension in requirements.txt"
    echo "  has no free-threaded wheel for this interpreter. Record which one,"
    echo "  because it is the actual blocker on migrating, and it is a more"
    echo "  useful finding than any latency number below would have been."
    return 0
  fi
  sleep 6

  assert_gil "$image" "$expect_gil" || return 0

  echo "  enforced cpu.max: $(docker compose exec -T api cat /sys/fs/cgroup/cpu.max)"
  # python:*-slim has no `ps` (no procps), so `ps ax | grep -c uvicorn` reports
  # 0 for every configuration. Count uvicorn's forked workers through /proc.
  echo "  uvicorn workers: $(docker compose exec -T api sh -c '
    n=0
    for d in /proc/[0-9]*; do
      tr "\0" "\n" < "$d/cmdline" 2>/dev/null | grep -q -- "--multiprocessing-fork" && n=$((n+1))
    done
    if [ "$n" -eq 0 ]; then echo "1 (serving in-process, no forks)"; else echo "$n forked"; fi' 2>/dev/null || echo '?')"

  local before after
  before="$(docker compose exec -T api cat /sys/fs/cgroup/cpu.stat)"

  echo
  echo "  --- /cpu at $RATE req/s (CPU-bound: the endpoint the GIL was in the way of) ---"
  docker compose --profile load run --rm --no-deps -e ENDPOINT=/cpu -e RATE="$RATE" \
    -e DURATION="$DURATION" k6 run /scripts/steady.js 2>&1 \
    | grep -E '^(endpoint|offered|completed|p50|p99|max|dropped_iterations)' | sed 's/^/    /' || true

  # Read the backend count DURING the run's tail, not after: pools are lazy
  # and a post-run reading shows connections already returned.
  echo "  PG backends now: $(docker compose exec -T db psql -U lab -d container_lab -tAc \
    "select count(*) from pg_stat_activity where datname='container_lab';" | tr -d '[:space:]')"

  echo
  echo "  --- /db at $DB_RATE req/s (IO-bound: the wait was never a GIL wait) ---"
  docker compose --profile load run --rm --no-deps -e ENDPOINT=/db -e RATE="$DB_RATE" \
    -e DURATION="$DURATION" k6 run /scripts/steady.js 2>&1 \
    | grep -E '^(endpoint|offered|completed|p50|p99|max|dropped_iterations)' | sed 's/^/    /' || true

  after="$(docker compose exec -T api cat /sys/fs/cgroup/cpu.stat)"

  echo
  echo "  cpu.stat delta across both runs:"
  join <(sort <<<"$before") <(sort <<<"$after") | awk '{printf "    %-16s %s\n", $1, $3-$2}'
  join <(sort <<<"$before") <(sort <<<"$after") | awk '
    $1=="nr_periods"   {p=$3-$2}
    $1=="nr_throttled" {t=$3-$2}
    END {if (p>0) printf "    %-16s %.3f   <- can go the WRONG way on 3.14t\n", "throttle ratio", t/p}'

  echo "  RSS: $(docker stats --no-stream --format '{{.MemUsage}}' "$(docker compose ps -q api)")"
}

echo "7.7 -- free-threaded Python, honestly, against the harness"
echo "  Both sides pinned to 3.14. The ONE variable is the interpreter build."
echo "  Comparing 3.13 against 3.14t would measure two changes at once."

row "A -- 3.14, 4 workers (the GIL build, as deployed today)" \
    "python:3.14-slim" 4 "True"

row "B -- 3.14t, 4 workers (free-threaded, same worker count)" \
    "python:3.14t-slim" 4 "False"

row "C -- 3.14t, 1 worker (ONE process -- the configuration the argument is about)" \
    "python:3.14t-slim" 1 "False"

cat <<'MSG'

==================================================================
  Reading the three rows
==================================================================

  A -> B is the comparison people publish, and it is the least interesting
  one. It answers "is free-threaded Python faster at the same shape", and
  for a service whose time goes to Postgres and CFS throttling the answer
  is "no, and it was never going to be".

  A -> C is the argument. One process instead of four means one connection
  pool instead of four, one copy of every in-process cache instead of four,
  and one interpreter's memory overhead instead of four. Walk 7.4's four
  ceilings against those two rows:

    quota        unchanged. The bucket is the same size.
    connections  IMPROVED, by a factor of the worker count. This is the win.
    threads      changed shape: the anyio limiter is per-process, so one
                 process means 40 tokens instead of 160 -- raise it
                 deliberately rather than discovering it under load.
    memory       two effects in opposite directions: fewer interpreters
                 (better) against higher per-object overhead (worse). Which
                 wins is measurable and is in the RSS lines above.

  And the row that can go the wrong way: the throttle ratio. More runnable
  threads in one cgroup drain a fixed bucket faster, so at the same cpus:
  the free-threaded build can be throttled MORE for identical throughput --
  and the freezes are still quantised to the period, so the tail gets worse
  in exactly the shape 7.2 taught you to recognise.

  Reset the harness when you are done:
    docker compose up -d --build --force-recreate api
MSG
