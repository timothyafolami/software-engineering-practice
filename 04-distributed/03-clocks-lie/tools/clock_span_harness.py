"""
Layer 4 Topic 3 (Part C) -- the instrumentation half: a p99 poisoned by a clock.

WHAT THIS DEMONSTRATES: a span-timing harness that records the SAME operation
twice, once with the wall clock and once with a monotonic clock, while a
background thread steps the application's perceived wall clock mid-run -- exactly
what an NTP correction does to a running process. Then it compares the two p99s
and counts negative samples.

This is the direct, practical version of the topic: before you chase a latency
spike in application code, rule out that you measured it wrong.

WHAT TO LOOK FOR IN THE OUTPUT:
  * NEGATIVE SAMPLES on the wall-clock row. A span that finished before it
    started is not a slow request; it is a broken measurement.
  * the p99 ratio between the two rows. Identical work, identical machine,
    identical instant -- the only difference is which clock the harness read.
  * the "landed inside a span" check. If the steps missed every span, the run
    proves nothing, and the harness says so rather than printing a clean table.
    That check is the README's "what would mean the experiment is broken" note,
    enforced in code instead of trusted to the reader.

  python3 tools/clock_span_harness.py --step-ms 40000
  python3 tools/clock_span_harness.py --step-ms 250 --steps 20 --workers 4
"""
from __future__ import annotations

import argparse
import statistics
import threading
import time


class AppClock:
    """The application's own now(). Every service has one; most read wall time.

    The offset is how this harness models an NTP step. It does not touch the
    system clock: lab/README.md explains that CLOCK_REALTIME is not namespaced
    and Docker Desktop is one VM, so per-container skew is not achievable at all
    on this machine -- and an application-level offset makes the independent
    variable explicit rather than magic.
    """

    def __init__(self) -> None:
        self._offset = 0.0
        self._lock = threading.Lock()
        self.steps_applied = 0

    def now(self) -> float:
        with self._lock:
            return time.time() + self._offset

    def step(self, seconds: float) -> None:
        with self._lock:
            self._offset += seconds
            self.steps_applied += 1


class Span:
    __slots__ = ("wall_ms", "mono_ms", "spanned_a_step")

    def __init__(self, wall_ms: float, mono_ms: float, spanned_a_step: bool) -> None:
        self.wall_ms = wall_ms
        self.mono_ms = mono_ms
        self.spanned_a_step = spanned_a_step


def work(seconds: float) -> None:
    """The operation being timed. Busy, not sleeping, so it costs real time."""
    end = time.perf_counter() + seconds
    total = 0
    while time.perf_counter() < end:
        total += 1
    return None if total else None


def stepper(clock: AppClock, step_seconds: float, count: int, interval: float,
            stop: threading.Event) -> None:
    """Steps the app clock back and forth, from a real background thread.

    Alternating direction on purpose. A backwards step produces a negative
    sample; a forwards step produces an enormous positive one. The second is the
    one that actually shows up on dashboards, because a negative duration is
    often dropped by the metrics client before you ever see it -- and then the
    only survivor is the fake spike.
    """
    for i in range(count):
        if stop.wait(interval):
            return
        clock.step(step_seconds if i % 2 == 0 else -step_seconds)


def run_worker(clock: AppClock, spans: list[Span], n: int, op_seconds: float,
               lock: threading.Lock) -> None:
    local: list[Span] = []
    for _ in range(n):
        before_steps = clock.steps_applied
        w0, m0 = clock.now(), time.perf_counter()
        work(op_seconds)
        w1, m1 = clock.now(), time.perf_counter()
        local.append(Span((w1 - w0) * 1000.0, (m1 - m0) * 1000.0,
                          clock.steps_applied != before_steps))
    with lock:
        spans.extend(local)


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    i = min(len(s) - 1, max(0, round(q * len(s) + 0.5) - 1))
    return s[i]


def row(name: str, values: list[float]) -> str:
    neg = sum(1 for v in values if v < 0)
    return (f"  {name:<26}{pct(values, 0.50):10.3f}{pct(values, 0.99):12.3f}"
            f"{max(values):14.1f}{min(values):14.1f}{neg:11d}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--step-ms", type=float, default=40000,
                    help="size of each clock step, in ms (default 40000 = 40s)")
    ap.add_argument("--steps", type=int, default=6, help="how many steps to apply")
    ap.add_argument("--spans", type=int, default=2000, help="spans per worker")
    ap.add_argument("--workers", type=int, default=2, help="concurrent span producers")
    ap.add_argument("--op-ms", type=float, default=1.0,
                    help="duration of the operation being timed, in ms")
    ap.add_argument("--window", type=int, default=100,
                    help="spans per aggregation window, modelling a dashboard's "
                         "scrape interval (default 100)")
    args = ap.parse_args(argv)

    clock = AppClock()
    spans: list[Span] = []
    lock = threading.Lock()
    stop = threading.Event()

    total_spans = args.spans * args.workers
    est_seconds = args.spans * args.op_ms / 1000.0
    interval = max(est_seconds / (args.steps + 1), 0.001)

    print("=" * 78)
    print("Layer 4 Topic 3 (Part C) -- span timing under a stepping clock")
    print("=" * 78)
    print(f"  spans           {total_spans}  ({args.workers} workers x {args.spans})")
    print(f"  operation       {args.op_ms:.3f} ms of real work per span")
    print(f"  clock steps     {args.steps} x {args.step_ms:.0f} ms, alternating direction,")
    print(f"                  one every ~{interval * 1000:.0f} ms from a background thread")
    print(f"  system clock    NOT touched -- the offset lives in the app's now()")

    t = threading.Thread(target=stepper, daemon=True,
                         args=(clock, args.step_ms / 1000.0, args.steps, interval, stop))
    workers = [
        threading.Thread(target=run_worker, daemon=True,
                         args=(clock, spans, args.spans, args.op_ms / 1000.0, lock))
        for _ in range(args.workers)
    ]
    started = time.perf_counter()
    t.start()
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    stop.set()
    t.join(timeout=1.0)
    elapsed = time.perf_counter() - started

    wall = [s.wall_ms for s in spans]
    mono = [s.mono_ms for s in spans]
    hit = sum(1 for s in spans if s.spanned_a_step)

    print()
    print("-" * 78)
    print(f"the same {len(spans)} spans, measured two ways")
    print("-" * 78)
    print(f"  {'clock':<26}{'p50':>10}{'p99':>12}{'max':>14}{'min':>14}{'negative':>11}")
    print(row("wall clock", wall))
    print(row("monotonic clock", mono))
    print("  (milliseconds; 'negative' counts spans that finished before they started)")

    # A dashboard does not compute one percentile over your whole run; it
    # computes one per scrape interval. That distinction is the entire reason a
    # handful of poisoned samples matters, so model it rather than assert it.
    print()
    print("-" * 78)
    print(f"per-window p99, {args.window} spans per window "
          f"(a scrape interval, not the whole run)")
    print("-" * 78)
    windows_wall = [wall[i:i + args.window] for i in range(0, len(wall), args.window)]
    windows_mono = [mono[i:i + args.window] for i in range(0, len(mono), args.window)]
    p99_wall = [pct(w, 0.99) for w in windows_wall if w]
    p99_mono = [pct(w, 0.99) for w in windows_mono if w]
    poisoned = [i for i, w in enumerate(windows_wall) if pct(w, 0.99) > 10 * pct(p99_mono, 0.50)]
    print(f"  windows                    {len(p99_wall)}")
    print(f"  wall      p99 across windows   median {statistics.median(p99_wall):9.3f} ms"
          f"   worst {max(p99_wall):12.1f} ms")
    print(f"  monotonic p99 across windows   median {statistics.median(p99_mono):9.3f} ms"
          f"   worst {max(p99_mono):12.1f} ms")
    print(f"  windows whose wall p99 is >10x the monotonic median: {len(poisoned)}"
          f" of {len(p99_wall)}")

    print()
    print("-" * 78)
    print("harness self-check")
    print("-" * 78)
    print(f"  steps applied              {clock.steps_applied} of {args.steps} requested")
    print(f"  spans containing a step    {hit}  "
          f"({args.workers} workers each observe every step)")
    print(f"  wall-clock negatives       {sum(1 for v in wall if v < 0)}")
    print(f"  monotonic negatives        {sum(1 for v in mono if v < 0)}")
    print(f"  wall mean                  {statistics.fmean(wall):+.3f} ms")
    print(f"  monotonic mean             {statistics.fmean(mono):+.3f} ms")
    print(f"  wall clock elapsed         {elapsed:.2f}s")

    if hit == 0:
        print()
        print("  *** BROKEN RUN, not a wrong prediction. ***")
        print("  No step landed inside a span, so nothing was measured. The README lists")
        print("  this exact outcome: lengthen the spans (--op-ms) or step more often")
        print("  (--steps), and confirm the offset applies to the clock the harness reads.")
        return 1

    print()
    print("  Read the two tables together. Over the whole run the poisoned samples are")
    print(f"  {hit} out of {len(spans)} and cannot move a p99 by rank -- which is why this")
    print("  bug survives so long. Per window they are a different story, and a window")
    print("  is what your dashboard actually plots.")
    print()
    print("  The wall mean above is the other half. The steps alternate direction, so")
    print("  the mean is not merely wrong by some amount, it is not a duration at all --")
    print("  a metric that can go negative is not measuring elapsed time. If it is")
    print("  positive on your run, that is arithmetic luck, not a working measurement.")
    print()
    print("  The monotonic row ran the identical workload, on the identical machine,")
    print("  in the identical instant, and never noticed. If your p99 dashboard spikes")
    print("  at the same time every week, this is the first thing to rule out, and it")
    print("  costs one grep for time.time() on your span-timing path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
