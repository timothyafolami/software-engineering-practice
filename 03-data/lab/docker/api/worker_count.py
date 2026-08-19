"""
How many workers should this process start? Ask the quota, not the kernel.

    python3 07-connection-pools/python/worker_count.py

WHAT IT DEMONSTRATES: `os.cpu_count()` inside a container reports the HOST's
cores. It has no idea a CPU quota exists. So the conventional
`workers = 2 * cores + 1` on a container limited to `cpus: "0.5"` starts nine
worker processes on half a core -- nine processes' worth of pool connections,
one half-process's worth of CPU, and CFS throttling on top of that.

The quota lives in `/sys/fs/cgroup/cpu.max` (cgroup v2) and reads as two
numbers, `QUOTA PERIOD`, both in microseconds:

    50000 100000    -> half a CPU
    100000 100000   -> one CPU
    max 100000      -> unlimited

Dividing the first by the second is the only correct way to ask "how much CPU do
I have". cgroup v1 puts the same pair in two separate files under
`/sys/fs/cgroup/cpu/`, which this program also reads.

THIS IS LINUX-ONLY AND IT MUST RUN INSIDE A CONTAINER. `/sys/fs/cgroup` does not
exist on macOS; a script that reads it on a Mac finds nothing and reports
nothing, which is not a result. On this machine the program says so and prints
the command that runs it where the quota is real:

    CPUS=0.5 docker compose -f lab/docker/compose.yml run --rm api worker_count.py

CPUS must precede `docker compose`, not arrive as `-e CPUS=0.5`: it is a
COMPOSE variable interpolated into the service's `cpus:` limit, not a process
environment variable. Passed with -e it changes nothing and the quota stays
at the compose default.

WHAT TO LOOK FOR: the two worker counts, and the connection arithmetic under
them. The gap between "workers from os.cpu_count()" and "workers from the quota"
is the number of extra processes you are paying pool connections for and getting
no CPU for.
"""
from __future__ import annotations

import math
import os
import platform
import sys

CGROUP_V2 = "/sys/fs/cgroup/cpu.max"
CGROUP_V1_QUOTA = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
CGROUP_V1_PERIOD = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"

POOL_SIZE = int(os.environ.get("POOL_SIZE", "5"))
MAX_OVERFLOW = int(os.environ.get("MAX_OVERFLOW", "10"))
REPLICAS = int(os.environ.get("REPLICAS", "10"))


def read_quota() -> tuple[float | None, str]:
    """Available CPU from the cgroup, and where the number came from."""
    try:
        with open(CGROUP_V2) as fh:
            quota, period = fh.read().split()
        if quota == "max":
            return None, f"{CGROUP_V2}: 'max' -- no quota set"
        return int(quota) / int(period), f"{CGROUP_V2}: {quota} {period}"
    except FileNotFoundError:
        pass
    try:
        with open(CGROUP_V1_QUOTA) as fh:
            quota = int(fh.read().strip())
        with open(CGROUP_V1_PERIOD) as fh:
            period = int(fh.read().strip())
        if quota <= 0:
            return None, f"{CGROUP_V1_QUOTA}: {quota} -- no quota set"
        return quota / period, f"cgroup v1: quota={quota} period={period}"
    except FileNotFoundError:
        return None, "no cgroup cpu files on this system"


def workers_for(cpus: float) -> int:
    """The conventional formula, applied to whatever you hand it.

    The formula is not the problem. What you hand it is.
    """
    return max(1, math.ceil(2 * cpus + 1))


def main() -> None:
    print("=" * 78)
    print("Worker count: os.cpu_count() vs the cgroup quota")
    print("=" * 78)
    print(f"  platform      {platform.system()} {platform.machine()}")
    print(f"  os.cpu_count() = {os.cpu_count()}")

    quota, source = read_quota()
    print(f"  quota source  {source}")

    if quota is None and platform.system() != "Linux":
        print("\n  BLOCKED on this machine, honestly: /sys/fs/cgroup does not exist on")
        print(f"  {platform.system()}. There is no quota to read, so there is no experiment")
        print("  here -- a command that appears to do nothing is not a result.")
        print("\n  unblock: run it inside a container, where the quota is real.")
        print("  CPUS goes in front of `docker compose`, NOT in a -e flag: compose")
        print("  substitutes ${CPUS} into the service's `cpus:` limit while it parses")
        print("  the file, so it has to be in compose's own environment. `-e CPUS=0.5`")
        print("  sets a variable INSIDE the container and leaves the quota at its")
        print("  default -- cpu.max still reads 200000 100000 and every run prints the")
        print("  same numbers.")
        print("    CPUS=0.5 docker compose -f lab/docker/compose.yml \\")
        print("      run --rm api python worker_count.py")
        print("\n  and repeat at cpus 0.5, 1.0 and 2.0, recording all six combinations:")
        print("    workers from os.cpu_count(), workers from cpu.max, req/s, p99.")
        print("\n  What the numbers would be here if the host's cores were the quota:")
        quota = float(os.cpu_count() or 1)
        hypothetical = True
    else:
        hypothetical = False

    if quota is None:
        print("\n  Running on Linux with no CPU quota set: the container can use every core")
        print("  the host has, so os.cpu_count() is correct here and this whole class of bug")
        print("  does not apply. It applies the moment somebody adds a `cpus:` line.")
        return

    naive = workers_for(os.cpu_count() or 1)
    correct = workers_for(quota)

    print(f"\n  {'sized from':<26}{'cpus seen':>11}{'workers':>9}"
          f"{'max connections':>18}{'x {} replicas'.format(REPLICAS):>18}")
    print("  " + "-" * 82)
    per_worker = POOL_SIZE + MAX_OVERFLOW
    for label, cpus, workers in (("os.cpu_count()", float(os.cpu_count() or 1), naive),
                                 ("cgroup cpu.max", quota, correct)):
        print(f"  {label:<26}{cpus:>11.2f}{workers:>9}"
              f"{workers * per_worker:>18,}{workers * per_worker * REPLICAS:>18,}")

    print(f"\n  pool_size={POOL_SIZE} + max_overflow={MAX_OVERFLOW} = {per_worker} connections "
          f"per worker.")
    print(f"  total possible = replicas x workers x (pool_size + max_overflow)")
    if not hypothetical and naive != correct:
        extra = (naive - correct) * per_worker * REPLICAS
        print(f"\n  Sizing from os.cpu_count() asks for {extra:,} more connections than the")
        print("  quota can ever use CPU for. Those processes are not idle -- they are")
        print("  scheduled, throttled by CFS, and each one holds its pool open.")
    if hypothetical:
        print("\n  (the two rows above are identical because there is no quota here --")
        print("   that IS the point, and it is why this has to be run in a container)")


if __name__ == "__main__":
    main()
