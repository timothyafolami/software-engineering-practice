"""Topic 7's fix kit: deadline, retry-with-budget, latency-tripped breaker.

WHAT THIS DEMONSTRATES: every item in the kit is ~40 lines of ordinary code,
not a language feature. That is the whole argument of the C++ paragraph in
topic 7 -- "the reason your framework has a default is that somebody made a
choice you have not read."

WHAT TO LOOK FOR: none of these three improve throughput. They convert
unbounded latency into fast, honest failure, and `RetryBudget.stats()` /
`LatencyBreaker.stats()` print the numbers that let you tell the difference.
Recognising that as a win is most of what this topic is trying to build.

Everything here is pure asyncio with no third-party dependency, so it is unit
testable natively on macOS with no container.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .errors import DeadlineExceeded, Unavailable

# --- the deadline -----------------------------------------------------------
# Go gets this right structurally: the deadline is a mandatory first parameter,
# so forgetting to propagate it is a visible omission at every call site. Python
# has no such forcing function, so this is the closest approximation -- a
# context variable, plus the discipline of calling `remaining()` before every
# outbound call. A lint rule is the other half and it is not in this file.

_deadline_at: ContextVar[float | None] = ContextVar("deadline_at", default=None)


def remaining() -> float | None:
    """Seconds left in this request's budget, or None if no deadline is set."""
    at = _deadline_at.get()
    return None if at is None else at - time.monotonic()


@asynccontextmanager
async def deadline(budget_s: float | None):
    """Set one budget for this request and enforce it.

    A budget that is *set* but never *checked* is decoration. `asyncio.timeout`
    does the enforcing; the context variable is what lets code three calls down
    ask how much is left instead of starting work it cannot finish.
    """
    if budget_s is None:
        yield
        return
    token = _deadline_at.set(time.monotonic() + budget_s)
    try:
        async with asyncio.timeout(budget_s):
            yield
    except TimeoutError as exc:
        raise DeadlineExceeded(f"request exceeded its {budget_s * 1000:.0f}ms budget") from exc
    finally:
        _deadline_at.reset(token)


# --- retry with a budget ----------------------------------------------------


@dataclass
class RetryBudget:
    """Cap retries as a *fraction of total requests*, not as a per-call count.

    `@retry(3)` is a load multiplier: at the moment the dependency is slowest,
    every caller triples its offered rate and the incident sustains itself. A
    budget makes retries impossible to turn into the load, because the ratio is
    checked before each one.

    `pct=0` disables retrying entirely, which is the lab default.
    """

    pct: float
    window_s: float = 10.0
    _requests: deque = None  # type: ignore[assignment]
    _retries: deque = None  # type: ignore[assignment]

    def __post_init__(self):
        self._requests = deque()
        self._retries = deque()

    def _trim(self, now: float) -> None:
        for q in (self._requests, self._retries):
            while q and q[0] < now - self.window_s:
                q.popleft()

    def record_request(self) -> None:
        now = time.monotonic()
        self._trim(now)
        self._requests.append(now)

    def try_spend(self) -> bool:
        """True if one retry fits inside the budget; records it if so."""
        if self.pct <= 0:
            return False
        now = time.monotonic()
        self._trim(now)
        allowed = len(self._requests) * (self.pct / 100.0)
        if len(self._retries) + 1 > allowed:
            return False
        self._retries.append(now)
        return True

    def stats(self) -> dict:
        self._trim(time.monotonic())
        reqs = len(self._requests)
        return {
            "requests": reqs,
            "retries": len(self._retries),
            # This is the number topic 7 asks you to verify. If it is not
            # pinned near `pct`, the budget is not engaged and the ladder will
            # improve monotonically -- which means you have not reproduced the
            # incident.
            "retry_fraction_pct": round(100.0 * len(self._retries) / reqs, 2) if reqs else 0.0,
        }


def full_jitter(attempt: int, base_s: float = 0.05, cap_s: float = 2.0) -> float:
    """Full jitter, not exponential-with-a-fixed-base.

    Exponential backoff with no jitter keeps every retrying client in lockstep,
    so the herd arrives together at 2x, 4x, 8x. `random.uniform(0, backoff)`
    spreads them, and it is strictly better than "equal jitter" for this shape
    -- see the AWS Architecture Blog's backoff-and-jitter post.
    """
    return random.uniform(0, min(cap_s, base_s * (2 ** attempt)))


async def with_retries(fn, *, attempts: int, budget: RetryBudget):
    """Call `fn`, retrying transient failures within budget and deadline."""
    budget.record_request()
    last: Exception | None = None
    for attempt in range(attempts + 1):
        try:
            return await fn()
        except Unavailable as exc:
            last = exc
            if attempt >= attempts or not budget.try_spend():
                raise
            delay = full_jitter(attempt)
            left = remaining()
            if left is not None and left <= delay:
                # Sleeping past the deadline to retry work we cannot deliver is
                # pure waste offered to a dependency that is already struggling.
                raise DeadlineExceeded("no budget left to retry") from exc
            await asyncio.sleep(delay)
    raise last  # unreachable; kept so the type checker and the reader agree


# --- the latency-tripped circuit breaker ------------------------------------


class LatencyBreaker:
    """Trip on *latency*, not only on errors.

    Most breakers count failures, so during exactly this incident -- where every
    call succeeds, slowly -- they never trip. Tripping on a rolling p95 of call
    duration is the version that fires. It introduces a new failure mode of its
    own (a breaker that opens on a latency blip nothing was wrong with), which
    is why `min_samples` and the half-open probe exist.
    """

    def __init__(self, threshold_ms: int | None, *, window: int = 50, min_samples: int = 20,
                 open_for_s: float = 5.0):
        self.threshold_ms = threshold_ms
        self.window = window
        self.min_samples = min_samples
        self.open_for_s = open_for_s
        self._samples: deque[float] = deque(maxlen=window)
        self._opened_at: float | None = None
        self._trips = 0

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.open_for_s:
            self._opened_at = None       # half-open: let exactly one call through
            self._samples.clear()
            return False
        return True

    def observe(self, duration_s: float) -> None:
        if self.threshold_ms is None:
            return
        self._samples.append(duration_s * 1000.0)
        if len(self._samples) < self.min_samples or self._opened_at is not None:
            return
        ordered = sorted(self._samples)
        p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
        if p95 > self.threshold_ms:
            self._opened_at = time.monotonic()
            self._trips += 1

    @asynccontextmanager
    async def guard(self):
        if self.is_open:
            raise Unavailable("circuit open on latency", retry_after=self.open_for_s)
        started = time.monotonic()
        try:
            yield
        finally:
            self.observe(time.monotonic() - started)

    def stats(self) -> dict:
        return {"open": self._opened_at is not None, "trips": self._trips,
                "samples": len(self._samples)}
