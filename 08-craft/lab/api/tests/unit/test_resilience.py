"""Topic 7's fix kit, unit tested. No container, no database, no k6.

WHAT THIS DEMONSTRATES: each fix is ordinary code with ordinary tests. The
budget test is the one that matters -- topic 7's broken-experiment note says
"adding retries improves things monotonically at every step" means the budget is
not engaged, and this is how you check that before spending an hour on a ladder.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.errors import DeadlineExceeded, Unavailable
from app.core.resilience import LatencyBreaker, RetryBudget, deadline, full_jitter, with_retries


async def test_retry_budget_caps_retries_at_the_configured_fraction():
    budget = RetryBudget(pct=10)

    async def always_unavailable():
        raise Unavailable("db slow")

    for _ in range(100):
        with pytest.raises(Unavailable):
            await with_retries(always_unavailable, attempts=3, budget=budget)

    stats = budget.stats()
    assert stats["requests"] == 100
    # Without a budget, attempts=3 would have produced 300 retries -- a 4x load
    # multiplier applied at the exact moment the dependency is struggling.
    assert stats["retries"] <= 10
    assert stats["retry_fraction_pct"] <= 10.0


async def test_zero_budget_disables_retrying_entirely():
    budget = RetryBudget(pct=0)
    calls = {"n": 0}

    async def counted():
        calls["n"] += 1
        raise Unavailable("db slow")

    with pytest.raises(Unavailable):
        await with_retries(counted, attempts=5, budget=budget)
    assert calls["n"] == 1


async def test_deadline_translates_timeout_into_the_taxonomy():
    """A bare asyncio.TimeoutError escaping to the edge is a 500. It is not a
    bug -- it is a caller-actionable condition wearing the wrong clothes."""
    with pytest.raises(DeadlineExceeded):
        async with deadline(0.02):
            await asyncio.sleep(1)


async def test_deadline_is_a_no_op_when_unset():
    async with deadline(None):
        await asyncio.sleep(0)


async def test_breaker_trips_on_latency_with_zero_errors():
    """The whole point: every call below SUCCEEDS. An error-counting breaker
    never fires during this incident; a latency-tripped one does."""
    breaker = LatencyBreaker(threshold_ms=20, min_samples=5, open_for_s=10)
    for _ in range(5):
        async with breaker.guard():
            await asyncio.sleep(0.03)
    assert breaker.stats()["trips"] == 1
    with pytest.raises(Unavailable):
        async with breaker.guard():
            pass


async def test_breaker_is_a_no_op_when_threshold_unset():
    breaker = LatencyBreaker(threshold_ms=None, min_samples=1)
    for _ in range(10):
        async with breaker.guard():
            await asyncio.sleep(0.01)
    assert breaker.stats()["trips"] == 0


def test_full_jitter_spans_zero_to_the_backoff():
    samples = [full_jitter(3, base_s=0.05, cap_s=2.0) for _ in range(500)]
    assert min(samples) >= 0
    assert max(samples) <= 0.05 * 2 ** 3
    # Exponential-with-a-fixed-base would put every client on the same instant.
    assert len(set(round(s, 6) for s in samples)) > 100
