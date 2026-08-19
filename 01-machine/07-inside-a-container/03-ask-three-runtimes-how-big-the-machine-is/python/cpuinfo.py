"""
7.3 -- Python: three calls, three answers, none of them the enforced number.

WHAT THIS DEMONSTRATES
  The load-bearing sentence for every Python service in a container:
  NOTHING IN THE STANDARD LIBRARY READS YOUR CPU QUOTA. Not the old call,
  not the Linux-specific one, not the shiny cross-platform 3.13 one.

    os.cpu_count()                -> host logical CPUs. Ignores affinity,
                                     ignores quota.
    len(os.sched_getaffinity(0))  -> the affinity mask. Correct under
                                     cpuset.cpus, BLIND to cpu.max. Linux only.
    os.process_cpu_count()  (3.13+) -> the cross-platform replacement for
                                     the above. Honours PYTHON_CPU_COUNT and
                                     -X cpu_count. STILL blind to cpu.max.

  Under the overwhelmingly common container spec -- `--cpus=2` on an
  8-core host -- all three say 8, and the kernel enforces 2. That gap is
  not an error you can catch; it is a plausible number, 4x too large, fed
  straight into every worker-count formula in every deployment guide.

  The one that hurts: `workers = 2 * os.cpu_count() + 1` is the most-copied
  line in Python deployment advice, and on an 8-core host it returns 17. It
  returns 17 whether your quota is 8 CPUs or 0.5. This program computes it
  next to the honest answer so you can see the size of the mistake rather
  than be told about it.

WHAT TO LOOK FOR IN THE OUTPUT
  1. The three runtime answers, then the enforced quota underneath. Inside
     a container under `--cpus=1.5` the first three agree with each other
     and disagree with the fourth. That is the whole matrix.
  2. The "workers" block at the bottom: what four common formulas return
     here, and what the quota says they should have returned.
  3. The env-var route. `WEB_CONCURRENCY` is not a workaround for a broken
     API -- it is the better engineering, because the deployment already
     knows the answer and making it say so out loud beats making the
     process guess.

RUN
    python3 cpuinfo.py

  Run it INSIDE a Linux container to see the disagreement:
    docker run --rm --cpus=1.5 -v "$PWD:/w" -w /w python:3.13-slim python cpuinfo.py
    docker run --rm --cpuset-cpus=0,1 -v "$PWD:/w" -w /w python:3.13-slim python cpuinfo.py

  On macOS every cgroup reading is None -- correctly, since Darwin has no
  cgroupfs -- so the matrix collapses to one column and there is nothing
  to compare. The program says so rather than printing a zero.
"""
from __future__ import annotations

import math
import os
import platform
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2] / "00-harness" / "local"
sys.path.insert(0, str(HARNESS))

import cgroup  # noqa: E402
from openloop import table  # noqa: E402


def answer(label: str, call: str, value, question: str, tracks: str) -> list[str]:
    return [label, call, "n/a" if value is None else str(value), question, tracks]


def main() -> None:
    print("7.3 -- how big is this machine? Python's answers")
    print(f"  interpreter : CPython {sys.version.split()[0]} on "
          f"{platform.system()} {platform.machine()}")
    print(f"  GIL         : {'enabled' if getattr(sys, '_is_gil_enabled', lambda: True)() else 'DISABLED (free-threaded build)'}")
    print()

    # ---- what the runtime will tell you ---------------------------------
    rows = []
    rows.append(answer(
        "os.cpu_count()", "os.cpu_count()", os.cpu_count(),
        "(1) how big is the machine", "nothing -- the host, always"))

    if hasattr(os, "process_cpu_count"):  # 3.13+
        rows.append(answer(
            "os.process_cpu_count()", "os.process_cpu_count()",
            os.process_cpu_count(),
            "(2) which CPUs may I use", "affinity mask + PYTHON_CPU_COUNT"))
    else:
        rows.append(["os.process_cpu_count()", "os.process_cpu_count()", "n/a",
                     "(2) which CPUs may I use", "not present before 3.13"])

    if hasattr(os, "sched_getaffinity"):  # Linux only
        rows.append(answer(
            "len(sched_getaffinity(0))", "len(os.sched_getaffinity(0))",
            len(os.sched_getaffinity(0)),
            "(2) which CPUs may I use", "cpuset.cpus"))
    else:
        rows.append(["len(sched_getaffinity(0))", "len(os.sched_getaffinity(0))",
                     "n/a", "(2) which CPUs may I use",
                     f"Linux-only call; absent on {platform.system()}"])

    quota = cgroup.cpu_quota()
    rows.append([
        "/sys/fs/cgroup/cpu.max", "cgroup.cpu_quota()",
        "n/a" if quota is None else f"{quota:.2f}",
        "(3) how much CPU TIME may I consume",
        "cpu.max -- THE ENFORCED NUMBER"])

    print(table(rows, ["what people call", "the call", "answer here",
                       "which question it answers", "what it tracks"]))
    print()

    # ---- and what is actually enforced ----------------------------------
    print("  ground truth on this host:")
    if cgroup.have_cgroup_v2():
        print(f"    cpu.max               {'no ceiling' if quota is None else f'{quota:.2f} CPU'}")
        print(f"    cpuset.cpus.effective {cgroup.cpuset_effective()}")
        print(f"    cpu.weight            {cgroup.cpu_weight()}")
        memory = cgroup.memory_limit()
        host_memory = cgroup.host_memory_bytes()
        print(f"    memory.max            "
              f"{'no ceiling' if memory is None else f'{memory / 2**20:.0f} MiB'}")
        print(f"    /proc/meminfo says    "
              f"{'?' if host_memory is None else f'{host_memory / 2**20:.0f} MiB'}"
              "   <- the HOST's memory, not yours (7.6)")
    else:
        print("    " + cgroup.environment_banner().strip().replace("\n", "\n    "))
    print()

    if quota is None:
        print("  NOTE: no CPU quota is enforced here, so every row above agrees and")
        print("        the matrix has one column. That is the correct result on this")
        print("        host, and it is also why this experiment has to run inside a")
        print("        container: run it under --cpus=1.5 and the rows separate.")
        print()

    # ---- the consequence, in the units people actually type --------------
    host = os.cpu_count() or 1
    affinity = (len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity")
                else os.process_cpu_count() if hasattr(os, "process_cpu_count")
                else host)
    enforced = quota

    print("  What each common worker formula returns HERE:")
    formulas = [
        ("2 * os.cpu_count() + 1  (the most-copied line in Python deploy docs)",
         2 * host + 1),
        ("os.cpu_count()", host),
        ("len(os.sched_getaffinity(0))" if hasattr(os, "sched_getaffinity")
         else "os.process_cpu_count()", affinity),
        ("multiprocessing.cpu_count()", host),
    ]
    for label, value in formulas:
        print(f"    {value:>4}   {label}")

    honest = max(1, math.floor(enforced)) if enforced else None
    print()
    if honest is not None:
        print(f"  What the quota says a CPU-bound service should run: {honest} worker(s)")
        print(f"    (floor of {enforced:.2f} CPU. Each additional CPU-saturated worker")
        print("     beyond the quota buys no throughput -- the quota was always the")
        print("     ceiling -- and costs tail latency. That is 7.2, measured.)")
        worst = 2 * host + 1
        if worst > honest:
            print(f"    The popular formula would have given you {worst}: "
                  f"{worst / honest:.0f}x too many.")
    else:
        print("  No quota to derive a worker count from on this host. In production")
        print("  the number comes from cpu.max, or from the env var below.")

    print()
    print("  The env-var route, which is the better engineering:")
    for name in ("WEB_CONCURRENCY", "PYTHON_CPU_COUNT", "GOMAXPROCS", "UV_THREADPOOL_SIZE"):
        print(f"    {name:<20} {os.environ.get(name, '<unset>')}")
    print("    Whatever sets `cpus:` in your manifest already knows the number.")
    print("    Making the deployment say it out loud beats making the process")
    print("    guess -- and it is the only route that works for a third-party")
    print("    library that sized its own pool from os.cpu_count() at import.")
    print()
    print("  Python will not fix this in the standard library. The twenty lines")
    print("  that do read the enforced number are in")
    print("  ../../00-harness/local/cgroup.py -- copy them, or set the env var.")


if __name__ == "__main__":
    main()
