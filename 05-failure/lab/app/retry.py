"""
Layer 5 lab - retries, backoff, jitter and a token-bucket budget (topic 3).

WHAT THIS DEMONSTRATES
  Retries at every hop multiply. Three hops each retrying 3 times is 27
  requests at the leaf for one request at the edge, and the leaf is the
  thing that was already struggling. The four variants the topic sweeps
  are all config, not code:

    naive      RETRY_ATTEMPTS=3  RETRY_JITTER=none  RETRY_BUDGET_PCT=0
    jitter     RETRY_ATTEMPTS=3  RETRY_JITTER=full  RETRY_BUDGET_PCT=0
    budget     RETRY_ATTEMPTS=3  RETRY_JITTER=full  RETRY_BUDGET_PCT=10
    edge_only  RETRY_ATTEMPTS=1 everywhere except the hop next to the
               database, which keeps 3

WHAT TO LOOK FOR
  `retries` and `retry_denied` in /admin/counters. Amplification is the
  leaf's `received` divided by the generator's offered rate, and the
  number that matters is not its peak during the fault but its value two
  minutes after the fault is gone.

WHY A BUDGET RATHER THAN A CAP
  A cap of 3 is a multiplier: it scales with how much trouble you are in,
  which is precisely backwards. A budget is a token bucket refilled by
  SUCCESSES, so when everything is failing there is nothing refilling it
  and retries stop by themselves. RETRY_BUDGET_PCT=10 means "retries may
  add at most 10% to the load I am already generating".
"""
from __future__ import annotations

import asyncio
import random
import threading
import time
from typing import Awaitable, Callable, TypeVar

from .config import config
from .metrics import counters

T = TypeVar("T")


class RetryBudget:
    """Token bucket: successes deposit, retries withdraw.

    ratio = RETRY_BUDGET_PCT / 100. Each completed request deposits `ratio`
    tokens; each retry costs one. The bucket is capped so a long quiet
    period cannot bank enough tokens to fund a storm later.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.tokens = 0.0
        self.capacity = 100.0

    def deposit(self) -> None:
        ratio = float(config.get("RETRY_BUDGET_PCT")) / 100.0
        if ratio <= 0:
            return
        with self._lock:
            self.tokens = min(self.capacity, self.tokens + ratio)

    def withdraw(self) -> bool:
        """True if a retry is affordable. Always true when the budget is off."""
        if float(config.get("RETRY_BUDGET_PCT")) <= 0:
            return True
        with self._lock:
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
        counters.inc("retry_denied")
        return False

    def level(self) -> float:
        with self._lock:
            return round(self.tokens, 2)


budget = RetryBudget()


def backoff_ms(attempt: int) -> float:
    """Exponential backoff for attempt n (1-based), with optional full jitter.

    RETRY_JITTER=none is the version everyone writes first, and it is why
    retries arrive in a synchronised wave: every client that failed at the
    same instant retries at the same instant. `full` spreads the same mean
    delay uniformly over [0, backoff], which is the whole fix.
    """
    base = float(config.get("RETRY_BASE_MS")) * (2 ** max(0, attempt - 1))
    if config.get("RETRY_JITTER") == "full":
        return random.uniform(0.0, base)
    return base


async def with_retries(
    attempt_fn: Callable[[int, float], Awaitable[T]],
    *,
    is_retryable: Callable[[T], bool],
    deadline_ms: float | None = None,
) -> T:
    """Run attempt_fn until it succeeds, runs out of attempts, or runs out of time.

    attempt_fn receives (attempt_number, per_attempt_timeout_ms) and must
    not raise for a normal failure - it returns a result that is_retryable
    can classify. Attempts past the first are counted, charged to the
    budget, and abandoned if the caller's deadline has already passed:
    retrying into an expired deadline is the purest form of zombie work.
    """
    attempts = max(1, int(config.get("RETRY_ATTEMPTS")))
    from .deadline import outbound_timeout_ms, remaining_ms  # local: avoids a cycle

    result: T | None = None
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            if not budget.withdraw():
                break
            counters.inc("retries")
        timeout = float(outbound_timeout_ms(deadline_ms))
        result = await attempt_fn(attempt, timeout)
        if not is_retryable(result):
            budget.deposit()
            return result
        if attempt == attempts:
            break
        delay = backoff_ms(attempt) / 1000.0
        left = remaining_ms(deadline_ms)
        if left is not None and config.get("PROPAGATE_DEADLINE") and left <= delay * 1000.0:
            break
        await asyncio.sleep(delay)
    assert result is not None  # attempts >= 1, so the loop ran at least once
    return result


def sleep_deadline(seconds: float) -> float:
    """Absolute monotonic deadline, for callers that want one."""
    return time.monotonic() + seconds
