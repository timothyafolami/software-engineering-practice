"""
Layer 2 · Topic 3 - Metastable failure, reproduced, then stopped.

This is the Bronson et al. (HotOS '21) mechanism in a file you can run:
a transient trigger raises load, retries amplify it, and the system stays
collapsed AFTER the trigger is gone, because the retry load is now the
trigger.

The measurement that matters here is not p99 during the fault. It is
TIME TO RECOVERY AFTER THE FAULT IS REMOVED. A system that recovers when
the trigger stops was merely overloaded. A system that does not is
metastable, and no amount of adding capacity during the incident will fix
it -- you have to shed the retries.

Setup, all in one process, no containers:

  upstream : capacity CAPACITY concurrent requests, SERVICE_MS each, with
             a bounded wait queue. That gives it a real, computable
             throughput ceiling (Little's Law again).
  fault    : between FAULT_START and FAULT_END the upstream's service time
             multiplies by SLOWDOWN. Nothing else changes. The fault is
             removed cleanly at FAULT_END.
  client   : open-model arrivals at ARRIVAL_RATE. Two retry policies.

  policy 1 - NAIVE     : up to 3 attempts, immediate, no jitter, no budget.
  policy 2 - DISCIPLINED: exponential backoff, FULL jitter, attempt cap, a
             retry BUDGET (a token bucket capping retries at 10% of base
             traffic), and a circuit breaker.

What to look for in the output: the per-second timeline.
  - `up/s` is what the upstream actually received. Under the naive policy
    it goes far above `off/s`. That multiplier is the amplification.
  - after the fault is removed (the line marked FAULT OFF), watch `good/s`.
    If it does not return to `off/s` promptly, the system is metastable.
  - the disciplined policy's amplification should stay near 1.1x, which is
    the retry budget doing its job.

Run: python3 retry_storm_and_budget.py
"""
import asyncio
import random
import time

CAPACITY = 20            # concurrent requests the upstream can serve
SERVICE_MS = 20          # normal service time
SLOWDOWN = 8             # multiplier applied during the fault
QUEUE_LIMIT = 400        # upstream rejects with 503 beyond this many waiting
ARRIVAL_RATE = 600       # requests per second, open model
CLIENT_TIMEOUT = 0.25    # per-attempt timeout
TOTAL_SECONDS = 18
FAULT_START = 4
FAULT_END = 8


class Upstream:
    """A dependency with a real capacity limit and a bounded queue."""

    def __init__(self):
        self.semaphore = asyncio.Semaphore(CAPACITY)
        self.waiting = 0
        self.slow = False
        self.arrivals = 0          # every attempt, including retries
        self.rejections = 0
        self.wasted = 0            # work finished for a client that had left
        self._running = set()

    async def _serve(self, started_for):
        try:
            service = SERVICE_MS * (SLOWDOWN if self.slow else 1) / 1000
            await asyncio.sleep(service)
        finally:
            self.semaphore.release()
            if started_for["abandoned"]:
                self.wasted += 1

    async def call(self):
        self.arrivals += 1
        if self.waiting >= QUEUE_LIMIT:
            # Shedding at the dependency. Note what this does under a naive
            # retry policy: a fast 503 is a fast invitation to try again.
            self.rejections += 1
            raise ConnectionError("503 from upstream")
        self.waiting += 1
        try:
            await self.semaphore.acquire()
        finally:
            self.waiting -= 1

        # THE detail that makes a retry storm self-sustaining, and the one a
        # naive simulation leaves out: once the upstream has started work, the
        # client giving up does NOT stop it. The server has no idea the caller
        # left. So capacity keeps being spent on answers nobody will read,
        # while the retry those clients just sent queues behind that work.
        # asyncio.shield() models exactly that: cancelling the caller does not
        # cancel the task it is shielding.
        marker = {"abandoned": False}
        task = asyncio.create_task(self._serve(marker))
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            marker["abandoned"] = True
            raise


class RetryBudget:
    """A token bucket that caps retries as a FRACTION of base traffic.

    This is the part of the AWS Builders' Library retry advice that people
    leave out, and it is the part that actually stops a storm. Backoff and
    jitter change WHEN retries arrive. Only a budget changes HOW MANY.
    """

    def __init__(self, ratio=0.1, capacity=100):
        self.ratio = ratio
        self.capacity = capacity
        self.tokens = capacity

    def on_request(self):
        self.tokens = min(self.capacity, self.tokens + self.ratio)

    def try_spend(self):
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class CircuitBreaker:
    """Closed -> open on failure ratio, half-open after a cooldown."""

    def __init__(self, threshold=0.5, minimum=20, cooldown=1.0):
        self.threshold = threshold
        self.minimum = minimum
        self.cooldown = cooldown
        self.failures = 0
        self.successes = 0
        self.opened_at = None
        self.rejected = 0

    def allows(self):
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown:
            # Half-open: let one through and see.
            self.opened_at = None
            self.failures = self.successes = 0
            return True
        self.rejected += 1
        return False

    def record(self, ok):
        if ok:
            self.successes += 1
        else:
            self.failures += 1
        total = self.successes + self.failures
        if total >= self.minimum and self.failures / total > self.threshold:
            self.opened_at = time.monotonic()


class Stats:
    def __init__(self):
        self.offered = 0
        self.good = 0
        self.bad = 0
        self.attempts = 0


async def naive_client(upstream, stats):
    """Three attempts, immediately, forever. The default everyone writes."""
    for attempt in range(3):
        stats.attempts += 1
        try:
            await asyncio.wait_for(upstream.call(), CLIENT_TIMEOUT)
            stats.good += 1
            return
        except (asyncio.TimeoutError, ConnectionError):
            continue
    stats.bad += 1


async def disciplined_client(upstream, stats, budget, breaker):
    budget.on_request()
    for attempt in range(3):
        if not breaker.allows():
            stats.bad += 1
            return
        if attempt > 0:
            if not budget.try_spend():
                # Out of retry budget. This request fails without retrying,
                # which is the whole point: the storm is capped globally
                # rather than per-request.
                stats.bad += 1
                return
            # Exponential backoff with FULL jitter: sleep(random(0, base*2^n)).
            # Not "base*2^n +/- a bit" -- full jitter, because the failure
            # jitter prevents is synchronised retries from many clients that
            # all failed at the same instant.
            backoff = min(0.4, 0.02 * (2 ** attempt))
            await asyncio.sleep(random.uniform(0, backoff))
        stats.attempts += 1
        try:
            await asyncio.wait_for(upstream.call(), CLIENT_TIMEOUT)
            breaker.record(True)
            stats.good += 1
            return
        except (asyncio.TimeoutError, ConnectionError):
            breaker.record(False)
    stats.bad += 1


async def scenario(label, disciplined):
    upstream = Upstream()
    stats = Stats()
    budget = RetryBudget()
    breaker = CircuitBreaker()

    print(f"  {label}")
    print("    sec |  off/s   up/s  good/s   bad/s | amp  | note")
    print("    ----+-------------------------------+------+------------------------")

    start = time.monotonic()
    tasks = []
    index = 0
    last_snapshot = (0, 0, 0, 0)
    recovery_second = None

    for second in range(TOTAL_SECONDS):
        upstream.slow = FAULT_START <= second < FAULT_END
        deadline = start + second + 1
        while True:
            due = start + index / ARRIVAL_RATE
            if due >= deadline:
                break
            now = time.monotonic()
            if due > now:
                await asyncio.sleep(due - now)
            stats.offered += 1
            index += 1
            if disciplined:
                tasks.append(asyncio.create_task(
                    disciplined_client(upstream, stats, budget, breaker)))
            else:
                tasks.append(asyncio.create_task(naive_client(upstream, stats)))
        now = time.monotonic()
        if deadline > now:
            await asyncio.sleep(deadline - now)

        snapshot = (stats.offered, upstream.arrivals, stats.good, stats.bad)
        offered, arrivals, good, bad = (a - b for a, b in zip(snapshot, last_snapshot))
        last_snapshot = snapshot
        amplification = arrivals / offered if offered else 0

        note = ""
        if second == FAULT_START:
            note = "FAULT ON (upstream 8x slower)"
        elif second == FAULT_END:
            note = "FAULT OFF - the trigger is gone"
        elif second > FAULT_END:
            if good >= offered * 0.95 and recovery_second is None:
                recovery_second = second
                note = f"recovered ({second - FAULT_END}s after the fault ended)"
            elif recovery_second is None:
                note = "still collapsed"
        print(f"    {second:3d} | {offered:6d} {arrivals:6d} {good:7d} {bad:7d} "
              f"| {amplification:4.1f}x | {note}")

    # Do not wait forever for a collapsed run to drain; report it instead.
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=15)
        drained = True
    except asyncio.TimeoutError:
        drained = False

    print(f"    totals: offered {stats.offered}, upstream saw {upstream.arrivals} "
          f"({upstream.arrivals / max(stats.offered, 1):.2f}x), "
          f"good {stats.good}, bad {stats.bad}")
    print(f"    upstream capacity spent on work whose caller had already given up: "
          f"{upstream.wasted}")
    if breaker.rejected:
        print(f"    circuit breaker rejected {breaker.rejected} calls without trying")
    if recovery_second is None:
        print("    NEVER RECOVERED inside the run window. That is metastability:")
        print("    the fault ended and the failure did not.")
    if not drained:
        print("    (work still in flight after 15s of draining)")
    return recovery_second


async def main():
    random.seed(7)  # so backoff jitter is reproducible run to run
    print("=" * 78)
    print("Retry amplification and metastable failure, with and without a budget")
    print("=" * 78)
    print(f"  upstream: {CAPACITY} concurrent x {SERVICE_MS} ms = "
          f"{CAPACITY / (SERVICE_MS / 1000):.0f} rps of capacity")
    print(f"  during the fault: {CAPACITY / (SERVICE_MS * SLOWDOWN / 1000):.0f} rps")
    print(f"  offered load: {ARRIVAL_RATE} rps, fault from t={FAULT_START}s to "
          f"t={FAULT_END}s\n")

    naive_recovery = await scenario("1. NAIVE RETRY - 3 attempts, no backoff, no budget",
                                    disciplined=False)
    print()
    good_recovery = await scenario(
        "2. DISCIPLINED - backoff + full jitter + attempt cap + retry budget + breaker",
        disciplined=True)

    print()
    print("  Recovery after the fault was removed:")
    print(f"    naive       {naive_recovery if naive_recovery is not None else 'never (within the run)'}")
    print(f"    disciplined {good_recovery if good_recovery is not None else 'never (within the run)'}")
    print()
    print("  The four parts of the fix, and what each one actually does:")
    print("    exponential backoff - spreads a client's own retries over time.")
    print("                          Does nothing about how MANY there are.")
    print("    full jitter         - stops many clients that failed at the same")
    print("                          instant from retrying at the same instant.")
    print("                          random.uniform(0, base*2^n), not base +/- x.")
    print("    attempt cap         - bounds the worst case per request.")
    print("    retry BUDGET        - bounds retries across the whole client as a")
    print("                          fraction of base traffic. This is the one that")
    print("                          stops the storm, and the one people omit.")
    print("    circuit breaker     - stops calling a dependency that is failing,")
    print("                          which converts slow failures into fast ones.")
    print()
    print("  What would mean this run is broken rather than your prediction wrong:")
    print("    - amplification stays at 1.0x under the naive policy: nothing")
    print("      failed, so nothing retried. Increase SLOWDOWN or ARRIVAL_RATE.")
    print("    - the naive run recovers instantly: the backlog never got large")
    print("      enough to sustain itself. Lengthen the fault window.")
    print("    - both policies look identical: check that the retry budget is")
    print("      actually being consumed (tokens start at 100 and refill at 10%).")


if __name__ == "__main__":
    asyncio.run(main())
