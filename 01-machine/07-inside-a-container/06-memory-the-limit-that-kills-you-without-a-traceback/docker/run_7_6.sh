#!/usr/bin/env bash
# 7.6 -- collect all five pieces of evidence, including the empty one.
#
# WHAT THIS DEMONSTRATES
#   Two things, and the second is the one worth the script:
#
#   1. THE SIX RUNTIMES, each under the same --memory=256m, each reporting
#      what its language did about running out: which of them printed
#      something before dying, which exit code you got, and what the JVM's
#      OutOfMemoryError looks like next to Python's silence.
#
#   2. THE SOFT LIMIT. `--high 200m` writes memory.high BELOW memory.max,
#      which Compose has no key for at all, and re-runs the service version.
#      The container should SURVIVE, the `high` counter should climb,
#      memory.pressure should rise, and throughput should degrade gradually
#      instead of the process disappearing. That is a signal you can alert
#      on before an incident rather than a corpse to autopsy after one.
#
#   For every run it collects the whole evidence table -- .State.OOMKilled,
#   the exit code, memory.events, and the container's own last words -- so
#   the empty cell (your application logs) is visibly empty rather than
#   merely described as empty.
#
# WHAT TO LOOK FOR IN THE OUTPUT
#   The "printed anything?" column. Node and Java say something before they
#   go; Python, Rust, C++ and Go do not. That column is the difference
#   between a debuggable failure and "the pod restarts sometimes and we
#   can't find the error".
#
# RUN
#   ./run_7_6.sh                  # the six runtimes at 256m
#   ./run_7_6.sh --high 200m      # the soft-limit version, on the harness
#   ./run_7_6.sh --only python,java
set -euo pipefail

cd "$(dirname "$0")/.."
TOPIC_DIR="$(basename "$PWD")"
HARNESS="$PWD/../00-harness"
# Mount the whole of 07-inside-a-container. python/oom.py imports the
# harness's cgroup.py from ../../00-harness/local/, which is outside this
# directory: mounting only this directory makes the Python row die on
# ModuleNotFoundError before it allocates a byte.
cd ..
ROOT="$PWD"
WORK="/w/$TOPIC_DIR"

MEM="${MEM:-256m}"
# The soft-limit demo needs memory.max ABOVE what the program will allocate,
# or the hard limit kills it before the soft limit has anything to show.
# python/oom.py climbs to 384 MiB, so 512m is the smallest honest ceiling.
HIGH_MAX="${HIGH_MAX:-512m}"
# The soft-limit run does not finish on its own: throttled hard enough, the
# allocation loop makes almost no progress. Sample it for a fixed window and
# say so, rather than waiting for an end that is not coming.
HIGH_SECONDS="${HIGH_SECONDS:-45}"
HIGH=""
ONLY="all"
while [ $# -gt 0 ]; do
  case "$1" in
    --high) HIGH="$2"; shift 2 ;;
    --only) ONLY="$2"; shift 2 ;;
    --mem)  MEM="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if ! docker info >/dev/null 2>&1; then
  cat >&2 <<'MSG'
docker daemon is not running.

This one matters more than usual: macOS has no cgroup memory controller, no
memory.events and no cgroup OOM killer. Running the per-language programs on
the Mac host would page, swap and eventually annoy you -- a different
experiment with a different lesson -- so each of them imposes its own ceiling
there and says clearly that it stopped ITSELF rather than being killed.

Start Docker Desktop and re-run. To see the honest host-side half:
  python3 python/oom.py --free
  node nodejs/oom.js --heap
  (cd golang && GOMEMLIMIT=64MiB go run oom.go -pointers)
  (cd rust/oom && cargo run --release)
  g++ -O2 -std=c++17 -o /tmp/oom cpp/oom.cpp && /tmp/oom --reserve-only
  (cd java && javac Oom.java -d /tmp/b && java -Xmx64m -cp /tmp/b Oom --heap)
MSG
  exit 1
fi

wants() { [ "$ONLY" = "all" ] || [[ ",$ONLY," == *",$1,"* ]]; }

# numfmt is GNU coreutils and is NOT on macOS. The original code fell back to
# passing the literal string ("200m") to memory.high, the write failed inside
# the sidecar, and the run carried on measuring the ORIGINAL soft limit.
to_bytes() {
  awk -v v="$1" 'BEGIN{
      n = v + 0;
      u = tolower(substr(v, length(n "") + 1, 1));
      mult = (u=="k") ? 1024 : (u=="m") ? 1048576 : (u=="g") ? 1073741824 : 1;
      printf "%d", n * mult;
  }'
}

# One row of the evidence table. Runs the program in its own container under
# --memory=$MEM, then collects every reading from the README's table --
# including the one that is always empty.
evidence_row() {
  local name="$1" image="$2" command="$3"
  shift 3
  local extra=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --extra-mount) extra+=(-v "$2"); shift 2 ;;
      *) shift ;;
    esac
  done

  wants "$name" || return 0

  echo
  echo "=================================================================="
  echo "  $name   under --memory=$MEM"
  echo "=================================================================="

  local cid
  cid="$(docker create --memory="$MEM" --memory-swap="$MEM" \
    -v "$ROOT:/w:ro" "${extra[@]+"${extra[@]}"}" -w /w "$image" sh -c "$command")"
  # --memory-swap equal to --memory disables swap for the container. Without
  # it the kernel can swap instead of killing, and the experiment quietly
  # becomes a much slower, much less interesting one.

  docker start -a "$cid" 2>&1 | sed 's/^/    /' || true

  local exit_code oom_killed
  exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$cid")"
  oom_killed="$(docker inspect --format '{{.State.OOMKilled}}' "$cid")"

  echo
  echo "  --- the evidence ---"
  printf '    %-34s %s\n' "docker inspect .State.OOMKilled" "$oom_killed"
  printf '    %-34s %s' "exit code" "$exit_code"
  case "$exit_code" in
    137) echo "   <- 128 + 9 (SIGKILL). Killed." ;;
    134) echo "   <- 128 + 6 (SIGABRT). The runtime aborted itself." ;;
    0)   echo "   <- finished normally. Nothing was enforced." ;;
    *)   echo "" ;;
  esac
  printf '    %-34s %s\n' "the last line above" "<- this is your entire application log"

  docker rm "$cid" >/dev/null
}

echo "7.6 -- memory: the limit that kills you without a traceback"
echo "  limit for every row below: --memory=$MEM (swap disabled)"
echo
echo "  Derive the exit code rather than memorising it: a shell reports a"
echo "  signal-terminated process as 128 + signal, and SIGKILL is signal 9."
echo "  So 137 in a restart log is a killed process, and in a container it is"
echo "  nearly always this."

evidence_row python "python:3.13-slim" \
  "python $WORK/python/oom.py"

# Three node rows, not two, because the obvious two do not say what they look
# like they say. V8 DOES derive its old-space limit from the cgroup -- and on
# this machine it derived 259 MiB against a memory.max of 256 MiB, i.e. 101% of
# the container. So plain `--heap` is killed by the KERNEL at 137 before V8's
# own ceiling is ever reached, and the documented "exit 134 with a stack trace"
# never happens. It happens once you set the ceiling below the container's,
# which is the thing you were supposed to be doing anyway.
evidence_row node "node:24-slim" \
  "echo '--- --heap, V8 sizing itself from the cgroup (its limit lands ABOVE memory.max) ---';
   node $WORK/nodejs/oom.js --heap || true;
   echo; echo '--- --heap with --max-old-space-size=160, i.e. BELOW the container (expect 134) ---';
   node --max-old-space-size=160 $WORK/nodejs/oom.js --heap || echo \"    (node exited \$?)\";
   echo; echo '--- --buffer (outside the heap entirely: expect 137 and silence) ---';
   node $WORK/nodejs/oom.js --buffer"

# Go is compiled OUTSIDE the limit and only then run inside it. `go run` under
# --memory=256m gets the COMPILER OOM-killed
# ("compile: signal: killed", exit 1) and the program never starts: the row
# measured the toolchain's memory, not the program's. Worse, `go run` survives
# its child, so the container exits 1 rather than 137 and the OOMKilled
# evidence points at the wrong process. Build first, then run the binary.
GO_BIN="$(mktemp -d)"
docker run --rm -v "$ROOT:/w:ro" -v "$GO_BIN:/out" -w /tmp golang:1.25 \
  sh -c "cp $WORK/golang/oom.go /tmp/ && go build -o /out/oom oom.go" >/dev/null
trap 'rm -rf "$GO_BIN"' EXIT

evidence_row go "golang:1.25" \
  "cp /gobin/oom /tmp/oom && exec /tmp/oom" --extra-mount "$GO_BIN:/gobin:ro"

evidence_row go-memlimit "golang:1.25" \
  "cp /gobin/oom /tmp/oom && GOMEMLIMIT=230MiB exec /tmp/oom" --extra-mount "$GO_BIN:/gobin:ro"

evidence_row rust "rust:1" \
  "mkdir -p /tmp/oom && cp -r $WORK/rust/oom/Cargo.toml $WORK/rust/oom/src /tmp/oom/ && cd /tmp/oom && cargo run --release --quiet"

evidence_row cpp "gcc:14" \
  "g++ -O2 -std=c++17 -o /tmp/oom $WORK/cpp/oom.cpp &&
   echo '--- --reserve-only (allocate, do not touch: expect survival) ---' &&
   /tmp/oom --reserve-only &&
   echo && echo '--- touch every page (expect 137) ---' && /tmp/oom"

evidence_row java "eclipse-temurin:21" \
  "javac $WORK/java/Oom.java -d /tmp/b &&
   echo '--- --heap (expect a CAUGHT OutOfMemoryError and exit 1) ---' &&
   (java -XX:MaxRAMPercentage=75 -cp /tmp/b Oom --heap || true) &&
   echo && echo '--- --direct, MaxDirectMemorySize raised past the container (expect 137) ---' &&
   java -XX:MaxRAMPercentage=50 -XX:MaxDirectMemorySize=2g -cp /tmp/b Oom --direct"

# ------------------------------------------------------------ the soft limit

if [ -n "$HIGH" ]; then
  echo
  echo "=================================================================="
  echo "  memory.high = $HIGH, under a memory.max of $HIGH_MAX"
  echo "=================================================================="
  echo
  echo "  Compose exposes only the hard limit, so memory.high has to be"
  echo "  written into the cgroup directly. This is the version you can"
  echo "  debug: the kernel puts allocating tasks under heavy reclaim"
  echo "  pressure instead of killing them, so the process survives, gets"
  echo "  slower, and TELLS YOU -- via memory.events' high counter and"
  echo "  memory.pressure -- while it is still alive to be looked at."
  echo
  echo "  This runs python/oom.py, not the harness api. The api was the"
  echo "  original subject here and it demonstrated nothing: one uvicorn"
  echo "  worker serving /mixed sits at ~45 MiB whatever you throw at it,"
  echo "  so memory.high was never approached, the high counter stayed 0"
  echo "  and memory.pressure stayed 0.00 -- a container that survives"
  echo "  because it never allocated is not evidence that memory.high"
  echo "  works. The program that allocates is the one to point at it."
  echo

  HIGH_BYTES="$(to_bytes "$HIGH")"
  echo "  memory.high $HIGH -> $HIGH_BYTES bytes (memory.max stays at $HIGH_MAX)"

  # Start gated: the container waits for a file before it allocates a byte, so
  # memory.high is in place BEFORE the first page is charged. Write it after
  # the container exists but before it starts working, or the process races
  # past the soft limit while you are still looking up its cgroup path.
  CID="$(docker run -d --memory="$HIGH_MAX" --memory-swap="$HIGH_MAX" \
    -v "$ROOT:/w:ro" -w /w python:3.13-slim \
    sh -c "while [ ! -f /tmp/go ]; do sleep 0.2; done; exec python $WORK/python/oom.py")"

  docker run --rm --privileged --cgroupns=host \
    -v /sys/fs/cgroup:/sys/fs/cgroup:rw alpine:3 sh -c "
      for d in /sys/fs/cgroup/docker/$CID \
               /sys/fs/cgroup/system.slice/docker-$CID.scope; do
        if [ -f \"\$d/memory.high\" ]; then
          echo '$HIGH_BYTES' > \"\$d/memory.high\"
          echo \"  wrote memory.high = \$(cat \$d/memory.high) into \$d\"
          exit 0
        fi
      done
      echo '  could not locate the cgroup directory' >&2; exit 1"

  # Everything below reads the cgroup from a HOST-side sidecar, never with
  # `docker exec`. Once memory.high is exceeded the cgroup's PSI sits near 90%
  # and the kernel cannot schedule a NEW process into it either: `docker exec`
  # into a throttled container hangs indefinitely. Observing from outside is
  # not a nicety here, it is the only thing that returns.
  read_cg() {
    docker run --rm --privileged --cgroupns=host \
      -v /sys/fs/cgroup:/sys/fs/cgroup:ro alpine:3 sh -c "
        d=\$(ls -d /sys/fs/cgroup/docker/$CID* 2>/dev/null | head -1)
        [ -n \"\$d\" ] || { echo '    (cgroup gone -- the container exited)'; exit 0; }
        echo \"    memory.current  \$(cat \$d/memory.current)\"
        echo \"    memory.high     \$(cat \$d/memory.high)\"
        echo \"    memory.max      \$(cat \$d/memory.max)\"
        echo '    memory.events:';   sed 's/^/      /' \$d/memory.events
        echo '    memory.pressure:'; sed 's/^/      /' \$d/memory.pressure"
  }

  echo
  echo "  before it allocates anything:"
  read_cg

  docker exec "$CID" touch /tmp/go
  echo
  echo "  allocating for ${HIGH_SECONDS}s under the soft limit..."
  sleep "$HIGH_SECONDS"

  echo
  echo "  after ${HIGH_SECONDS}s:"
  read_cg

  RUNNING="$(docker inspect --format '{{.State.Running}}' "$CID" 2>/dev/null || echo false)"
  OOMK="$(docker inspect --format '{{.State.OOMKilled}}' "$CID" 2>/dev/null || echo '?')"
  echo
  echo "  still running? $RUNNING   OOMKilled=$OOMK"
  echo
  echo "  Read those three together. memory.current sits ABOVE memory.high and"
  echo "  BELOW memory.max; memory.events' high counter is climbing steadily"
  echo "  (thousands of events in this window); oom_kill is 0 and the process"
  echo "  is still alive. That"
  echo "  is the whole argument for memory.high: a limit you can observe from"
  echo "  a live process instead of infer from a restart log."
  echo
  echo "  And the cost, which is not optional and is rarely mentioned: with"
  echo "  swap disabled and an all-anonymous heap there is nothing reclaimable,"
  echo "  so the kernel throttles the allocator instead. cpu-equivalent stall"
  echo "  is the 'full' line above. At that level the cgroup cannot start a new"
  echo "  process either -- this script reads the numbers from outside because"
  echo "  'docker exec' into the container does not return."
  docker rm -f "$CID" >/dev/null 2>&1 || true
  echo
  echo '  The high counter climbing while the process stays up is the'
  echo "  whole argument for memory.high. Compare it against the rows above,"
  echo "  where the only evidence was a number in a restart log."
fi

cat <<'MSG'

==================================================================
  Reading the table
==================================================================

  printed a diagnosable error:     Node (--heap with --max-old-space-size
                                   BELOW memory.max), Java (--heap)
  printed nothing at all:          Python, Rust, C++, Go, Node --buffer,
                                   Java --direct -- and Node --heap when V8
                                   is left to size itself, because the limit
                                   it derives can land ABOVE memory.max

  The split is not about language quality. It is about WHO enforced the
  limit. A limit the runtime enforces produces a diagnosable error with a
  stack trace. A limit the KERNEL enforces produces SIGKILL, which cannot
  be caught, blocked or handled -- so there is nothing to print, by
  construction, in any language.

  That is why "the pod restarts sometimes and we can't find the error" is
  such a durable mystery: there is nothing to find in the place everyone
  looks. The evidence exists, in the kernel's records rather than yours:

    docker inspect <c> --format '{{.State.OOMKilled}}'
    the exit code (137)
    /sys/fs/cgroup/memory.events   (oom_kill)
    dmesg on the host
MSG
