"""
Layer 5 - Topic 2: deadline propagation through a three-hop chain, in one
Python process.

Python is the language where this topic has a second half nobody expects.
Cancelling a coroutine cancels it at its next `await` -- it does NOT stop
work that has already been handed to something else. The query you already
sent keeps running on the database server until it finishes or
`statement_timeout` kills it, holding its pooled connection the whole time.
So a Python deadline discipline has two halves, and variant 3 below has
only the first one.

WHAT THIS DEMONSTRATES
  gateway -> service_b -> service_c, where C holds a pooled connection for
  a controlled service time. The gateway's budget is 500ms. Four variants:

    1. naive, C healthy       everything succeeds; the bug is invisible
    2. naive, C slow          one query in four takes 800ms; the gateway
                              504s at 500ms and C works on anyway
    3. deadline propagated    B and C refuse work that cannot finish, and
                              hand back a connection the moment they find
                              the request behind it is already dead
    4. + statement_timeout    the query itself is bounded, not just the
                              coroutine awaiting it

WHAT TO LOOK FOR IN THE OUTPUT
  1. `zombie/s` is zero in variant 1, large in variant 2. A zombie is a
     completion C finished AFTER the gateway had already returned 504:
     one pool slot, one full service time, zero value.
  2. `C pool in use` is pinned at the pool size in variant 2. That is
     topic 1's L, consumed entirely by work nobody is waiting for.
  3. Variant 3 helps and does not fix it. This is the Python-specific
     finding, and it is the point of the whole file: the ContextVar
     stopped your Python, and the 800ms query kept running to the end
     holding its connection. You shed load in the app and left the
     database pinned -- the worst of both, since you get the errors AND
     keep the load.
  4. Variant 4 is the one that frees the pool, because it bounds the work
     at the resource rather than at the caller. Compare the gateway
     success column across 2, 3 and 4 -- that is the number that pays for
     all of this.

The load generator is OPEN MODEL. It does not wait for a response before
sending the next request, because a real client does not either, and a
closed-loop generator would quietly hide every effect below.

RUN
    python3 deadline_chain.py
"""
from __future__ import annotations

import asyncio
import contextvars
import statistics
import time

# ---------------------------------------------------------------- config

GATEWAY_BUDGET = 0.500      # what the gateway promises its own caller
SLACK = 0.020               # subtracted per hop; also the reject-now floor
HOP_OVERHEAD = 0.005        # B's and C's own work, before the next hop
C_SERVICE_FAST = 0.040      # the ordinary query
C_SERVICE_SLOW = 0.800      # the same query when the dependency is unwell
SLOW_FRACTION = 0.25        # a slow dependency is usually slow for a SUBSET
C_POOL_SIZE = 8
RATE = 50.0                 # offered requests per second, Poisson arrivals
DURATION = 12.0
GAUGE_EVERY = 0.020

# Absolute deadline, in perf_counter time, for the request being handled on
# this task. contextvars is the only ambient carrier Python has, and an
# ABSOLUTE deadline is the load-bearing choice: a per-call `timeout=`
# argument cannot compose, because the third call in a handler has no idea
# what the first two already spent.
DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "deadline", default=None
)


def remaining() -> float:
    """Budget left for the current request, or infinity if nobody said."""
    d = DEADLINE.get()
    return float("inf") if d is None else d - time.perf_counter()


# ------------------------------------------------------------ the metrics


class Metrics:
    def __init__(self) -> None:
        self.gateway_ok = 0
        self.gateway_timeout = 0
        self.gateway_rejected = 0     # refused up front for lack of budget
        self.c_started = 0
        self.c_completed = 0
        self.c_zombie = 0             # completed after its gateway gave up
        self.c_killed = 0             # statement_timeout got there first
        self.c_abandoned = 0          # got a connection, gave it straight back
        self.c_latencies: list[float] = []
        self.pool_gauge: list[int] = []

    def row(self, label: str, seconds: float, pool_size: int) -> str:
        total = self.gateway_ok + self.gateway_timeout + self.gateway_rejected
        success = 100.0 * self.gateway_ok / total if total else 0.0
        p99 = _percentile(sorted(self.c_latencies), 99) * 1000
        gauge = statistics.fmean(self.pool_gauge) if self.pool_gauge else 0.0
        return (
            f"{label:<28} {success:>9.1f}% {self.c_zombie / seconds:>9.1f} "
            f"{gauge:>8.1f}/{pool_size:<5} {p99:>9.0f} "
            f"{self.c_killed / seconds:>9.1f} {self.c_abandoned / seconds:>11.1f}"
        )


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    k = round(p / 100 * (len(sorted_values) - 1))
    return sorted_values[min(k, len(sorted_values) - 1)]


# ------------------------------------------------- hop 3, and its database


class Database:
    """
    A pool of connections and a server that does not care about your
    coroutines. `query` holds a connection for the whole of its duration.
    Cancelling the *caller* does not shorten it -- only `statement_timeout`
    does, and only when one was set.
    """

    def __init__(self, size: int, metrics: Metrics) -> None:
        self.sem = asyncio.Semaphore(size)
        self.in_use = 0
        self.metrics = metrics

    async def query(self, duration: float, deadline: float | None,
                    use_statement_timeout: bool) -> bool:
        """Returns True if the query ran to completion, False if it did not."""
        async with self.sem:
            # Checked out. Everything below happens INSIDE the transaction,
            # which is the only place `SET LOCAL` means anything.
            now = time.perf_counter()
            if deadline is not None and deadline - now < SLACK:
                # The request that queued for this connection died while it
                # was queueing. Give the connection straight back rather
                # than spending a service time on a corpse. Under overload
                # this is where most of the recovered capacity comes from.
                self.metrics.c_abandoned += 1
                return False

            self.in_use += 1
            try:
                stmt = None
                if deadline is not None and use_statement_timeout:
                    # Derived from the SAME number as the application budget.
                    # Two independently chosen timeouts is how you get a
                    # service that sheds load while Postgres stays pinned.
                    stmt = max(0.0, deadline - now - SLACK)

                if stmt is None or stmt >= duration:
                    await asyncio.sleep(duration)
                    return True
                await asyncio.sleep(stmt)
                self.metrics.c_killed += 1
                return False
            finally:
                self.in_use -= 1


async def service_c(db: Database, m: Metrics, slow: bool,
                    propagate: bool, use_statement_timeout: bool,
                    gateway_deadline: float) -> None:
    if propagate and remaining() < SLACK:
        # Refuse to START work that cannot finish. This is the cheapest win
        # in the whole layer: a request rejected here costs no pool slot,
        # no queue position, nothing at all.
        raise TimeoutError("no budget left at C")

    await asyncio.sleep(HOP_OVERHEAD)

    duration = C_SERVICE_SLOW if slow else C_SERVICE_FAST
    started = time.perf_counter()
    m.c_started += 1

    # THE PYTHON-SPECIFIC PART. The query runs as its own task. When the
    # caller's `wait_for` fires, THIS coroutine is cancelled -- and the task
    # below is not, because it is shielded. That is precisely what happens
    # with a real driver: your `await` is abandoned, the statement on the
    # server is not.
    task = asyncio.create_task(
        db.query(duration, DEADLINE.get() if propagate else None,
                 use_statement_timeout)
    )

    def account(_fut: asyncio.Future) -> None:
        done = time.perf_counter()
        m.c_completed += 1
        m.c_latencies.append(done - started)
        if done > gateway_deadline:
            m.c_zombie += 1

    task.add_done_callback(account)
    await asyncio.shield(task)


# ------------------------------------------------------------ hops 2 and 1


async def service_b(db: Database, m: Metrics, slow: bool,
                    propagate: bool, use_statement_timeout: bool,
                    gateway_deadline: float) -> None:
    if propagate:
        left = remaining()
        if left < SLACK:
            raise TimeoutError("no budget left at B")

    await asyncio.sleep(HOP_OVERHEAD)

    if propagate:
        # budget_out = budget_in - elapsed_here - slack
        out = max(0.0, remaining() - SLACK)
    else:
        # The bug, and it looks completely reasonable on the page: a
        # constant, the same one the gateway used, chosen once and copied.
        out = GATEWAY_BUDGET

    await asyncio.wait_for(
        service_c(db, m, slow, propagate, use_statement_timeout,
                  gateway_deadline),
        timeout=out,
    )


async def gateway(db: Database, m: Metrics, slow: bool,
                  propagate: bool, use_statement_timeout: bool) -> None:
    deadline = time.perf_counter() + GATEWAY_BUDGET
    if propagate:
        # The one line that starts the whole discipline. Downstream reads it
        # from the context; over a real network it becomes a header.
        DEADLINE.set(deadline)

    try:
        await asyncio.wait_for(
            service_b(db, m, slow, propagate, use_statement_timeout, deadline),
            timeout=GATEWAY_BUDGET,
        )
        m.gateway_ok += 1
    except TimeoutError:
        # asyncio.wait_for raises TimeoutError on expiry, and our own
        # "refuse to start" path raises the same type on purpose: from the
        # gateway's seat they are the same outcome with very different costs.
        m.gateway_timeout += 1


# ------------------------------------------------------------- the driver


async def run_variant(slow_fraction: float, propagate: bool,
                      use_statement_timeout: bool) -> Metrics:
    m = Metrics()
    db = Database(C_POOL_SIZE, m)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    async def sample_pool() -> None:
        while not stop.is_set():
            await asyncio.sleep(GAUGE_EVERY)
            m.pool_gauge.append(db.in_use)

    sampler = asyncio.create_task(sample_pool())

    import random
    rng = random.Random(20250502)   # same arrivals and same slow requests
    begin = loop.time()             # in every variant, so the comparison
    end = begin + DURATION          # is of the policy and nothing else
    at = begin
    tasks: list[asyncio.Task] = []

    while True:
        at += rng.expovariate(RATE)
        if at > end:
            break
        delay = at - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        slow = rng.random() < slow_fraction
        # Each request runs in its own context, so the ContextVar set inside
        # the gateway belongs to that request and nothing else.
        tasks.append(
            asyncio.create_task(
                gateway(db, m, slow, propagate, use_statement_timeout),
                context=contextvars.copy_context(),
            )
        )

    await asyncio.gather(*tasks, return_exceptions=True)
    # Drain. Zombies are by definition still running after everyone gave up,
    # so a report taken at the end of the load would undercount them.
    await asyncio.sleep(C_SERVICE_SLOW + 0.3)
    stop.set()
    await sampler
    return m


HEADER = (f"{'variant':<28} {'gw success':>10} {'zombie/s':>9} "
          f"{'C pool in use':>14} {'C p99 ms':>9} {'killed/s':>9} {'gaveback/s':>11}")


async def main() -> None:
    fast_demand = RATE * (1 - SLOW_FRACTION) * C_SERVICE_FAST
    slow_demand = RATE * SLOW_FRACTION * C_SERVICE_SLOW
    print("Deadline propagation through gateway -> service_b -> service_c.")
    print(f"Gateway budget {GATEWAY_BUDGET*1000:.0f}ms, slack {SLACK*1000:.0f}ms/hop, "
          f"C pool {C_POOL_SIZE}, offered {RATE:.0f} rps for {DURATION:.0f}s.")
    print(f"When C is unwell, {SLOW_FRACTION:.0%} of queries take "
          f"{C_SERVICE_SLOW*1000:.0f}ms and the rest take {C_SERVICE_FAST*1000:.0f}ms.")
    print(f"Demand on the pool is then {slow_demand:.1f} + {fast_demand:.1f} = "
          f"{slow_demand + fast_demand:.1f} connection-seconds per second "
          f"against {C_POOL_SIZE} available,")
    print(f"i.e. rho = {(slow_demand + fast_demand) / C_POOL_SIZE:.2f}. The slow "
          f"queries alone are {slow_demand / C_POOL_SIZE:.0%} of the pool, and "
          f"none of them can beat the budget.\n")
    print(HEADER)
    print("-" * len(HEADER))

    variants = [
        ("1 naive, C healthy", 0.0, False, False),
        ("2 naive, C slow", SLOW_FRACTION, False, False),
        ("3 propagated", SLOW_FRACTION, True, False),
        ("4 propagated + stmt_timeout", SLOW_FRACTION, True, True),
    ]
    for label, frac, prop, stmt in variants:
        m = await run_variant(frac, prop, stmt)
        print(m.row(label, DURATION, C_POOL_SIZE))

    print()
    print("Rows 2 and 3: propagation stops C queueing work whose caller has")
    print("already gone, and hands connections back the moment it finds one")
    print("checked out for a dead request ('gaveback/s'). That is real, and on")
    print("its own it is not enough.")
    print()
    print("Rows 3 and 4 are the Python footnote, and the reason this file is")
    print("written in Python. Cancelling the coroutine stopped your code; the")
    print("800ms query kept running to the end, holding its connection, and")
    print("25% of arrivals doing that is more than the pool has to give. Only")
    print("row 4 bounds the work where the work actually is. Shedding in the")
    print("app while the database stays pinned is the worst of both outcomes:")
    print("you get the errors AND you keep the load.")
    print()
    print("Every zombie completion is one pool slot for one service time,")
    print("producing a response no client will read. That is topic 1's rho,")
    print("climbing for work worth nothing.")


if __name__ == "__main__":
    asyncio.run(main())
