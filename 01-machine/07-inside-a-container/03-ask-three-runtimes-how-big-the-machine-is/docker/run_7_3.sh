#!/usr/bin/env bash
# 7.3 -- the whole matrix: six runtimes, one container spec, twice.
#
# WHAT THIS DEMONSTRATES
#   Every probe in this folder, run inside the SAME container spec, so the
#   comparison means something. Two columns, and the point is that they are
#   different columns:
#
#     column 1   --cpus=1.5        writes cpu.max. Calls that read the
#                                  BANDWIDTH limit move; calls that read the
#                                  affinity mask do not.
#     column 2   --cpuset-cpus=0,1 writes cpuset.cpus. Exactly the reverse.
#
#   Two sets of calls, two knobs, and they are not the same set. cpuset is
#   the knob runtimes accidentally get right; quota is the knob they get
#   wrong, silently.
#
#   Each language runs in its own official image, so no single image has to
#   carry six toolchains -- and so the version each runtime reports is a
#   version you could actually deploy.
#
# WHAT TO LOOK FOR IN THE OUTPUT
#   The cells the README asks you to predict first:
#     * Go's rounding at 1.5 -- and note this repo's toolchain is go1.24,
#       where GOMAXPROCS ignores cpu.max entirely. The probe detects and
#       says which behaviour you got; the golang: image tag below decides.
#     * Python's os.process_cpu_count(), the modern cross-platform call that
#       looks obviously correct and still does not read cpu.max.
#     * Java's two rows, with and without -XX:-UseContainerSupport.
#
# RUN
#   ./run_7_3.sh                     # both columns, all six
#   ./run_7_3.sh --only python,go    # a subset
#   CPUS=2.5 ./run_7_3.sh            # a different quota
set -euo pipefail

cd "$(dirname "$0")/.."
TOPIC_DIR="$(basename "$PWD")"
# Mount the whole of 07-inside-a-container, not just this sub-topic. The
# Python probe imports the harness's cgroup.py from ../../00-harness/local/,
# which is outside this directory: mounting only this directory made
# `python /w/python/cpuinfo.py` die on ModuleNotFoundError: No module named
# 'cgroup' before it printed a single row. Mounting the parent keeps every
# probe's relative path resolving exactly as it does on the host.
cd ..
ROOT="$PWD"
WORK="/w/$TOPIC_DIR"

if ! docker info >/dev/null 2>&1; then
  cat >&2 <<'MSG'
docker daemon is not running.

There is no host-side version of this experiment. On macOS every cgroup
reading is absent -- correctly, since Darwin has no cgroupfs -- so the matrix
collapses to a single column and there is nothing to compare.

You can still run each probe on the host to see the single-column answer and
the "n/a" rows that say why:
  python3 python/cpuinfo.py
  node nodejs/cpuinfo.js
  (cd golang && go run cpuinfo.go)
  (cd rust/cpuinfo && cargo run --release)
  g++ -O2 -std=c++17 -pthread -o /tmp/cpuinfo cpp/cpuinfo.cpp && /tmp/cpuinfo
  (cd java && javac CpuInfo.java -d /tmp/javabuild && java -cp /tmp/javabuild CpuInfo)
MSG
  exit 1
fi

CPUS="${CPUS:-1.5}"
CPUSET="${CPUSET:-0,1}"
ONLY="all"
[ "${1:-}" = "--only" ] && ONLY="$2"

wants() { [ "$ONLY" = "all" ] || [[ ",$ONLY," == *",$1,"* ]]; }

# The VM's own size decides whether this experiment has anything to show. A
# 2-CPU Docker Desktop VM under a 1.5-CPU quota is not a gap worth measuring.
HOST_CPUS="$(docker info --format '{{.NCPU}}')"
echo "7.3 -- the CPU-count matrix"
echo "  Docker's Linux VM has $HOST_CPUS CPUs."
if [ "$HOST_CPUS" -lt 4 ]; then
  echo "  WARNING: fewer than 4. Raise it in Docker Desktop's settings, or the"
  echo "  host-vs-quota gap this entire topic is about will be too small to see."
fi
echo "  cgroup driver: $(docker info --format '{{.CgroupDriver}}') / version $(docker info --format '{{.CgroupVersion}}')"
echo

# One image per language. Pinned to the majors this topic talks about, so the
# version each probe reports is one you could deploy rather than "latest".
run_probe() {
  local name="$1" image="$2" command="$3"
  shift 3
  local limit=("$@")

  wants "$name" || return 0
  echo
  echo "--- $name --------------------------------------------------------"
  docker run --rm "${limit[@]}" \
    -v "$ROOT:/w:ro" -w /w \
    "$image" sh -c "$command" 2>&1 | sed 's/^/  /'
}

column() {
  local title="$1"
  shift
  local limit=("$@")

  echo
  echo "##################################################################"
  echo "# $title"
  echo "#   docker run ${limit[*]}"
  echo "##################################################################"

  # Print what the kernel got, before any runtime is asked anything. Do this
  # first, always -- a matrix built on an unapplied limit is six wrong rows.
  echo
  echo "  what the kernel actually got:"
  docker run --rm "${limit[@]}" alpine:3 sh -c \
    'echo "    cpu.max               $(cat /sys/fs/cgroup/cpu.max 2>/dev/null || echo "?")";
     echo "    cpuset.cpus.effective $(cat /sys/fs/cgroup/cpuset.cpus.effective 2>/dev/null || echo "?")";
     echo "    cpu.weight            $(cat /sys/fs/cgroup/cpu.weight 2>/dev/null || echo "?")"'

  run_probe python "python:3.13-slim" \
    "python $WORK/python/cpuinfo.py" "${limit[@]}"

  run_probe node "node:24-slim" \
    "node $WORK/nodejs/cpuinfo.js" "${limit[@]}"

  # Two rows from one image: the current default, then the pre-1.25 behaviour.
  run_probe go "golang:1.25" \
    "cd /tmp && cp $WORK/golang/cpuinfo.go . && go run cpuinfo.go
     echo; echo '  ### same binary, GODEBUG=containermaxprocs=0 (the pre-1.25 answer) ###'
     GODEBUG=containermaxprocs=0 go run cpuinfo.go | head -12" "${limit[@]}"

  run_probe rust "rust:1-slim" \
    "mkdir -p /tmp/c && cp -r $WORK/rust/cpuinfo/Cargo.toml $WORK/rust/cpuinfo/src /tmp/c/ && cd /tmp/c && cargo run --release --quiet" "${limit[@]}"

  run_probe cpp "gcc:14" \
    "g++ -O2 -std=c++17 -pthread -o /tmp/cpuinfo $WORK/cpp/cpuinfo.cpp && /tmp/cpuinfo" "${limit[@]}"

  # Two rows again: with container support (the default) and without, which is
  # Java's equivalent of Go's GODEBUG switch.
  run_probe java "eclipse-temurin:21" \
    "javac $WORK/java/CpuInfo.java -d /tmp/b && java -cp /tmp/b CpuInfo
     echo; echo '### same VM, -XX:-UseContainerSupport (the pre-8u191 answer) ###'
     java -XX:-UseContainerSupport -cp /tmp/b CpuInfo | head -12" "${limit[@]}"
}

column "COLUMN 1 -- quota: --cpus=$CPUS   (writes cpu.max)" --cpus="$CPUS"
column "COLUMN 2 -- pin:   --cpuset-cpus=$CPUSET   (writes cpuset.cpus)" --cpuset-cpus="$CPUSET"

cat <<'MSG'

##################################################################
# Reading the two columns
##################################################################

  Calls that track the AFFINITY MASK moved between the columns:
    python  len(os.sched_getaffinity(0)), os.process_cpu_count()
    go      runtime.NumCPU()
    c++     sched_getaffinity(2)
    rust    available_parallelism()   (it reads both)
    java    availableProcessors()     (it reads both)

  Calls that track the BANDWIDTH LIMIT moved in column 1 only:
    go      GOMAXPROCS(0), on a 1.25+ toolchain
    node    os.availableParallelism(), on libuv >= 1.49
    rust    available_parallelism()
    java    availableProcessors()

  Calls that moved in NEITHER column -- the ones people actually type:
    python  os.cpu_count()
    node    os.cpus().length
    c++     std::thread::hardware_concurrency()

  That last group is the whole topic. Those three are not "sometimes
  wrong": they have never once reported the number the kernel enforces,
  and they never say so.
MSG
