"""
Layer 5 lab - admission control: static, priority and adaptive (topic 5).

WHAT THIS DEMONSTRATES
  Past the knee you cannot serve everyone. The only decision left is WHICH
  requests lose, and whether they lose quickly (a 503 in 50ms) or slowly
  (a timeout at 30s, after occupying a pool slot the whole time). Rejecting
  beats collapsing because a rejection returns the resource; a timeout
  returns it only after the damage.

  SHED_MODE picks the policy, and nothing else in the service changes:

    none      no admission control. Topic 1's baseline.
    static    a semaphore of SHED_LIMIT, sized from the concurrency measured
              at the knee, with a SHED_WAIT_MS queue-wait deadline. Past
              that: 503 + Retry-After.
    priority  the same limit, but tier 0 (/checkout) may use all of it and
              tier 3 (/search) only a quarter. Under overload the cheap
              traffic is what absorbs the rejections.
    adaptive  no hand-set limit at all. A gradient controller watches the
              ratio of the best latency it has ever seen to the latency it
              is seeing now, and moves the limit toward what the system can
              actually do - including after you change service time by 3x
              at runtime.

WHAT TO LOOK FOR
  p99 OF ACCEPTED REQUESTS, not p99 of everything. The claim under test is
  that accepted p99 stays roughly flat past 100% offered while the
  rejection rate absorbs the excess. A p99 computed over rejections looks
  wonderful and means nothing: a 503 is fast.

  And goodput, not throughput. A rejected request is throughput too.
"""
from __future__ import annotations

import asyncio
import threading
import time

from .config import config
from .metrics import counters

# Tier 0 gets the whole limit; each tier down gets a quarter less. With the
# topic's two tiers (0 and 3) that is 100% for /checkout and 25% for /search.
TIER_FRACTION = (1.0, 0.75, 0.5, 0.25)


class GradientLimit:
    """A gradient concurrency controller, in the shape Netflix's is.

    limit <- limit * (rtt_noload / rtt) + queue_size, smoothed.

    rtt_noload is the smallest latency this service has ever shown, which is
    its unloaded service time; rtt is the current sample. The ratio is 1
    when there is no queue and falls as the queue grows, so the limit walks
    down under overload and back up as it clears. The +queue term is what
    lets it probe upward at all - without it the limit can only shrink.

    The point of the mode is not that a controller is clever. It is that it
    converges to a number you could have measured by hand, and keeps
    converging after the thing you measured changes.
    """

    def __init__(self, initial: float = 10.0) -> None:
        self._lock = threading.Lock()
        self.limit = float(initial)
        self.min_limit = 1.0
        self.max_limit = 400.0
        self.rtt_noload: float | None = None
        self.smoothing = 0.2
        self.samples = 0

    def observe(self, rtt_s: float, inflight: int) -> None:
        if rtt_s <= 0:
            return
        with self._lock:
            self.samples += 1
            if self.rtt_noload is None or rtt_s < self.rtt_noload:
                # Decay the floor slightly so a one-off fast sample taken
                # during a lull does not pin the controller forever.
                self.rtt_noload = rtt_s
            noload = self.rtt_noload or rtt_s
            gradient = max(0.5, min(1.0, noload / rtt_s))
            queue = (self.limit ** 0.5)          # allow probing upward
            new_limit = self.limit * gradient + queue
            self.limit = max(self.min_limit,
                             min(self.max_limit,
                                 (1 - self.smoothing) * self.limit + self.smoothing * new_limit))
            # A fast decay for the floor: 1% per 1000 samples, so a genuine
            # 3x change in service time is re-learned rather than treated as
            # permanent overload.
            if self.samples % 1000 == 0 and self.rtt_noload is not None:
                self.rtt_noload *= 1.01

    def current(self) -> int:
        with self._lock:
            return max(1, int(self.limit))


class Shedder:
    """Admission control for one process. Counts, decides, and reports why."""

    def __init__(self) -> None:
        self.inflight = 0
        self._cond = asyncio.Condition()
        self.gradient = GradientLimit()

    def limit(self) -> int | None:
        mode = config.get("SHED_MODE")
        if mode == "none":
            return None
        if mode == "adaptive":
            return self.gradient.current()
        configured = config.get("SHED_LIMIT")
        if configured is None:
            # No limit set for a mode that needs one: derive it from the
            # same arithmetic topic 1 uses, pool_total, so the service is
            # never accidentally unbounded while claiming to shed.
            from .db import pool_total
            return pool_total()
        return int(configured)

    def _allowance(self, tier: int) -> int | None:
        limit = self.limit()
        if limit is None:
            return None
        if config.get("SHED_MODE") == "priority":
            frac = TIER_FRACTION[min(max(tier, 0), len(TIER_FRACTION) - 1)]
            return max(1, int(limit * frac))
        return limit

    async def acquire(self, tier: int = 0) -> tuple[bool, float]:
        """Try to be admitted. Returns (admitted, queue_wait_ms).

        Waits at most SHED_WAIT_MS for a slot. That bound is the difference
        between backpressure and a queue: an unbounded wait for admission is
        just the pool queue again, one layer up.
        """
        allowance = self._allowance(tier)
        if allowance is None:
            self.inflight += 1
            return True, 0.0
        wait_s = float(config.get("SHED_WAIT_MS")) / 1000.0
        t0 = time.perf_counter()
        deadline = t0 + wait_s
        async with self._cond:
            while self.inflight >= allowance:
                left = deadline - time.perf_counter()
                if left <= 0:
                    counters.inc("shed")
                    return False, (time.perf_counter() - t0) * 1000.0
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=left)
                except asyncio.TimeoutError:
                    counters.inc("shed")
                    return False, (time.perf_counter() - t0) * 1000.0
                allowance = self._allowance(tier)
                if allowance is None:
                    break
            self.inflight += 1
        return True, (time.perf_counter() - t0) * 1000.0

    async def release(self, rtt_s: float | None = None) -> None:
        if rtt_s is not None and config.get("SHED_MODE") == "adaptive":
            self.gradient.observe(rtt_s, self.inflight)
        async with self._cond:
            self.inflight = max(0, self.inflight - 1)
            self._cond.notify()

    def snapshot(self) -> dict:
        return {
            "mode": config.get("SHED_MODE"),
            "inflight": self.inflight,
            "limit": self.limit(),
            "adaptive_limit": self.gradient.current(),
            "rtt_noload_ms": round((self.gradient.rtt_noload or 0.0) * 1000.0, 2),
        }


shedder = Shedder()
