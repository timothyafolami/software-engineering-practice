"""
7.2 -- Python: throttled at 30% average CPU, and the four fixes.

WHAT THIS DEMONSTRATES
  The headline failure of this whole topic, reproduced and then fixed in
  one program so the contrast is in one output.

  A container with a 1.0-CPU quota is offered work that needs about 0.35 of
  a CPU. It is nowhere near its limit on average. Run that work across FOUR
  worker threads instead of one and it starts getting frozen anyway,
  because four runnable threads drain a 100ms bucket in ~25ms of wall clock
  and the kernel then dequeues every thread in the cgroup -- including the
  heartbeat, which was using no CPU at all.

  Then four fixes are applied one variable at a time, exactly as the README
  lists them: fewer workers, more quota, a shorter period, and cpu.max.burst.

  Python earns its place here for a reason the other five cannot show: the
  GIL makes wall time and CPU time different things in the same thread, so
  this file charges the budget with `time.thread_time()` rather than a
  stopwatch. Charge wall time under a GIL and you bill a thread for time it
  spent waiting to run, and the whole measurement inverts.

WHAT TO LOOK FOR IN THE OUTPUT
  1. Row 1 vs row 2: same offered load, same quota, only the worker count
     changes -- and the throttle ratio goes from ~0 to well above it while
     AVERAGE CPU BARELY MOVES. That divergence is the entire lesson.
  2. The heartbeat max gap. It should land near a multiple of the period
     length. The period is the fingerprint; a p99 that is suspiciously
     close to 100ms is not a coincidence, it is the diagnosis.
  3. The `50ms period` row: identical average CPU, identical throughput,
     smaller freezes. Same allowance, finer granularity.
  4. The burst row: nr_bursts stops being zero. Neither Docker nor
     Kubernetes sets cpu.max.burst, which is why almost nobody has seen
     this column be nonzero in production.

RUN
    python3 quota_freeze.py

  On macOS the quota is enforced by the userspace model in
  00-harness/local/cfs_sim.py and the program says so. Inside a Linux
  container, run ../docker/run_7_2.sh for the kernel's own numbers.
"""
from __future__ import annotations

import random
import sys
import threading
import time
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2] / "00-harness" / "local"
sys.path.insert(0, str(HARNESS))

import cgroup  # noqa: E402
from cfs_sim import CpuBudget, burn_cpu, calibrate_ms_per_block, fallback_banner  # noqa: E402
from openloop import table  # noqa: E402

WORK_MS = 40.0            # CPU cost of one request. A FastAPI handler that
                          # validates and serialises a few hundred rows
                          # really does cost this much; "thin" handlers are
                          # rarer than people think.
OFFERED_RATE = 9.0        # requests/sec -> ~0.36 CPU of demand. Deliberately
                          # far under the 1.0 CPU quota: the point is that
                          # being under your limit on average saves you
                          # from nothing.
RUN_SECONDS = 15.0        # long enough that p99 is ~135 samples, not ~90
HEARTBEAT_MS = 10.0
PERIOD_US = 100_000


def thread_census() -> int:
    """OS threads this process has, before we start any of our own."""
    linux = Path("/proc/self/status")
    if linux.exists():
        for line in linux.read_text().splitlines():
            if line.startswith("Threads:"):
                return int(line.split()[1])
    try:
        import os
        import subprocess

        out = subprocess.check_output(
            ["ps", "-M", "-p", str(os.getpid())], text=True, stderr=subprocess.DEVNULL
        )
        return max(1, len(out.strip().splitlines()) - 1)
    except Exception:
        return threading.active_count()


class Heartbeat:
    """A thread that wants to tick every HEARTBEAT_MS and records when it can't.

    It burns no measurable CPU, so it can never be the cause of throttling.
    It is frozen anyway. That asymmetry -- punished for someone else's
    consumption -- is what makes a health check fail on a container whose
    own CPU graph looks calm.
    """

    def __init__(self, budget: CpuBudget) -> None:
        self.budget = budget
        self.gaps_ms: list[float] = []
        self.ticks = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        last = time.perf_counter()
        while not self._stop.is_set():
            self._stop.wait(HEARTBEAT_MS / 1000.0)
            self.budget.park_if_throttled()
            now = time.perf_counter()
            self.gaps_ms.append((now - last) * 1000.0)
            last = now
            self.ticks += 1

    def __enter__(self) -> "Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    @property
    def max_gap_ms(self) -> float:
        return max(self.gaps_ms) if self.gaps_ms else float("nan")


def run_variant(
    workers: int,
    quota_cpus: float,
    period_us: int = PERIOD_US,
    burst_us: int = 0,
) -> dict:
    """One row of the table. Open-loop arrivals into a fixed worker pool."""
    quota_us = int(quota_cpus * period_us)
    latencies: list[float] = []
    lock = threading.Lock()
    inbox: list[float] = []
    inbox_cv = threading.Condition(lock)
    stop = threading.Event()

    with CpuBudget(quota_us, period_us, burst_us=burst_us) as budget:
        def worker() -> None:
            while True:
                with inbox_cv:
                    while not inbox and not stop.is_set():
                        inbox_cv.wait(timeout=0.05)
                    if not inbox:
                        if stop.is_set():
                            return
                        continue
                    due = inbox.pop(0)
                burn_cpu(WORK_MS, budget)
                with lock:
                    latencies.append((time.perf_counter() - due) * 1000.0)

        pool = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
        for thread in pool:
            thread.start()

        with Heartbeat(budget) as heartbeat:
            # Open loop, POISSON arrivals. Two deliberate choices:
            #
            # Open, because a closed loop would slow its own sending rate
            # the instant the container froze and would measure this system
            # as healthy -- coordinated omission.
            #
            # Poisson, because evenly-spaced arrivals cannot reproduce this
            # failure at all. Throttling at low average utilisation is a
            # BURSTINESS effect: the bucket is drained by demand that
            # clumps inside one 100ms window, not by demand averaged over a
            # minute. Real traffic clumps. A `for i in range(n)` load
            # generator does not, which is why hand-rolled load tests so
            # reliably fail to reproduce production tail latency.
            rng = random.Random(20260818)
            start = time.perf_counter()
            due = start
            while due - start < RUN_SECONDS:
                sleep_for = due - time.perf_counter()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                with inbox_cv:
                    inbox.append(due)
                    inbox_cv.notify()
                due += rng.expovariate(OFFERED_RATE)

            deadline = time.perf_counter() + 5.0
            while inbox and time.perf_counter() < deadline:
                time.sleep(0.01)
            time.sleep(0.2)
            avg_cpu = budget.average_cpu_percent()
            stat = budget.stat
            hb_gap = heartbeat.max_gap_ms
            hb_ticks = heartbeat.ticks

        stop.set()
        with inbox_cv:
            inbox_cv.notify_all()
        for thread in pool:
            thread.join(timeout=2.0)

    ordered = sorted(latencies)
    def pct(p: float) -> float:
        if not ordered:
            return float("nan")
        return ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))]

    return {
        "completed": len(latencies),
        "req_per_s": len(latencies) / RUN_SECONDS,
        "avg_cpu": avg_cpu,
        "stat": stat,
        "p50": pct(50),
        "p99": pct(99),
        "hb_gap": hb_gap,
        "hb_ticks": hb_ticks,
    }


def main() -> None:
    print("7.2 -- throttled at 30% CPU: Python")
    print(f"  runtime                : CPython {sys.version.split()[0]}, "
          f"GIL {'on' if getattr(sys, '_is_gil_enabled', lambda: True)() else 'OFF'}")
    for name, value in cgroup.runtime_cpu_answers().items():
        print(f"  {name:<22} : {value}")
    quota = cgroup.cpu_quota()
    print(f"  quota actually enforced: "
          f"{f'{quota:.2f} CPU' if quota else 'none (no cgroup on this host)'}")
    print(f"  OS threads at rest     : {thread_census()}  "
          "(before this program starts any of its own)")
    print()
    if not cgroup.have_cgroup_v2():
        print(fallback_banner("no /sys/fs/cgroup on this host"))
    else:
        # Say it here too. Every row of the table below comes from the same
        # userspace bucket in cfs_sim.py whether or not a real quota exists --
        # a process cannot write its own cgroup, so "2.0 CPU", "50ms period"
        # and "+ burst" could not be kernel-enforced from in here even in
        # principle. Printing "quota actually enforced: 1.00 CPU" and then a
        # column headed `throttled/periods` invites the reader to take these
        # for cpu.stat readings. They are not, and the file should say so in
        # the one place a reader is looking.
        print(fallback_banner(
            "a real quota IS enforced here, but the table below is still the "
            "userspace model: this process cannot rewrite its own cgroup, so "
            "the four fix rows could not be kernel-enforced from inside it. "
            "For the kernel's own cpu.stat, run ../docker/run_7_2.sh"))

    print(f"  one hash block costs {calibrate_ms_per_block():.3f} ms here (measured)")
    print(f"  offered load: {OFFERED_RATE:.0f} req/s x {WORK_MS:.0f}ms CPU "
          f"= {OFFERED_RATE * WORK_MS / 1000:.2f} CPU of demand")
    print(f"  quota:        1.00 CPU. The demand is comfortably under the limit.")
    print(f"  heartbeat wants a tick every {HEARTBEAT_MS:.0f}ms; {RUN_SECONDS:.0f}s per row")
    print()

    variants = [
        ("4 workers, 1.0 CPU  (baseline)", dict(workers=4, quota_cpus=1.0)),
        ("fix 1: 1 worker, 1.0 CPU", dict(workers=1, quota_cpus=1.0)),
        ("fix 2: 4 workers, 2.0 CPU", dict(workers=4, quota_cpus=2.0)),
        ("fix 3: 4 workers, 50ms period", dict(workers=4, quota_cpus=1.0, period_us=50_000)),
        ("fix 4: 4 workers, + burst", dict(workers=4, quota_cpus=1.0, burst_us=100_000)),
    ]

    rows = []
    for label, kwargs in variants:
        result = run_variant(**kwargs)
        stat = result["stat"]
        rows.append([
            label,
            f"{result['completed']}",
            f"{result['req_per_s']:.1f}",
            f"{result['avg_cpu']:.0f}%",
            f"{stat.nr_throttled}/{stat.nr_periods}",
            f"{stat.throttle_ratio:.3f}",
            f"{result['p50']:.0f}",
            f"{result['p99']:.0f}",
            f"{result['hb_gap']:.0f}",
            str(stat.nr_bursts),
        ])
        print(f"  ran: {label}")

    print()
    print(table(rows, ["variant", "n", "req/s", "avg CPU", "throttled",
                       "ratio", "p50 ms", "p99 ms", "hb gap ms", "bursts"]))
    print()
    print("  The baseline row is the whole topic: average CPU well under the")
    print("  limit, throttle ratio well over zero, p50 fine, p99 and heartbeat")
    print("  gap destroyed. No dashboard built on average utilisation can see")
    print("  this. /sys/fs/cgroup/cpu.stat can see it in ten seconds.")
    print()
    print("  Throughput is identical on every row. The quota was never the")
    print("  throughput ceiling -- it was only ever the source of the freezes.")
    print("  That is why 'add more workers' so often makes p99 worse and buys")
    print("  no capacity at all.")
    print()
    print("  Read fix 1 and fix 3 carefully; neither is a free win.")
    print("   * Fix 1 cuts the freeze because one thread cannot drain the")
    print("     bucket faster than the kernel refills it -- a single-threaded")
    print("     process under a 1.0 CPU quota is very nearly unthrottleable.")
    print("     What it gives up is parallelism during a burst: arrivals now")
    print("     queue behind each other instead of running side by side.")
    print("   * Fix 3 only helps while one request's CPU cost is SMALL")
    print("     compared with the period. Halve the period below the cost of")
    print("     a single request and one request can exhaust a whole period")
    print("     on its own, which makes throttling more frequent, not less.")
    print("     Check your own numbers before reaching for this one.")


if __name__ == "__main__":
    main()
