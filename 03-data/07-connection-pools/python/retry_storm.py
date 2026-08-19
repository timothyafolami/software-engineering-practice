"""
Metastability: the system that does not recover when the spike ends.

    python3 07-connection-pools/python/retry_storm.py

WHAT IT DEMONSTRATES: a load spike above capacity, then offered load DROPPED to
half of capacity -- comfortably serviceable -- and a system that stays broken
anyway, because its own retries are now the load.

The loop, which is the definition of a metastable failure:

    requests time out  ->  clients retry  ->  effective load goes UP  ->
    the queue gets longer  ->  more requests time out  ->  ...

The spike is the trigger. The retries are the sustaining feedback. Remove the
trigger and the feedback keeps the system in the bad state, which is why "it
recovered when we restarted it" is such a common incident note -- the restart
dropped the in-flight retries, and nothing else would have.

TWO RUNS:
  A. retry up to MAX_RETRIES, immediately, no budget -- the shape almost every
     HTTP client library ships with.
  B. the same retries, with FULL JITTER on the backoff and a RETRY BUDGET: at
     most RETRY_BUDGET of total requests may be retries, enforced by a token
     bucket shared across the whole client. Past that, failures fail.

WHAT TO LOOK FOR: the per-second timeline. The vertical bar marks where offered
load drops back to half of capacity. Read what happens AFTER that bar -- that is
the entire experiment. The recovery column says whether goodput came back to the
offered rate within the recovery window.

THE MINIMUM FIX: not exponential backoff. Backoff alone still lets every client
retry, just later and together. The budget is the thing that changes the
system's behaviour, because it bounds retries as a FRACTION of traffic rather
than per request -- so the worse things get, the smaller the share of load that
retries are allowed to be.

WHY THERE IS A statement_timeout HERE, and why the experiment does not work
without one. Retries only sustain an overload if a retried request COSTS the
bottleneck something. A request that gives up waiting for a pool slot never
reached the database, so the server did no work for it, so the retry adds
nothing to the server's load -- and a work-conserving server drains its queue
the instant offered load drops below capacity. Run this file with
STATEMENT_TIMEOUT_MS=0 and you will watch both variants recover every time,
which is a true result about a system that cannot go metastable rather than a
demonstration of one.

What makes real systems metastable is WASTED work: a query the server executes
and then throws away because the client stopped waiting for it. A
statement_timeout below the queued service time is the ordinary production way
that happens, and it is what turns retries into a feedback loop -- every retry
consumes capacity that produces no goodput, which makes the next request slower,
which makes it time out too.

Knobs: POOL_SIZE, SPIKE_MULT, RECOVER_MULT, SPIKE_S, RECOVER_S, MAX_RETRIES,
RETRY_BUDGET, STATEMENT_TIMEOUT_MS (0 disables it).
"""
from __future__ import annotations

import os
import random
import sys
import threading
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pool_lab  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402

POOL_SIZE = int(os.environ.get("POOL_SIZE", "8"))
POOL_TIMEOUT = float(os.environ.get("POOL_TIMEOUT", "0.5"))
SPIKE_MULT = float(os.environ.get("SPIKE_MULT", "3.0"))
RECOVER_MULT = float(os.environ.get("RECOVER_MULT", "0.5"))
SPIKE_S = float(os.environ.get("SPIKE_S", "10"))
RECOVER_S = float(os.environ.get("RECOVER_S", "12"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
RETRY_BUDGET = float(os.environ.get("RETRY_BUDGET", "0.10"))
# 0 = no statement timeout. The default is computed from the measured service
# time in main() so it means the same thing on a laptop and on a server.
STATEMENT_TIMEOUT_MS = os.environ.get("STATEMENT_TIMEOUT_MS")


class RetryBudget:
    """A token bucket over the WHOLE client, not per request.

    Every request contributes `budget` tokens; every retry spends one. When the
    bucket is empty, retries are refused and the failure is returned to the
    caller. Ten lines, and it is the difference between a system that recovers
    and one that does not.
    """

    def __init__(self, ratio: float, enabled: bool):
        self.ratio = ratio
        self.enabled = enabled
        self.tokens = 0.0
        self.refused = 0
        self.lock = threading.Lock()

    def on_request(self) -> None:
        with self.lock:
            self.tokens = min(self.tokens + self.ratio, 100.0)

    def allow_retry(self) -> bool:
        if not self.enabled:
            return True
        with self.lock:
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            self.refused += 1
            return False


class Timeline:
    def __init__(self, t0: float):
        self.t0 = t0
        self.good = defaultdict(int)
        self.failed = defaultdict(int)
        self.attempts = defaultdict(int)
        self.lock = threading.Lock()

    def record(self, ok: bool, attempts: int) -> None:
        second = int(time.perf_counter() - self.t0)
        with self.lock:
            (self.good if ok else self.failed)[second] += 1
            self.attempts[second] += attempts

    def render(self, seconds: int, target: float) -> tuple[str, str]:
        blocks = " .:-=+*#%@"
        good, load = [], []
        for s in range(seconds):
            g = self.good.get(s, 0)
            good.append("_" if g == 0 else blocks[min(9, 1 + int(8 * g / max(target, 1)))])
            a = self.attempts.get(s, 0)
            load.append("_" if a == 0 else blocks[min(9, 1 + int(8 * a / max(target * 2, 1)))])
        return "".join(good), "".join(load)


def make_handler(budget: RetryBudget, timeline: Timeline, jitter: bool):
    def handler(engine, scheduled_at, result) -> None:
        budget.on_request()
        attempts = 0
        for attempt in range(MAX_RETRIES + 1):
            attempts += 1
            try:
                with engine.connect() as conn:
                    pool_lab.do_work(conn)
                result.ok(scheduled_at)
                timeline.record(True, attempts)
                return
            except Exception as exc:  # noqa: BLE001
                kind = pool_lab.classify(exc)
                if attempt == MAX_RETRIES or not budget.allow_retry():
                    result.fail(kind, scheduled_at)
                    timeline.record(False, attempts)
                    return
                if jitter:
                    # Full jitter: sleep a RANDOM amount up to the backoff, not
                    # the backoff itself. Backoff without jitter re-synchronises
                    # every client onto the same instant and rebuilds the spike.
                    time.sleep(random.uniform(0, 0.05 * (2 ** attempt)))
    return handler


def run_variant(label: str, jitter: bool, budget_on: bool, rate_capacity: float,
                statement_timeout_ms: int | None) -> dict:
    engine = pool_lab.make_engine(POOL_SIZE, 0, POOL_TIMEOUT, app_name="sep-retry",
                                  statement_timeout_ms=statement_timeout_ms)
    result = pool_lab.Result()
    budget = RetryBudget(RETRY_BUDGET, budget_on)
    t0 = time.perf_counter()
    timeline = Timeline(t0)
    handler = make_handler(budget, timeline, jitter)

    spike_rate = rate_capacity * SPIKE_MULT
    recover_rate = rate_capacity * RECOVER_MULT
    pool_lab.open_loop(engine, handler, spike_rate, SPIKE_S, result)
    spike_end = int(time.perf_counter() - t0)
    pool_lab.open_loop(engine, handler, recover_rate, RECOVER_S, result)
    elapsed = time.perf_counter() - t0
    engine.dispose()

    # Recovery: goodput in the last third of the recovery window, against the
    # rate actually being offered there.
    tail_from = int(elapsed) - max(2, int(RECOVER_S / 3))
    tail = [timeline.good.get(s, 0) for s in range(tail_from, int(elapsed))]
    tail_goodput = sum(tail) / max(len(tail), 1)
    return {
        "label": label, "timeline": timeline, "seconds": int(elapsed),
        "spike_end": spike_end, "recover_rate": recover_rate,
        "tail_goodput": tail_goodput,
        "recovered": tail_goodput >= recover_rate * 0.85,
        "refused": budget.refused,
        "summary": result.summary(elapsed),
    }


def report(run: dict) -> None:
    good, load = run["timeline"].render(run["seconds"], run["recover_rate"])
    bar = [" "] * run["seconds"]
    if run["spike_end"] < len(bar):
        bar[run["spike_end"]] = "|"
    print(f"\n  {run['label']}")
    print(f"    offered  {load}")
    print(f"    goodput  {good}")
    print(f"    load drop{''.join(bar)}")
    s = run["summary"]
    print(f"    completed {s['completed']:,} of {s['attempts']:,} requests, "
          f"p99 {s['p99']:.0f}ms, errors {s['error_count']:,}")
    print(f"    goodput in the last seconds: {run['tail_goodput']:.0f} req/s "
          f"against {run['recover_rate']:.0f} req/s offered  ->  "
          f"{'RECOVERED' if run['recovered'] else 'STILL DEAD'}")
    if run["refused"]:
        print(f"    retries refused by the budget: {run['refused']:,}")


def calibrate(service_ms: float) -> float:
    """Measure achievable throughput rather than deriving it.

    cores / service_time is the right first estimate and it is optimistic here,
    because the load generator, the driver and Postgres are all on this one
    machine and competing for the same cores. Offering deliberately more load
    than that estimate for a few seconds and recording what actually completes
    gives the real ceiling -- and every "recover to half of capacity" claim in
    this program depends on that number being true rather than assumed.
    """
    cpus = os.cpu_count() or 4
    optimistic = cpus * 1000.0 / service_ms
    engine = pool_lab.make_engine(POOL_SIZE, 0, 5, app_name="sep-calibrate")
    result = pool_lab.Result()
    timeline = Timeline(time.perf_counter())
    handler = make_handler(RetryBudget(0.0, True), timeline, jitter=False)
    elapsed = pool_lab.open_loop(engine, handler, optimistic * 2, 5.0, result)
    engine.dispose()
    achieved = result.summary(elapsed)["completed"] / elapsed
    print(f"  service time {service_ms:.0f}ms on {cpus} cores suggests "
          f"{optimistic:.0f} req/s;")
    print(f"  offering {optimistic * 2:.0f} req/s for 5s actually completed "
          f"{achieved:.0f} req/s -- that is the real ceiling,")
    print("  and everything below is sized off the measured number, not the derived one.")
    return achieved


def main() -> None:
    pool_lab.prepare()
    lab_db.banner("Retry storms and metastability")

    probe = pool_lab.make_engine(1, 0, 10, app_name="sep-probe")
    service_ms = pool_lab.measure_service_time(probe)
    probe.dispose()
    capacity = float(os.environ.get("CAPACITY", "0")) or calibrate(service_ms)
    print(f"  spike: {capacity * SPIKE_MULT:.0f} req/s for {SPIKE_S:.0f}s "
          f"({SPIKE_MULT:.0f}x capacity)")
    print(f"  then:  {capacity * RECOVER_MULT:.0f} req/s for {RECOVER_S:.0f}s "
          f"({RECOVER_MULT:.1f}x capacity -- comfortably serviceable)")
    stmt_timeout = (int(STATEMENT_TIMEOUT_MS) if STATEMENT_TIMEOUT_MS is not None
                    else max(50, int(service_ms * 1.5)))
    stmt_timeout = stmt_timeout or None
    print(f"  pool {POOL_SIZE}, pool_timeout {POOL_TIMEOUT}s, up to {MAX_RETRIES} retries")
    if stmt_timeout:
        print(f"  statement_timeout {stmt_timeout}ms -- 1.5x the uncontended service time, so a")
        print("  query that queues behind others is cancelled AFTER burning server CPU.")
        print("  That wasted work is the amplifier; without it retries cost the server")
        print("  nothing and no amount of them can keep it down (STATEMENT_TIMEOUT_MS=0).")
    else:
        print("  statement_timeout disabled -- expect BOTH variants to recover, because a")
        print("  request that never got a pool slot never cost the server anything.")
    print("\n  Predict before reading: does A recover on its own? How long does B take?")
    print("  ( _ = nothing completed that second; the bar marks the drop in offered load )")

    a = run_variant("A. retry immediately, no budget", jitter=False, budget_on=False,
                    rate_capacity=capacity, statement_timeout_ms=stmt_timeout)
    report(a)
    time.sleep(2)     # let the server settle between runs
    b = run_variant(f"B. full jitter + {RETRY_BUDGET:.0%} retry budget", jitter=True,
                    budget_on=True, rate_capacity=capacity, statement_timeout_ms=stmt_timeout)
    report(b)

    print("\n  Read the section after the bar in each timeline.")
    print(f"  This run: A {'recovered' if a['recovered'] else 'stayed down'}, "
          f"B {'recovered' if b['recovered'] else 'stayed down'}.")
    if not a["recovered"] and b["recovered"]:
        print("  That is the result this experiment is built to produce.")
        print("  A stayed down after offered load fell to half of capacity. Nothing was")
        print("  wrong with the database, the query, or the pool size -- the load that kept")
        print("  it down was the client's own retries of requests that had already failed.")
        print("  B recovered, on the same hardware, with the same pool, at the same offered")
        print("  rate. The only difference is that its retries were capped as a FRACTION of")
        print("  traffic, so they could not grow to replace the spike.")
    elif a["recovered"] and b["recovered"]:
        print("  Both recovered, so this run did not reach the metastable state and proves")
        print("  nothing about retry budgets either way. The system stayed work-conserving:")
        print("  every unit of server capacity produced goodput, so goodput came back the")
        print("  moment offered load fell below capacity. To push it over the edge, make the")
        print("  wasted work larger or the failures faster, one knob at a time:")
        print("    STATEMENT_TIMEOUT_MS lower (more queries cancelled after burning CPU),")
        print("    SPIKE_MULT or SPIKE_S higher, POOL_TIMEOUT lower, MAX_RETRIES higher.")
    elif not a["recovered"] and not b["recovered"]:
        print("  Neither recovered, so the budget did not save B and the comparison is not")
        print("  a comparison yet. RETRY_BUDGET is too generous for this ratio (try 0.05),")
        print("  or the overload is deep enough that no retry policy recovers inside")
        print(f"  RECOVER_S={RECOVER_S:.0f}s -- lengthen it and see whether B comes back late.")
    else:
        print("  A recovered and B did not, which is the reverse of the intended result and")
        print("  is NOT evidence against retry budgets -- B refuses retries, so it can only")
        print("  ever offer the server LESS load than A. Read it as noise: the two variants")
        print("  ran seconds apart on a machine that is also running the database, and at")
        print("  this spike size the outcome is close to the threshold. Re-run it, and if it")
        print("  persists, raise SPIKE_S so both variants are pushed well past the edge")
        print("  rather than balanced on it.")
    print()
    print("  What makes it metastable rather than merely overloaded: the system has two")
    print("  stable states at the SAME offered load -- serving, and not serving -- and the")
    print("  spike moves it from one to the other. Nothing about the load afterwards moves")
    print("  it back. That is why the fix has to change the feedback loop, and why")
    print("  'add more capacity' postpones the trigger without removing the mechanism.")


if __name__ == "__main__":
    main()
