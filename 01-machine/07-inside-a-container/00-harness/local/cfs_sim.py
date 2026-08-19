"""
A userspace model of cgroup v2 CFS bandwidth control, for machines with no
cgroupfs (macOS, or Linux with the Docker daemon down).

WHAT THIS IS NOT: this is not the kernel. There is no cgroup here, nothing
reads /sys/fs/cgroup, and `nr_throttled` below is a counter this file
increments, not one the kernel wrote. Anything that imports this module
prints a FALLBACK banner saying so.

WHAT IT IS: the same accounting rule the kernel uses, applied to real
threads doing real work. Every `period_us` a bucket is refilled with
`quota_us` microseconds. Threads consume from the bucket as they burn CPU.
When the bucket hits zero every thread in the group is parked until the
next period boundary. That single rule is the whole of 7.1 and 7.2, and it
produces the real latency signature on real threads, on this machine,
today -- freezes quantised to the period length, tail latency that is a
multiple of 100ms, and a throttle ratio that moves independently of
average CPU utilisation.

Why Python threads can burn CPU in parallel here despite the GIL:
hashlib releases the GIL around each `update()` of a large buffer, so N
threads hashing 256 KiB blocks really do occupy N cores. The workload is
also an honest stand-in for what a FastAPI process actually spends CPU on:
serialisation, hashing, template rendering.

Ground truth for the real thing is always `/sys/fs/cgroup/cpu.stat` inside
the container. See ../../02-throttled-at-30-percent-cpu/python/cpu_stat_watch.py.

RUN
    python3 cfs_sim.py

  Runs the smallest demonstration the module can make: one thread and then
  four, burning identical CPU under an identical 1.0-CPU bucket, so the
  throttle counters move while the work does not. The real experiment is
  ../../02-throttled-at-30-percent-cpu/python/quota_freeze.py.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field

DEFAULT_PERIOD_US = 100_000  # the kernel default, and Docker's

_HASH_BLOCK = os.urandom(256 * 1024)


def fallback_banner(what: str) -> str:
    """Every program using this module prints this. It must never be
    mistaken for a measurement taken against the kernel."""
    return (
        f"  !! FALLBACK: {what}\n"
        f"  !! This is a userspace MODEL of cpu.max, not the Linux kernel.\n"
        f"  !! Real numbers come from /sys/fs/cgroup/cpu.stat inside a container.\n"
    )


@dataclass
class CpuStat:
    """Shaped exactly like /sys/fs/cgroup/cpu.stat so the habit transfers."""

    usage_usec: int = 0
    nr_periods: int = 0
    nr_throttled: int = 0
    throttled_usec: int = 0
    nr_bursts: int = 0
    burst_usec: int = 0

    @property
    def throttle_ratio(self) -> float:
        return self.nr_throttled / self.nr_periods if self.nr_periods else 0.0

    def render(self, indent: str = "  ") -> str:
        lines = [f"{indent}{k} {v}" for k, v in vars(self).items()]
        lines.append(f"{indent}# nr_throttled/nr_periods = {self.throttle_ratio:.3f}")
        return "\n".join(lines)


class CpuBudget:
    """One cgroup's worth of CPU bandwidth.

    quota_us=None means `cpu.max` is `max <period>` -- no ceiling at all,
    which is the Docker default and the thing people forget they are
    comparing against.
    """

    def __init__(
        self,
        quota_us: int | None,
        period_us: int = DEFAULT_PERIOD_US,
        burst_us: int = 0,
        name: str = "",
    ) -> None:
        self.quota_us = quota_us
        self.period_us = period_us
        self.burst_us = burst_us
        self.name = name

        self.stat = CpuStat()
        self._cond = threading.Condition(threading.Lock())
        self._bank = 0  # cpu.max.burst: unused quota banked from earlier periods
        self._allowance = quota_us or 0
        self._balance = quota_us or 0
        self._throttled_this_period = False
        self._stop = threading.Event()
        self._refill: threading.Thread | None = None
        self._wall_start = 0.0

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "CpuBudget":
        self._wall_start = time.perf_counter()
        if self.quota_us is not None:
            self._refill = threading.Thread(target=self._refill_loop, daemon=True)
            self._refill.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._refill:
            self._refill.join(timeout=1.0)

    # -- the accounting rule ----------------------------------------------

    def spend(self, micros: float) -> None:
        """Charge CPU time already burned, then park if the bucket is empty.

        This is the whole mechanism. The caller has already run; the kernel
        also charges after the fact, in slices, which is why a container can
        very slightly overshoot its quota within a period.
        """
        if self.quota_us is None:
            self.stat.usage_usec += int(micros)
            return
        with self._cond:
            self._balance -= micros
            self.stat.usage_usec += int(micros)
            while self._balance <= 0 and not self._stop.is_set():
                self._throttled_this_period = True
                frozen_at = time.perf_counter()
                self._cond.wait(timeout=self.period_us / 1e6)
                self.stat.throttled_usec += int(
                    (time.perf_counter() - frozen_at) * 1e6
                )

    def park_if_throttled(self) -> float:
        """Wait out a freeze without consuming any quota. Returns ms parked.

        This is the half of the mechanism people forget. Throttling dequeues
        EVERY task in the cgroup, not just the ones burning CPU. A heartbeat
        thread, a health-check handler, a metrics scraper -- all frozen, all
        contributing nothing to the usage that got you frozen. It is why a
        container can fail a liveness probe while its own CPU graph looks
        calm.
        """
        if self.quota_us is None:
            return 0.0
        with self._cond:
            if self._balance > 0:
                return 0.0
            frozen_at = time.perf_counter()
            while self._balance <= 0 and not self._stop.is_set():
                self._cond.wait(timeout=self.period_us / 1e6)
            return (time.perf_counter() - frozen_at) * 1000.0

    def _refill_loop(self) -> None:
        """Refill on absolute deadlines so periods do not drift."""
        start = time.perf_counter()
        elapsed_periods = 0
        period_s = self.period_us / 1e6
        while not self._stop.is_set():
            elapsed_periods += 1
            sleep_for = start + elapsed_periods * period_s - time.perf_counter()
            if sleep_for > 0:
                self._stop.wait(sleep_for)
            if self._stop.is_set():
                return
            with self._cond:
                self._close_period()
                self._open_period()
                self._cond.notify_all()

    def _close_period(self) -> None:
        used = self._allowance - max(0.0, self._balance)
        over = max(0.0, used - self.quota_us)
        under = max(0.0, self.quota_us - used)
        self.stat.nr_periods += 1
        if self._throttled_this_period:
            self.stat.nr_throttled += 1
        if over > 0:
            self.stat.nr_bursts += 1
            self.stat.burst_usec += int(over)
        self._bank = max(0.0, min(float(self.burst_us), self._bank + under - over))

    def _open_period(self) -> None:
        self._throttled_this_period = False
        self._allowance = self.quota_us + self._bank
        self._balance = self._allowance

    # -- reporting ---------------------------------------------------------

    def describe(self) -> str:
        if self.quota_us is None:
            return f'cpu.max "max {self.period_us}"  (no ceiling)'
        cpus = self.quota_us / self.period_us
        return (
            f'cpu.max "{self.quota_us} {self.period_us}"  '
            f"({cpus:.2f} CPU, {self.period_us / 1000:.0f}ms period"
            + (f", burst {self.burst_us}us" if self.burst_us else "")
            + ")"
        )

    def average_cpu_percent(self) -> float:
        """usage_usec over wall time -- exactly what `docker stats` shows you,
        and exactly the number that fails to reveal throttling."""
        wall_us = (time.perf_counter() - self._wall_start) * 1e6
        return 100.0 * self.stat.usage_usec / wall_us if wall_us else 0.0


# -- the work itself -------------------------------------------------------

_CHUNKS_PER_CHARGE = 4  # ~0.4ms of hashing between charges; the kernel uses 5ms slices


def burn_cpu(target_ms: float, budget: CpuBudget | None = None) -> None:
    """Burn roughly `target_ms` of real CPU on real work, charging a budget.

    Uses thread CPU time, not wall time, so time spent parked by the budget
    is not miscounted as work done. Cost is measured, not assumed: the loop
    runs until the thread has actually consumed target_ms of CPU.
    """
    target_s = target_ms / 1000.0
    digest = hashlib.sha256()
    consumed = 0.0
    while consumed < target_s:
        mark = time.thread_time()
        for _ in range(_CHUNKS_PER_CHARGE):
            digest.update(_HASH_BLOCK)
        slice_s = time.thread_time() - mark
        consumed += slice_s
        if budget is not None:
            budget.spend(slice_s * 1e6)
    digest.hexdigest()  # keep the optimiser honest: the result is used


def calibrate_ms_per_block() -> float:
    """How long one hash block costs on THIS machine. Never hardcode this."""
    mark = time.thread_time()
    digest = hashlib.sha256()
    for _ in range(64):
        digest.update(_HASH_BLOCK)
    digest.hexdigest()
    return (time.thread_time() - mark) * 1000.0 / 64


if __name__ == "__main__":
    import sys

    print("cfs_sim -- userspace model of CFS bandwidth control")
    print(fallback_banner("run directly, so nothing here is the kernel"))
    print(f"  one hash block costs {calibrate_ms_per_block():.3f} ms here (measured)")
    print("  Same offered work, same 1.0-CPU bucket, only the thread count changes.")
    print()

    WORK_MS = 400.0  # total CPU to burn per row, split across the threads

    rows = []
    for threads in (1, 4):
        budget = CpuBudget(DEFAULT_PERIOD_US, DEFAULT_PERIOD_US, name=f"{threads}t")
        with budget:
            started = time.perf_counter()
            pool = [
                threading.Thread(
                    target=burn_cpu, args=(WORK_MS / threads, budget), daemon=True
                )
                for _ in range(threads)
            ]
            for thread in pool:
                thread.start()
            for thread in pool:
                thread.join()
            wall_ms = (time.perf_counter() - started) * 1000.0
            rows.append((threads, wall_ms, budget.stat))

    header = f"  {'threads':>7}  {'wall ms':>8}  {'cpu ms':>7}  {'throttled':>9}  {'ratio':>6}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for threads, wall_ms, stat in rows:
        print(f"  {threads:>7}  {wall_ms:>8.0f}  {stat.usage_usec / 1000:>7.0f}  "
              f"{stat.nr_throttled:>4}/{stat.nr_periods:<4}  {stat.throttle_ratio:>6.3f}")
    print()
    print("  Both rows burned the same CPU. The four-thread row drained the")
    print("  bucket faster, so it spent more periods frozen -- which is 7.2 in")
    print("  four lines. Real numbers come from cpu.stat inside a container.")
    sys.exit(0)
