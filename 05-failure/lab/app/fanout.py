"""
Layer 5 lab - fan-out, tail amplification and hedging (topic 6).

WHAT THIS DEMONSTRATES
  If a request must wait for K backends and each has an independent
  probability p of being slow, the probability that at least one is slow is
  1 - (1-p)^K. At p = 1% that is 1% at K=1, 9.6% at K=10, 39% at K=50.
  Your p99 becomes your median. Nothing got slower - you just added
  dependencies, and the tail is where fan-out lives.

  The two distributions behave differently on purpose:

    lognormal  a continuous heavy tail. Hedging helps a lot, because a slow
               draw is a slow DRAW, and the second copy gets a fresh one.
    bimodal    1% of requests take TAIL_RATIO x the median because
               something about THEM is slow. Hedging still helps, but less,
               and if the slowness is a property of the request rather than
               of the attempt, it does not help at all - it just doubles
               the load. Which of your two tails you have decides whether
               hedging is a fix or a self-inflicted 2x.

WHAT TO LOOK FOR
  e2e p99 against K, next to the predicted 1 - (1-p)^K column. And with
  HEDGE=on, `hedges` and the backends' own received rate: hedging is not
  free and the point of capping it at HEDGE_BUDGET_PCT is to know exactly
  what it cost. A 5% budget buys most of the benefit for 5% more load; an
  uncapped hedge is a retry storm with a nicer name.
"""
from __future__ import annotations

import asyncio
import math
import random
import socket
import threading
import time

from .config import config
from .metrics import counters


def service_seconds() -> float:
    """One backend's service time, drawn from the configured distribution."""
    dist = config.get("LATENCY_DIST")
    p50 = float(config.get("LATENCY_P50_MS")) / 1000.0
    ratio = float(config.get("LATENCY_TAIL_RATIO"))
    if dist == "fixed":
        return p50
    if dist == "lognormal":
        # median = exp(mu) = p50; p99 = exp(mu + 2.326*sigma) = ratio * p50
        sigma = math.log(ratio) / 2.3263478740408408
        return random.lognormvariate(math.log(p50), sigma)
    # bimodal: a small fraction of requests are in a slow mode
    slow_pct = float(config.get("BIMODAL_SLOW_PCT"))
    if random.random() * 100.0 < slow_pct:
        return p50 * ratio
    return p50


class HedgeBudget:
    """Token bucket capping hedges at HEDGE_BUDGET_PCT of requests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.tokens = 0.0
        self.capacity = 50.0

    def deposit(self) -> None:
        pct = float(config.get("HEDGE_BUDGET_PCT")) / 100.0
        with self._lock:
            self.tokens = min(self.capacity, self.tokens + pct)

    def withdraw(self) -> bool:
        with self._lock:
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
        return False

    def level(self) -> float:
        with self._lock:
            return round(self.tokens, 2)


hedge_budget = HedgeBudget()


class BackendLatency:
    """A rolling window of observed backend latencies, for the hedge threshold.

    Hedging at "the measured p95" has to mean measured, not guessed - the
    experiment says to hedge at the backend's own p95 and then report what
    that cost. A fixed HEDGE_AFTER_MS is available for when you want to
    hold the threshold still and vary something else.
    """

    def __init__(self, size: int = 2000) -> None:
        self._lock = threading.Lock()
        self._samples: list[float] = []
        self._size = size

    def observe(self, seconds: float) -> None:
        with self._lock:
            self._samples.append(seconds)
            if len(self._samples) > self._size:
                del self._samples[: len(self._samples) - self._size]

    def quantile(self, q: float) -> float | None:
        with self._lock:
            if len(self._samples) < 50:
                return None
            ordered = sorted(self._samples)
        idx = min(len(ordered) - 1, int(q * len(ordered)))
        return ordered[idx]


backend_latency = BackendLatency()


def hedge_after_seconds() -> float | None:
    explicit = config.get("HEDGE_AFTER_MS")
    if explicit is not None:
        return float(explicit) / 1000.0
    return backend_latency.quantile(0.95)


_resolved: tuple[float, list[str]] = (0.0, [])


def backend_addresses(k: int) -> list[str]:
    """Up to k distinct backend addresses, from Docker's DNS round-robin record.

    `docker compose up --scale backend=K` gives the name `backend` one A
    record per replica, so resolving it once yields every replica. When
    fewer replicas are running than K asks for, addresses repeat - which is
    honest (the fan-out really is to K logical backends) but means the
    per-backend load is higher than production's would be. Scale to K if
    you care about that; the tail arithmetic is unaffected either way.
    """
    global _resolved
    host = config.get("BACKEND_HOST")
    port = int(config.get("BACKEND_PORT"))
    age, cached = _resolved
    if time.time() - age > 10.0 or not cached:
        try:
            infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
            cached = sorted({info[4][0] for info in infos})
        except OSError:
            cached = []
        _resolved = (time.time(), cached)
    if not cached:
        return [f"{host}:{port}"] * k
    return [f"{cached[i % len(cached)]}:{port}" for i in range(k)]


async def hedged_call(call, address: str, timeout_ms: float) -> tuple[dict, bool]:
    """Issue one call; if it is still running at the hedge threshold, issue a second.

    Take whichever answers first and CANCEL the other. The cancel matters:
    an un-cancelled hedge is two requests' worth of work for one request's
    worth of answer, forever, which is how a 5% hedge budget quietly becomes
    a 100% load increase.
    """
    threshold = hedge_after_seconds()
    first = asyncio.create_task(call(address, timeout_ms))
    if threshold is None or not config.get("HEDGE"):
        return await first, False
    done, _pending = await asyncio.wait({first}, timeout=threshold)
    if first in done:
        hedge_budget.deposit()
        return first.result(), False
    if not hedge_budget.withdraw():
        hedge_budget.deposit()
        return await first, False
    counters.inc("hedges")
    second = asyncio.create_task(call(address, timeout_ms))
    done, pending = await asyncio.wait({first, second}, return_when=asyncio.FIRST_COMPLETED)
    winner = next(iter(done))
    for task in pending:
        task.cancel()
    if winner is second:
        counters.inc("hedge_wins")
    hedge_budget.deposit()
    return winner.result(), True
