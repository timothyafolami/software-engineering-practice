"""
Layer 5 lab - locally injected faults (POST /admin/fault).

WHAT THIS IS, AND WHAT TOXIPROXY IS
  Two different layers, and using the wrong one produces a demonstration
  of the wrong thing.

  toxiproxy breaks the NETWORK to a dependency: latency, jitter, bandwidth,
  resets, on the connection between this service and Postgres or Redis.
  Nothing in the application knows, which is the point - "make the
  dependency slow, not absent" has to be done without touching application
  code or you have proved only that your mock works.

  This module breaks the SERVICE: a percentage of requests return 500, or
  every request gains N milliseconds before it starts. Use it where the
  failure genuinely belongs to the service - the leaf's own error rate in
  topic 3, the destroyed response path in topic 7, where the work COMPLETED
  and only the answer was lost.

FIELDS
  error_pct   0-100, chance this request returns 503 instead of running
  latency_ms  added before the handler starts
  drop_pct    0-100, chance the response is destroyed AFTER the work is
              done - the ambiguous case topic 7 is about
"""
from __future__ import annotations

import asyncio
import random
import threading


class Faults:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.error_pct = 0.0
        self.latency_ms = 0.0
        self.drop_pct = 0.0

    def apply(self, patch: dict) -> dict:
        with self._lock:
            if "error_pct" in patch:
                self.error_pct = max(0.0, min(100.0, float(patch["error_pct"])))
            if "latency_ms" in patch:
                self.latency_ms = max(0.0, float(patch["latency_ms"]))
            if "drop_pct" in patch:
                self.drop_pct = max(0.0, min(100.0, float(patch["drop_pct"])))
            return self.snapshot_locked()

    def snapshot_locked(self) -> dict:
        return {"error_pct": self.error_pct, "latency_ms": self.latency_ms,
                "drop_pct": self.drop_pct}

    def snapshot(self) -> dict:
        with self._lock:
            return self.snapshot_locked()

    def should_error(self) -> bool:
        with self._lock:
            pct = self.error_pct
        return pct > 0 and random.random() * 100.0 < pct

    def should_drop(self) -> bool:
        with self._lock:
            pct = self.drop_pct
        return pct > 0 and random.random() * 100.0 < pct

    async def delay(self) -> None:
        with self._lock:
            ms = self.latency_ms
        if ms > 0:
            await asyncio.sleep(ms / 1000.0)


faults = Faults()
