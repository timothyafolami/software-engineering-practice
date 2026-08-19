#!/usr/bin/env bash
# The two knobs Compose cannot express: the CFS period length, and burst.
#
# WHAT THIS DEMONSTRATES
#   Docker's `--cpus=N` and Compose's `cpus:` can only ever write
#   "N*100000 100000" -- N CPUs' worth of quota, in 100ms periods. The
#   period length is not exposed, and neither is cpu.max.burst. Kubernetes
#   cannot express burst either, which is most of why almost nobody has
#   ever seen nr_bursts be nonzero in production.
#
#   Both are just files. This script writes them, from a privileged
#   sidecar that mounts the host's cgroupfs, because a container's own
#   cgroup directory is read-only from inside it.
#
#   Fix 3 from the README -- "50000 50000" -- is the same 1.0 CPU on
#   average in half-length periods, so the same throughput arrives with a
#   freeze quantum half as long. Check the arithmetic every single time
#   you touch this file: the format is "QUOTA PERIOD", so "100000 50000"
#   would be TWO CPUs, not one. Writing the pair backwards is the most
#   common way to accidentally double a limit while believing you halved
#   a period.
#
# WHAT TO LOOK FOR IN THE OUTPUT
#   It reads the file back after writing and prints both values. Never
#   trust the write; the kernel rejects quotas below 1000us and periods
#   outside [1000, 1000000] with EINVAL, and a rejected write leaves the
#   old value in place while your experiment carries on believing.
#
# RUN
#   ./write_cgroup.sh api "50000 50000"          # fix 3: half-length periods
#   ./write_cgroup.sh api --burst 100000         # fix 4: bank unused quota
#   ./write_cgroup.sh api                        # just read the current values
#
# macOS: this drives Docker Desktop's linuxkit VM, where the cgroup files
# are real. There is nothing to write on the Darwin side -- no cgroupfs,
# no quota, no burst.
set -euo pipefail

SERVICE="${1:-api}"
shift || true

CPU_MAX=""
BURST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --burst) BURST="$2"; shift 2 ;;
    *)       CPU_MAX="$1"; shift ;;
  esac
done

if ! docker info >/dev/null 2>&1; then
  cat >&2 <<'MSG'
docker daemon is not running.
There is no host-side substitute: Darwin has no cgroupfs, so cpu.max and
cpu.max.burst do not exist to be written. Start Docker Desktop and re-run.
MSG
  exit 1
fi

# Resolve the service name to a container id. Accept a raw container name
# too, so this works outside the harness's compose project.
CID="$(docker compose ps -q "$SERVICE" 2>/dev/null || true)"
if [ -z "$CID" ]; then
  CID="$(docker inspect -f '{{.Id}}' "$SERVICE" 2>/dev/null || true)"
fi
if [ -z "$CID" ]; then
  echo "cannot find a container for '$SERVICE'." >&2
  echo "Run this from 00-harness/ with the stack up, or pass a container name." >&2
  exit 1
fi

# The sidecar: privileged, in the host's cgroup namespace, with the host's
# cgroupfs mounted read-write. Everything below runs in there, because the
# api container cannot write its own cgroup directory.
sidecar() {
  docker run --rm --privileged --cgroupns=host \
    -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
    alpine:3 sh -c "$1"
}

# Docker Desktop puts container cgroups under /sys/fs/cgroup/docker/<id> or
# system.slice/docker-<id>.scope depending on the cgroup driver. Search
# rather than assume; assuming is how you write a file that exists and
# affects nothing.
FIND_DIR='
for d in /sys/fs/cgroup/docker/'"$CID"' \
         /sys/fs/cgroup/system.slice/docker-'"$CID"'.scope \
         $(find /sys/fs/cgroup -maxdepth 4 -type d -name "*'"${CID:0:12}"'*" 2>/dev/null); do
  [ -f "$d/cpu.max" ] && { echo "$d"; exit 0; }
done
exit 1'

DIR="$(sidecar "$FIND_DIR" 2>/dev/null || true)"
if [ -z "$DIR" ]; then
  echo "could not locate the cgroup directory for $SERVICE ($CID)." >&2
  echo "Check 'docker info | grep -i cgroup' -- on cgroup v1 hosts the files" >&2
  echo "are cpu.cfs_quota_us / cpu.cfs_period_us under a different hierarchy." >&2
  exit 1
fi

echo "container : $SERVICE ($(echo "$CID" | cut -c1-12))"
echo "cgroup    : $DIR"
echo
echo "--- before ---"
sidecar "cat $DIR/cpu.max; cat $DIR/cpu.max.burst 2>/dev/null || echo '(no cpu.max.burst -- kernel < 5.14)'"

if [ -n "$CPU_MAX" ]; then
  echo
  echo "writing cpu.max = '$CPU_MAX'   (format is QUOTA PERIOD, both in us)"
  QUOTA="${CPU_MAX%% *}"; PERIOD="${CPU_MAX##* }"
  if [ "$QUOTA" != "max" ]; then
    awk -v q="$QUOTA" -v p="$PERIOD" 'BEGIN{printf "  -> %.2f CPU in %.0fms periods\n", q/p, p/1000}'
  fi
  sidecar "echo '$CPU_MAX' > $DIR/cpu.max"
fi

if [ -n "$BURST" ]; then
  echo
  echo "writing cpu.max.burst = $BURST us"
  echo "  burst banks unused quota from earlier periods to absorb a spike."
  echo "  It is capped at QUOTA, is 0 by default, and neither Docker nor"
  echo "  Kubernetes has a key for it. Watch nr_bursts / burst_usec move."
  sidecar "echo '$BURST' > $DIR/cpu.max.burst" || {
    echo "write failed -- cpu.max.burst needs Linux 5.14+ and burst <= quota." >&2
  }
fi

echo
echo "--- after (read back: never trust the write) ---"
sidecar "cat $DIR/cpu.max; cat $DIR/cpu.max.burst 2>/dev/null || true; echo '--- cpu.stat ---'; cat $DIR/cpu.stat"

echo
echo "NOTE: a subsequent 'docker compose up -d --force-recreate' throws all of"
echo "this away -- the container gets a new cgroup with Compose's values. Write"
echo "these AFTER recreating, and re-read the file before believing any number."
