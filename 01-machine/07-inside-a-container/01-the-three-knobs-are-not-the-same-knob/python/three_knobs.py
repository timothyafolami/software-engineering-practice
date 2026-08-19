"""
7.1 -- cpu.weight, cpu.max and cpuset.cpus are three different mechanisms
that happen to wear similar-looking numbers.

WHAT THIS DEMONSTRATES
  The 2x3 table from the README, measured rather than asserted:

    * weight (no quota)  costs you NOTHING on an idle host and everything
      on a contended one. It is a tie-breaker, not a ceiling.
    * quota (cpu.max)    costs you EXACTLY THE SAME on both. The bucket
      does not know or care whether anyone else wants the CPU. This is why
      a container can be throttled at 3am on an empty box.
    * cpuset             looks like quota on throughput and produces
      nr_throttled = 0, because narrowing which CPUs you may run on is a
      different mechanism from stopping you at a period boundary.

  The last column is the one to internalise. Two configs with the same
  throughput, one of which shows throttling and one of which cannot, is
  the difference between "you are being frozen for 87ms at a time" and
  "you are running steadily on fewer cores". Those need different fixes.

WHAT TO LOOK FOR IN THE OUTPUT
  1. The quota row's idle and contended numbers should be close to equal.
     Everything else about this topic follows from that one fact.
  2. The no-ceiling row should drop sharply between the two columns. That
     drop is the thing cpu.weight arbitrates. Weight decides how the drop
     is shared out; it never causes it.
  3. nr_throttled is nonzero for exactly one row.

RUN
    python3 three_knobs.py

  On Linux, inside a container, this reads the real cgroup files and the
  numbers are the kernel's. On macOS there is no cgroupfs, so the quota row
  is produced by the userspace model in 00-harness/local/cfs_sim.py and the
  program says so loudly. The contention column is real on both: it is
  produced by actually saturating this machine's cores.
"""
from __future__ import annotations

import multiprocessing
import os
import sys
import threading
import time
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[3] / "07-inside-a-container" / "00-harness" / "local"
sys.path.insert(0, str(HARNESS))

import cgroup  # noqa: E402
from cfs_sim import CpuBudget, burn_cpu, calibrate_ms_per_block, fallback_banner  # noqa: E402
from openloop import table  # noqa: E402

WORK_MS = 15.0          # one "request" of CPU, same unit as the harness /cpu
WORKER_THREADS = 4      # four uvicorn workers' worth of runnable threads
MEASURE_S = 4.0
QUOTA_CPUS = 1.0
CPUSET_CPUS = 1         # "cpuset: 0" -- one CPU, modelled as one worker thread


# ------------------------------------------------------------ contention

class HostHog:
    """Make the host genuinely busy, using real processes on real cores.

    Processes, not threads: under CPython's GIL, threads spinning in pure
    Python would contend for the GIL rather than for CPUs, and would not
    make the machine busy at all. This is the same distinction Topic 2 drew
    between a runtime's concurrency and the kernel's.
    """

    def __init__(self, procs: int) -> None:
        self.procs = procs
        self._pool: list[multiprocessing.Process] = []
        self._stop = multiprocessing.Event()
        # macOS defaults multiprocessing to SPAWN, not fork: each child is a
        # fresh interpreter that re-imports this module, which takes real
        # time. Waiting a fixed 0.3s is how you accidentally measure an idle
        # host twice and conclude contention does not matter.
        self._ready = multiprocessing.Barrier(procs + 1, timeout=60)

    def __enter__(self) -> "HostHog":
        for _ in range(self.procs):
            proc = multiprocessing.Process(
                target=_burn_forever, args=(self._stop, self._ready)
            )
            proc.daemon = True
            proc.start()
            self._pool.append(proc)
        self._ready.wait()  # every child is now spinning, not still importing
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        for proc in self._pool:
            proc.join(timeout=2.0)
            if proc.is_alive():
                proc.terminate()


def _burn_forever(stop, ready) -> None:
    # Top level so multiprocessing's spawn start method (the macOS default,
    # not fork) can pickle it.
    ready.wait()
    accumulator = 0
    while not stop.is_set():
        # Check the stop flag rarely. is_set() takes a lock; a child that
        # spends its time acquiring locks is not occupying a core, and the
        # contention column would quietly measure nothing.
        for step in range(200_000):
            accumulator = (accumulator + step) & 0xFFFF


# ------------------------------------------------------------- the work

def saturate(workers: int, budget: CpuBudget | None, seconds: float) -> int:
    """Run `workers` threads doing WORK_MS units flat out; count completions."""
    completed = [0] * workers
    stop = threading.Event()

    def worker(slot: int) -> None:
        while not stop.is_set():
            burn_cpu(WORK_MS, budget)
            completed[slot] += 1

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(workers)]
    for thread in threads:
        thread.start()
    time.sleep(seconds)
    stop.set()
    for thread in threads:
        thread.join(timeout=3.0)
    return sum(completed)


def run_config(name: str, workers: int, quota_cpus: float | None, seconds: float):
    """One cell of the table. Returns (req_per_s, CpuStat or None)."""
    if quota_cpus is None:
        completed = saturate(workers, None, seconds)
        return completed / seconds, None
    period_us = 100_000
    with CpuBudget(int(quota_cpus * period_us), period_us, name=name) as budget:
        completed = saturate(workers, budget, seconds)
        return completed / seconds, budget.stat


# ------------------------------------------------------------------ main

def main() -> None:
    print("7.1 -- the three knobs are not the same knob")
    print(cgroup.environment_banner())
    print()

    real_cgroup = cgroup.have_cgroup_v2()
    if not real_cgroup:
        print(fallback_banner("no /sys/fs/cgroup on this host"))
    else:
        print("  what Docker actually wrote into this container's cgroup:")
        print(f"    cpu.weight            {cgroup.cpu_weight()}")
        quota = cgroup.cpu_quota()
        print(f"    cpu.max               {'no ceiling' if quota is None else f'{quota:.2f} CPU'}")
        print(f"    cpuset.cpus.effective {cgroup.cpuset_effective()}")
        print()
        if quota is None and cgroup.cpu_weight() in (None, 100):
            print("  NOTE: neither a quota nor a non-default weight is set. You are")
            print("        running an unlimited container; rows below will be equal.")
            print()

    cost = calibrate_ms_per_block()
    cpus = os.cpu_count() or 1
    print(f"  calibrated on this machine: one hash block = {cost:.3f} ms")
    print(f"  host cores visible to the runtime: {cpus}")
    print(f"  work unit {WORK_MS:.0f}ms CPU, {WORKER_THREADS} worker threads, "
          f"{MEASURE_S:.0f}s per cell")
    print()

    configs = [
        # (label, workers, quota_cpus, note)
        ("no ceiling (weight only)", WORKER_THREADS, None,
         "cpu.weight decides the SPLIT, only when contended"),
        (f"quota {QUOTA_CPUS:.1f} CPU (cpu.max)", WORKER_THREADS, QUOTA_CPUS,
         "absolute ceiling, enforced by freezing you"),
        (f"cpuset: {CPUSET_CPUS} CPU", CPUSET_CPUS, None,
         "narrows which CPUs, never stops you"),
    ]

    print("  --- column 1: idle host ------------------------------------------")
    idle = {}
    for label, workers, quota, _note in configs:
        rate, stat = run_config(label, workers, quota, MEASURE_S)
        idle[label] = (rate, stat)
        print(f"    {label:<28} {rate:7.1f} req/s")

    print()
    print(f"  --- column 2: every core busy ({cpus} spinning processes) --------")
    contended = {}
    with HostHog(cpus):
        for label, workers, quota, _note in configs:
            rate, stat = run_config(label, workers, quota, MEASURE_S)
            contended[label] = (rate, stat)
            print(f"    {label:<28} {rate:7.1f} req/s")

    print()
    rows = []
    for label, _workers, _quota, note in configs:
        idle_rate, _ = idle[label]
        cont_rate, cont_stat = contended[label]
        if cont_stat is None:
            throttle = "n/a (no bucket)"
        else:
            throttle = (f"{cont_stat.nr_throttled}/{cont_stat.nr_periods}"
                        f" = {cont_stat.throttle_ratio:.3f}")
        change = (cont_rate / idle_rate - 1.0) * 100 if idle_rate else 0.0
        rows.append([label, f"{idle_rate:.1f}", f"{cont_rate:.1f}",
                     f"{change:+.0f}%", throttle])

    print(table(rows, ["config", "idle req/s", "busy req/s", "change", "throttled"]))
    print()
    print("  Read the 'change' column, not the absolute numbers. Quota should")
    print("  barely move; the unlimited row should collapse. That difference is")
    print("  the entire distinction between a share and a ceiling.")
    print()
    print("  What this host CANNOT show: how cpu.weight splits the contended")
    print("  case between two cgroups, because Darwin has no weights to set.")
    print("  For that column run ../docker/run_7_1.sh on a Linux host.")


if __name__ == "__main__":
    main()
