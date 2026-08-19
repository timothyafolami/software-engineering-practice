"""
Layer 5 lab - counters, gauges and histograms.

WHAT THIS IS
  Two views of the same numbers.

  /metrics    Prometheus text format. Histograms, never pre-computed
              percentiles - you cannot average a percentile, and this layer
              is entirely about the tail, so the buckets have to survive to
              query time.

  /admin/counters  A small JSON object of monotonic counters and gauges.
              The k6 scripts poll this once a second and diff successive
              samples to get rates, which is how amplification, goodput and
              pool utilisation reach the CSV that ../tools/ plots. Polling
              a counter and diffing is deliberate: a rate computed by the
              thing being measured is a rate you have to trust.

WHAT TO LOOK FOR
  `received` counts requests that ARRIVED, not requests that succeeded.
  The ratio of the leaf's `received` to the generator's offered rate is
  topic 3's amplification factor, and it is the only honest way to measure
  it - a counter of successes cannot see a retry storm.

  `zombies` counts work completed after the caller's deadline had already
  passed: topic 2's whole point, and the reason the gateway stamps an
  observational deadline header even in the variant that ignores it.
"""
from __future__ import annotations

import threading
import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# Buckets in seconds, dense where this layer lives (10ms .. 30s).
_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.25, 0.4, 0.6,
            0.8, 1.0, 1.5, 2.5, 4.0, 6.0, 10.0, 20.0, 30.0, float("inf"))

REQUEST_SECONDS = Histogram(
    "lab_request_seconds", "End-to-end handler duration",
    ["role", "endpoint", "outcome"], buckets=_BUCKETS,
)
POOL_WAIT_SECONDS = Histogram(
    "lab_pool_wait_seconds", "Time spent waiting for a pooled connection",
    ["role"], buckets=_BUCKETS,
)
DOWNSTREAM_SECONDS = Histogram(
    "lab_downstream_seconds", "Duration of one outbound attempt to the next hop",
    ["role", "outcome"], buckets=_BUCKETS,
)
INFLIGHT = Gauge("lab_inflight", "Requests currently in the handler", ["role"])
POOL_IN_USE = Gauge("lab_pool_in_use", "Connections checked out of the pool", ["role"])
POOL_TOTAL = Gauge("lab_pool_total", "pool_size + max_overflow", ["role"])
EVENTS = Counter("lab_events_total", "Countable things that happened", ["role", "event"])


class Counters:
    """Monotonic counters plus point-in-time gauges, as plain JSON.

    Everything here is process-local and resets when the process does. That
    is the honest behaviour for the crash test in topic 7, where a restart
    genuinely does lose the in-process view and only Postgres remembers.
    """

    NAMES = (
        "received",          # requests that arrived at this service
        "completed",         # requests that returned 2xx
        "failed",            # requests that returned 5xx (excluding shed)
        "shed",              # requests rejected by the admission controller
        "deadline_rejected",  # rejected before starting: no budget left
        "deadline_abandoned",  # abandoned AFTER the pool wait ate the budget
        "timeouts",          # outbound attempts that hit CLIENT_TIMEOUT_MS
        "retries",           # outbound attempts beyond the first
        "retry_denied",      # retries the budget refused
        "zombies",           # completions finishing after the caller's deadline
        "cache_hits",
        "cache_misses",
        "db_queries",
        "db_errors",
        "hedges",            # topic 6: second copies actually issued
        "hedge_wins",        # ... of which returned first
        "charges",           # topic 7: rows written to `charges`
        "conflicts",         # topic 7: 409s
        "fingerprint_rejects",  # topic 7: 422s
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._c = {name: 0 for name in self.NAMES}
        self._gauges: dict[str, float] = {}
        self.started = time.time()

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._c[name] = self._c.get(name, 0) + n

    def gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def reset(self) -> None:
        """Zero everything. Each run starts from a clean slate, and a rate
        computed across two different runs is not a rate."""
        with self._lock:
            for name in self._c:
                self._c[name] = 0
            self._gauges.clear()
        self.started = time.time()

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            out: dict[str, float] = dict(self._c)
            out.update(self._gauges)
        out["uptime_s"] = round(time.time() - self.started, 3)
        out["now_ms"] = round(time.time() * 1000.0, 1)
        return out


counters = Counters()

__all__ = [
    "CONTENT_TYPE_LATEST", "generate_latest", "counters",
    "REQUEST_SECONDS", "POOL_WAIT_SECONDS", "DOWNSTREAM_SECONDS",
    "INFLIGHT", "POOL_IN_USE", "POOL_TOTAL", "EVENTS",
]
