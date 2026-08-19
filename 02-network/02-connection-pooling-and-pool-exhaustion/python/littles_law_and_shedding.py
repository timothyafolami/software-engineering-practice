"""
Layer 2 · Topic 2 - The pool ceiling, crossed on purpose, four ways.

This is the load-shedding experiment from the README, self-contained: no
Docker, no k6, no Postgres. What it keeps from that design is the only
part that actually matters -- the load generator is OPEN model. Requests
arrive on a schedule whether or not the previous ones finished. A
closed-model generator (N workers, each waiting for a response before
sending again) slows down exactly when the service slows down, which means
it CANNOT produce queueing, pool exhaustion or a latency cliff. If you
take one habit from this file, take that one.

The service is a FastAPI handler's shape with the framework removed: each
request checks out one connection from a bounded pool, holds it for
QUERY_SECONDS, and returns. That is enough, because the mechanism is the
pool and not the framework.

Little's Law, L = lambda x W, gives the ceiling before you run anything:
a pool of N connections each held for W seconds sustains N / W requests
per second, and not one more. Everything above that queues.

Four configurations, all driven at the same arrival rate:

  1. defaults            - small pool, unbounded wait. The incident.
  2. bigger pool         - the fix everyone reaches for first.
  3. pool timeout        - bounded wait, requests fail instead of queueing.
  4. load shedding       - reject at a known in-flight threshold, immediately.

What to look for in the output:
  - completed/s is roughly the SAME in all four. The pool sets throughput;
    nothing here can beat Little's Law.
  - p99 and max queue depth are what change. Config 1 has an unbounded p99
    and a zero error rate, which is precisely why it is hard to diagnose:
    every dependency reports itself healthy while the service is unusable.
  - config 4 has a bounded p99 AND a visible error rate. That is the trade
    you are actually being offered.

Run: python3 littles_law_and_shedding.py
"""
import asyncio
import time

POOL_SIZE = 5             # SQLAlchemy's default pool_size, deliberately
QUERY_SECONDS = 0.050     # a 50 ms query, which is a good day
ARRIVAL_RATE = 250        # requests per second, open model
DURATION = 6.0            # seconds of load
SHED_THRESHOLD = POOL_SIZE * 3   # in-flight requests above which we return 503


class Pool:
    """A bounded connection pool with the two knobs that decide an incident.

    `timeout=None` is SQLAlchemy's `pool_timeout` being effectively infinite
    from the request's point of view (its real default is 30 s, which at any
    interesting arrival rate is the same thing: the client gave up long ago).
    """

    def __init__(self, size, timeout=None):
        self._semaphore = asyncio.Semaphore(size)
        self._timeout = timeout
        self.size = size
        self.waiters = 0
        self.max_waiters = 0
        self.timeouts = 0

    async def __aenter__(self):
        self.waiters += 1
        self.max_waiters = max(self.max_waiters, self.waiters)
        try:
            if self._timeout is None:
                await self._semaphore.acquire()
            else:
                await asyncio.wait_for(self._semaphore.acquire(), self._timeout)
        except asyncio.TimeoutError:
            self.timeouts += 1
            raise
        finally:
            self.waiters -= 1
        return self

    async def __aexit__(self, *exc_info):
        self._semaphore.release()


class Service:
    """One request handler. Checks out a connection, holds it, returns."""

    def __init__(self, pool, shed_above=None):
        self.pool = pool
        self.shed_above = shed_above
        self.in_flight = 0
        self.max_in_flight = 0
        self.latencies_ok = []
        self.latencies_shed = []
        self.completed = 0
        self.rejected = 0
        self.failed = 0

    async def handle(self):
        started = time.perf_counter()
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            # Load shedding happens BEFORE the pool, not after. Rejecting a
            # request that is already queued for a connection saves nothing.
            if self.shed_above is not None and self.in_flight > self.shed_above:
                self.rejected += 1
                self.latencies_shed.append((time.perf_counter() - started) * 1000)
                return
            try:
                async with self.pool:
                    await asyncio.sleep(QUERY_SECONDS)   # the query
            except asyncio.TimeoutError:
                self.failed += 1
                self.latencies_shed.append((time.perf_counter() - started) * 1000)
                return
            self.completed += 1
            self.latencies_ok.append((time.perf_counter() - started) * 1000)
        finally:
            self.in_flight -= 1


async def open_loop(service, rate, duration):
    """Fire requests on a fixed schedule. Never waits for a response.

    The schedule is computed from a start time rather than by sleeping for
    1/rate in a loop -- otherwise the scheduler's own overhead accumulates
    and the real arrival rate silently drifts below the configured one,
    which would hide the very effect being measured.
    """
    started = time.perf_counter()
    tasks = []
    index = 0
    while True:
        due = started + index / rate
        now = time.perf_counter()
        if due - started > duration:
            break
        if due > now:
            await asyncio.sleep(due - now)
        tasks.append(asyncio.create_task(service.handle()))
        index += 1
    issued = index
    issue_window = time.perf_counter() - started
    # Give in-flight work a bounded chance to drain, then stop waiting. A
    # config that cannot drain is a finding, not a reason to hang.
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=30)
        drained = True
    except asyncio.TimeoutError:
        drained = False
    # Two clocks on purpose. The arrival rate must be measured over the window
    # in which requests were ISSUED; measuring it over issue+drain would divide
    # by the backlog and make an open-model generator look closed-model, which
    # is the exact mistake this experiment exists to avoid.
    return issued, issue_window, time.perf_counter() - started, drained


def at(values, fraction):
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


async def scenario(label, pool_size, pool_timeout, shed_above):
    pool = Pool(pool_size, pool_timeout)
    service = Service(pool, shed_above)
    issued, issue_window, total_elapsed, drained = await open_loop(service, ARRIVAL_RATE, DURATION)

    ceiling = pool_size / QUERY_SECONDS
    all_latencies = service.latencies_ok + service.latencies_shed
    print(f"  {label}")
    print(f"    pool {pool_size}, pool_timeout {pool_timeout}, shed above "
          f"{shed_above if shed_above is not None else '-'} in flight")
    print(f"    Little's Law ceiling   {ceiling:.0f} rps   (pool {pool_size} / {QUERY_SECONDS}s)")
    print(f"    offered                {issued / issue_window:.0f} rps   "
          f"({issued} requests issued over {issue_window:.1f}s)")
    print(f"    completed              {service.completed / total_elapsed:.0f} rps   "
          f"(over the full {total_elapsed:.1f}s including drain)")
    if total_elapsed > issue_window * 1.2:
        print(f"    BACKLOG                took {total_elapsed - issue_window:.1f}s to drain "
              f"after the load stopped")
    print(f"    rejected (shed)        {service.rejected}")
    print(f"    failed (pool timeout)  {service.failed}")
    error_rate = (service.rejected + service.failed) / max(issued, 1) * 100
    print(f"    client-visible errors  {error_rate:.1f}%")
    print(f"    latency p50 {at(all_latencies, 0.50):8.1f} ms   "
          f"p95 {at(all_latencies, 0.95):8.1f} ms   "
          f"p99 {at(all_latencies, 0.99):8.1f} ms   "
          f"max {max(all_latencies) if all_latencies else float('nan'):8.1f} ms")
    if service.latencies_ok:
        print(f"    successful requests only: p99 {at(service.latencies_ok, 0.99):.1f} ms")
    print(f"    peak in-flight         {service.max_in_flight}")
    print(f"    peak waiting for pool  {pool.max_waiters}")
    if not drained:
        print("    NOTE: still had work in flight after 30s of draining. That is the")
        print("    finding, not a bug -- the backlog outlives the load that made it.")


async def main():
    print("=" * 78)
    print("Crossing the pool ceiling on purpose, with an open-model generator")
    print("=" * 78)
    print(f"  arrival rate {ARRIVAL_RATE} rps for {DURATION}s, "
          f"query time {QUERY_SECONDS * 1000:.0f} ms")
    print(f"  ceiling with the default pool of {POOL_SIZE}: "
          f"{POOL_SIZE / QUERY_SECONDS:.0f} rps")
    print(f"  offered load is {ARRIVAL_RATE / (POOL_SIZE / QUERY_SECONDS):.1f}x the ceiling\n")

    await scenario("1. DEFAULTS - pool of 5, wait forever",
                   POOL_SIZE, None, None)
    print()
    await scenario("2. BIGGER POOL - pool of 20, wait forever",
                   POOL_SIZE * 4, None, None)
    print()
    await scenario("3. POOL TIMEOUT - pool of 5, give up after 200 ms",
                   POOL_SIZE, 0.2, None)
    print()
    await scenario(f"4. LOAD SHEDDING - pool of 5, 503 above {SHED_THRESHOLD} in flight",
                   POOL_SIZE, None, SHED_THRESHOLD)

    print()
    print("  Read the four together:")
    print("    Config 2 quadruples the pool and moves the ceiling by exactly 4x --")
    print("    which is useful only if 4x is above your real arrival rate. It is")
    print("    also the config most likely to make a real incident worse, because")
    print("    the thing on the other end of those connections is a database with")
    print("    its own limit, and 4 workers x 20 connections is 80 of Postgres's")
    print("    100.")
    print("    Configs 3 and 4 do not raise throughput at all. They convert an")
    print("    unbounded queue into a bounded one, which turns an invisible")
    print("    latency failure into a visible error rate you can alert on, retry")
    print("    against, and put on a dashboard. That is the whole trade.")
    print()
    print("  What would mean this run is broken rather than your prediction wrong:")
    print("    - completed/s far above the Little's Law ceiling: the pool is not")
    print("      actually bounding anything; check the semaphore size.")
    print("    - offered/s well below ARRIVAL_RATE: the generator self-throttled,")
    print("      so it is closed-model after all and no queue can form.")
    print("    - p99 identical across all four configs: the arrival rate never")
    print("      crossed the ceiling. Raise ARRIVAL_RATE or QUERY_SECONDS.")


if __name__ == "__main__":
    asyncio.run(main())
