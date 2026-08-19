"""
An open-loop, constant-arrival-rate load driver -- the stdlib stand-in for
k6's `constant-arrival-rate` executor.

Why not just spin up N threads in a loop and hammer? Because that is a
CLOSED loop: each virtual user waits for its own response before sending
the next request, so when the server slows down the load generator quietly
slows down with it. The queue never builds, and the latency you measure is
the latency of a system that was never actually overloaded. That is
coordinated omission, and it is the single easiest way to measure a
throttled container and conclude it is healthy.

Here, arrival times are fixed in advance from a clock, requests are sent
whether or not earlier ones have finished, and latency is measured from the
time the request was *due* -- not from the time a worker got around to it.
Queue wait is part of the number, which is the whole point.

`dropped` is this module's `dropped_iterations`: if it is large the driver
itself could not keep up, and every latency below it is suspect.

RUN
    python3 openloop.py                      # drives $TARGET, or a local demo
    python3 openloop.py http://localhost:8000/db
    RATE=120 WORKERS=8 DURATION=10 python3 openloop.py http://...

  With a URL it drives that endpoint over HTTP. With none, and no reachable
  $TARGET, it drives a synthetic handler instead and says so -- so the shape
  of the output is the same either way and nothing here silently measures
  nothing.
"""

from __future__ import annotations

import queue
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class LoadResult:
    latencies_ms: list[float] = field(default_factory=list)
    scheduled: int = 0
    completed: int = 0
    dropped: int = 0
    wall_s: float = 0.0

    def pct(self, p: float) -> float:
        if not self.latencies_ms:
            return float("nan")
        ordered = sorted(self.latencies_ms)
        rank = min(len(ordered) - 1, int(round(p / 100.0 * (len(ordered) - 1))))
        return ordered[rank]

    @property
    def p50(self) -> float:
        return self.pct(50)

    @property
    def p99(self) -> float:
        return self.pct(99)

    @property
    def throughput(self) -> float:
        return self.completed / self.wall_s if self.wall_s else 0.0

    @property
    def mean(self) -> float:
        return statistics.fmean(self.latencies_ms) if self.latencies_ms else float("nan")


def constant_arrival_rate(
    handler: Callable[[], None],
    workers: int,
    rate_per_sec: float,
    duration_s: float,
    warmup_s: float = 0.0,
    backlog: int = 20_000,
) -> LoadResult:
    """Fire `rate_per_sec` requests a second at a pool of `workers` threads.

    `workers` stands in for uvicorn worker processes / anyio thread-pool
    tokens: the number of things that can be in service at once. Requests
    arriving with all workers busy sit in a queue, and their wait is
    counted.
    """
    inbox: queue.Queue = queue.Queue(maxsize=backlog)
    result = LoadResult()
    lock = threading.Lock()

    def worker() -> None:
        while True:
            item = inbox.get()
            if item is None:
                return
            due, counts = item
            handler()
            latency_ms = (time.perf_counter() - due) * 1000.0
            if counts:
                with lock:
                    result.latencies_ms.append(latency_ms)
                    result.completed += 1

    pool = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for thread in pool:
        thread.start()

    interval = 1.0 / rate_per_sec
    start = time.perf_counter()
    measured_from = start + warmup_s
    end = measured_from + duration_s
    index = 0
    while True:
        due = start + index * interval
        if due >= end:
            break
        index += 1
        now = time.perf_counter()
        if due > now:
            time.sleep(due - now)
        counts = due >= measured_from
        if counts:
            result.scheduled += 1
        try:
            inbox.put_nowait((due, counts))
        except queue.Full:
            if counts:
                result.dropped += 1

    result.wall_s = duration_s
    # Drain: let in-flight work finish so the tail is not truncated away.
    deadline = time.perf_counter() + 10.0
    while not inbox.empty() and time.perf_counter() < deadline:
        time.sleep(0.01)
    for _ in pool:
        inbox.put(None)
    for thread in pool:
        thread.join(timeout=2.0)
    return result


def table(rows: list[list[str]], headers: list[str]) -> str:
    """Fixed-width table. Deliberately not a dependency."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    rule = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = [
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows
    ]
    return "\n".join([line, rule, *body])


# -- running it directly ---------------------------------------------------
#
# 7.5's fallback advice names this file as a command, so it has to behave
# like one. Without a __main__ it printed nothing at all, which is the worst
# possible answer: indistinguishable from a load run that measured zero.


def _main(argv: list[str]) -> int:
    import os
    import sys
    import urllib.error
    import urllib.request

    rate = float(os.environ.get("RATE", "40"))
    workers = int(os.environ.get("WORKERS", "4"))
    duration = float(os.environ.get("DURATION", "10"))
    url = argv[1] if len(argv) > 1 else os.environ.get("TARGET")

    errors = {"count": 0}

    def http_handler() -> None:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                response.read()
        except (urllib.error.URLError, OSError, TimeoutError):
            errors["count"] += 1

    def demo_handler() -> None:
        # ~20ms of wait, the shape of a database round trip. Not CPU work:
        # this path exists to show the DRIVER working, not to benchmark the
        # machine.
        time.sleep(0.020)

    if url:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                response.read()
            handler, target_desc = http_handler, url
        except Exception as exc:  # noqa: BLE001 -- any failure means "not there"
            print(f"  {url} is not reachable ({type(exc).__name__}), so there is")
            print("  nothing to drive. Start the service, or run with no URL for")
            print("  the synthetic demo.")
            return 1
    else:
        handler, target_desc = demo_handler, "a synthetic 20ms handler (NO SERVER)"
        print("  !! No URL given and $TARGET is unset, so this is driving a")
        print("  !! synthetic handler. It demonstrates the DRIVER -- open-loop")
        print("  !! arrivals, queue wait counted, dropped reported -- and measures")
        print("  !! nothing about any service.")
        print()

    print("open-loop constant-arrival-rate driver")
    print(f"  target    : {target_desc}")
    print(f"  rate      : {rate:.0f} req/s   workers: {workers}   duration: {duration:.0f}s")
    print()

    result = constant_arrival_rate(handler, workers, rate, duration)

    rows = [[
        f"{result.scheduled}",
        f"{result.completed}",
        f"{result.dropped}",
        f"{result.throughput:.1f}",
        f"{result.p50:.0f}",
        f"{result.p99:.0f}",
        f"{result.mean:.0f}",
    ]]
    print(table(rows, ["scheduled", "completed", "dropped", "req/s",
                       "p50 ms", "p99 ms", "mean ms"]))
    print()
    if errors["count"]:
        print(f"  {errors['count']} request(s) failed. Latency above includes them.")
    if result.dropped:
        print("  dropped > 0: the DRIVER could not keep up, so every latency above")
        print("  is understated. Raise `workers` before believing any of it -- this")
        print("  is the same warning k6 gives you as `dropped_iterations`.")
    else:
        print("  dropped = 0: the driver kept up, so the queue wait in the numbers")
        print("  above belongs to the system under test rather than to this script.")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
