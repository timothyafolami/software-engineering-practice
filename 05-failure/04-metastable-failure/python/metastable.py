"""
Layer 5 - Topic 4: metastable failure, reproduced in one Python process.

THE FLAGSHIP. The claim is not "overload is bad" -- everyone knows that.
The claim is that the thing which TRIGGERS an outage and the thing which
SUSTAINS it are different mechanisms, so removing the trigger does not end
the outage. This file removes the trigger, keeps offered load exactly
where it was, waits, and shows you nothing improving.

Python is the most exposed of the six runtimes here, for reasons that are
all defaults rather than decisions:
  * `asyncio.create_task` never blocks, so there is no natural
    backpressure anywhere. An overloaded service quietly accumulates
    pending tasks until memory pressure and GC become a SECOND sustaining
    effect stacked on the first. The tell is RSS climbing while goodput
    falls, and the report below prints in-flight count for exactly that.
  * A pool waiter whose `pool_timeout` expires and which immediately
    retries re-enters the same queue -- a closed feedback loop entirely
    inside one process, with no clients involved.

WHAT THIS DEMONSTRATES
  A cache in front of a database, at a 90% hit rate, comfortably stable.
  The trigger is one instantaneous, fully reversible command: FLUSHALL.
  The cache is BACK the moment it starts refilling -- except that it never
  starts, because refilling requires a query to finish before its caller
  gives up, and no query does any more.

  HotOS '25 vocabulary, which this file is built to make concrete:
    trigger                 the cache flush, over in one millisecond
    amplification mechanism naive retries (topic 3) plus a 10x rise in
                            database load from the miss rate going 10% ->
                            100%
    sustaining effect       a cache that cannot refill, because fills only
                            happen on completions that beat the deadline

WHAT TO LOOK FOR IN THE OUTPUT
  1. `goodput` versus `thruput`. Throughput stays high while goodput goes
     to zero: the process is busy, the pool is full, requests are flowing,
     and almost none of them produce a response anybody receives. Every
     dashboard that counts "requests handled" shows a healthy system
     during a total outage.
  2. `hit%` stuck at zero AFTER the trigger is long gone. That is the
     sustaining effect, and it is why scenario 0 never recovers.
  3. `inflight` jumping from single digits to a few hundred the moment
     the trigger lands, and STAYING there. Read the plateau carefully:
     nothing in the runtime bounds that number -- `create_task` never
     refuses -- so what holds it down is the CLIENT giving up after
     ATTEMPTS tries and OFFERED_RPS being finite, not the server refusing
     anything. Make the caller patient, or raise ATTEMPTS, and the same
     line climbs without bound until memory pressure and GC become a
     second sustaining effect stacked on the first.
  4. Which escapes are SUFFICIENT rather than merely helpful. The verdict
     lines at the end are computed from THIS run, not asserted here -- and
     watch where the famous one, "drop traffic and let it back slowly",
     lands, for a reason worth more than the escape itself.

RUN
    python3 metastable.py

Roughly four minutes: five scenarios, the four with an escape running
longer because "did it recover" is a question about minutes, not seconds.
"""
from __future__ import annotations

import asyncio
import random
import time

# ---------------------------------------------------------------- config

OFFERED_RPS = 180.0        # constant. It never changes. That is the point.
KEYS = 400                 # the cache keyspace
EVICT_PER_SEC = 18.0       # TTL churn -> equilibrium hit rate 1 - 18/180 = 90%

DB_SERVICE = 0.200         # an uncached read
CACHE_SERVICE = 0.001      # a cached one
POOL_SIZE = 6              # 6 / 0.200 = 30 misses per second of capacity

CLIENT_TIMEOUT = 0.500     # longer than normal service time, shorter than
ATTEMPTS = 3               # degraded. Topic 4's third bullet, on purpose.

WARM_UNTIL = 6.0           # establish and verify the stable state
TRIGGER_AT = 6.0           # redis-cli FLUSHALL
ESCAPE_AT = 16.0           # ten seconds of watching nothing improve first
END_AT = 30.0              # long enough to prove scenario 0 does not recover
ESCAPE_END_AT = 50.0       # escapes get longer, because "did it recover" is a
                           # question about minutes, not seconds
REPORT_EVERY = 2.0

SHED_LIMIT = 8             # escape (c). Topic 5, borrowed early.
BUDGET_RATIO = 0.10        # escape (b). Topic 3's token bucket.
RAMP_BACK_SECONDS = 8.0    # escape (a) lets load back SLOWLY. It matters.
DROP_SECONDS = 5.0


# --------------------------------------------------------------- the cache


class Cache:
    """
    Redis, modelled as the only thing about Redis that matters here: a set
    of keys that are present, and the fact that emptying it is instant and
    refilling it is not.
    """

    def __init__(self) -> None:
        self.present: set[int] = set(range(KEYS))
        self.hits = 0
        self.misses = 0

    def get(self, key: int) -> bool:
        if key in self.present:
            self.hits += 1
            return True
        self.misses += 1
        return False

    def put(self, key: int) -> None:
        self.present.add(key)

    def flushall(self) -> None:
        # One command. Instantaneous. Fully reversible. This is the entire
        # trigger, and ten seconds later it will be completely irrelevant
        # to why the system is down.
        self.present.clear()

    def evict(self, n: int, rng: random.Random) -> None:
        # Ordinary TTL churn, which is what holds the hit rate at 90%
        # instead of letting it climb to 100% and make the experiment lie.
        for _ in range(n):
            if self.present:
                self.present.discard(rng.choice(tuple(self.present)))


# ------------------------------------------------------------ the database


class Database:
    """A real bounded pool. 6 connections at 200ms is 30 queries a second,
    and nothing anybody does to the application changes that number."""

    def __init__(self) -> None:
        self.sem = asyncio.Semaphore(POOL_SIZE)
        self.in_use = 0

    async def query(self) -> None:
        async with self.sem:
            self.in_use += 1
            try:
                await asyncio.sleep(DB_SERVICE)
            finally:
                self.in_use -= 1


# ------------------------------------------------------------ retry budget


class RetryBudget:
    """Topic 3's token bucket, used here only as escape (b)."""

    def __init__(self) -> None:
        self.tokens = 3.0

    def deposit(self) -> None:
        self.tokens = min(self.tokens + BUDGET_RATIO, 103.0)

    def withdraw(self) -> bool:
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


# ------------------------------------------------------------- the server


class Server:
    def __init__(self, cache: Cache, db: Database, m: "Metrics") -> None:
        self.cache = cache
        self.db = db
        self.m = m
        self.inflight = 0
        self.budget: RetryBudget | None = None    # escape (b)
        self.shed_limit: int | None = None        # escape (c)

    async def handle(self, key: int, client_deadline: float) -> bool:
        """One attempt. Returns True if the caller got an answer in time."""
        # Escape (c), and topic 5 in one line: refuse work you have no
        # capacity for, immediately, instead of accepting it and being late.
        if self.shed_limit is not None and self.inflight >= self.shed_limit:
            self.m.shed += 1
            return False

        self.inflight += 1
        try:
            if self.cache.get(key):
                await asyncio.sleep(CACHE_SERVICE)
                return time.perf_counter() <= client_deadline

            await self.db.query()
            in_time = time.perf_counter() <= client_deadline
            if in_time:
                # THE SUSTAINING EFFECT, in one `if`. The fill happens in
                # the handler, after the query returns -- and under overload
                # the handler has already been abandoned by then, so the
                # fill never happens. The cache cannot refill precisely
                # because the database is slow, and the database is slow
                # precisely because the cache is empty.
                self.cache.put(key)
            return in_time
        finally:
            self.inflight -= 1


# -------------------------------------------------------------- the client


async def client_request(server: Server, m: "Metrics", key: int) -> None:
    """Topic 3's naive retry client: no jitter, no budget unless escape (b)
    turned one on, and a per-attempt timeout that is comfortable when the
    system is well and hopeless when it is not."""
    for attempt in range(ATTEMPTS):
        if attempt > 0:
            if server.budget is not None and not server.budget.withdraw():
                break
            m.retries += 1
        deadline = time.perf_counter() + CLIENT_TIMEOUT
        try:
            ok = await asyncio.wait_for(server.handle(key, deadline),
                                        timeout=CLIENT_TIMEOUT)
        except TimeoutError:
            # We stopped waiting. The task inside did not stop working --
            # topic 2 -- and the retry we are about to send is additive.
            m.thruput_attempts += 1
            continue
        m.thruput_attempts += 1
        if ok:
            # GOODPUT: a response delivered to a caller that was still
            # waiting for it. Not "requests handled". This is the only
            # number in this file worth alerting on.
            m.goodput += 1
            if server.budget is not None:
                server.budget.deposit()
            return
    m.failed += 1


# ------------------------------------------------------------- the harness


class Metrics:
    def __init__(self) -> None:
        self.goodput = 0
        self.thruput_attempts = 0
        self.retries = 0
        self.failed = 0
        self.shed = 0
        self.end_at = END_AT
        self.rows: list[tuple] = []


def offered_rate(t: float, escape: str) -> float:
    """Offered load. Constant everywhere except escape (a), which is the
    only intervention in this file that touches the client side at all."""
    if escape != "a" or t < ESCAPE_AT:
        return OFFERED_RPS
    since = t - ESCAPE_AT
    if since < DROP_SECONDS:
        return 0.0                                   # take the load away
    ramp = (since - DROP_SECONDS) / RAMP_BACK_SECONDS  # ... and let it back
    return OFFERED_RPS * min(1.0, ramp)                # SLOWLY


async def run_scenario(escape: str) -> Metrics:
    end_at = ESCAPE_END_AT if escape else END_AT
    m = Metrics()
    cache = Cache()
    db = Database()
    server = Server(cache, db, m)
    rng = random.Random(20250504)

    loop = asyncio.get_running_loop()
    begin = loop.time()
    last_report = begin
    last = (0, 0, 0)
    triggered = False
    escaped = False
    tasks: list[asyncio.Task] = []

    last_evict = begin
    at = begin
    while True:
        t = at - begin
        if t > end_at:
            break

        rate = offered_rate(t, escape)
        if rate <= 0:
            at += 0.05
        else:
            at += rng.expovariate(rate)
        delay = at - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        now = loop.time()
        t = now - begin

        if not triggered and t >= TRIGGER_AT:
            cache.flushall()
            triggered = True
        if not escaped and t >= ESCAPE_AT:
            escaped = True
            if escape == "b":
                server.budget = RetryBudget()
            elif escape == "c":
                server.shed_limit = SHED_LIMIT
            elif escape == "d":
                # "Restart the app containers." Everything in the process
                # goes: the queue, the in-flight requests, the pool. The
                # cache is external and stays exactly as cold as it was,
                # and the clients never stopped retrying.
                for task in tasks:
                    task.cancel()
                tasks = []
                # Rebind rather than reset in place. A restart replaces the
                # process: the new one starts with an empty pool and a zero
                # gauge, and the dying requests unwind against the old
                # objects. Zeroing the counters underneath the tasks that
                # are still cancelling would have them run their `finally`
                # against the fresh state and drive the gauges NEGATIVE --
                # which is a bug in the instrument, not a finding.
                db = Database()
                server = Server(cache, db, m)

        if now - last_evict >= 1.0:
            cache.evict(int(EVICT_PER_SEC), rng)
            last_evict = now

        if rate > 0:
            tasks.append(asyncio.create_task(
                client_request(server, m, rng.randrange(KEYS))))
            # No backpressure anywhere in that line. create_task always
            # succeeds, whatever the state of the system it is feeding.

        if now - last_report >= REPORT_EVERY:
            span = now - last_report
            g, th, r = m.goodput, m.thruput_attempts, m.retries
            m.rows.append((
                t,
                rate,
                (th - last[1]) / span,
                (g - last[0]) / span,
                100.0 * cache.hits / max(1, cache.hits + cache.misses),
                db.in_use,
                server.inflight,
                (r - last[2]) / max(1e-9, (th - last[1])),
            ))
            cache.hits = cache.misses = 0
            last = (g, th, r)
            last_report = now

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    m.end_at = end_at
    return m


# ------------------------------------------------------------- reporting

HEADER = ("      t   offered   thruput   goodput   hit%   pg  inflight  retry/req"
          "   goodput as % of offered")


def render(title: str, note: str, m: Metrics) -> tuple[float, float]:
    print(f"\n=== {title} ===")
    print(f"    {note}")
    print(HEADER)
    print("-" * len(HEADER))
    for (t, offered, th, good, hit, pg, inflight, retry) in m.rows:
        frac = good / OFFERED_RPS
        bar = "#" * max(0, round(24 * min(1.0, frac)))
        mark = ""
        if abs(t - TRIGGER_AT) < REPORT_EVERY / 2:
            mark = "  <-- FLUSHALL"
        elif abs(t - ESCAPE_AT) < REPORT_EVERY / 2:
            mark = "  <-- escape applied"
        print(f"  {t:5.1f} {offered:9.1f} {th:9.1f} {good:9.1f} {hit:6.1f} "
              f"{pg:4d} {inflight:9d} {retry:10.2f}   |{bar}{mark}")

    before = [r for r in m.rows if r[0] < TRIGGER_AT]
    after = [r for r in m.rows if r[0] >= m.end_at - 6]
    g_before = sum(r[3] for r in before) / len(before) if before else 0.0
    g_after = sum(r[3] for r in after) / len(after) if after else 0.0
    print(f"    goodput before the trigger {g_before:6.1f} rps "
          f"({100 * g_before / OFFERED_RPS:.0f}% of offered)   "
          f"final 6 seconds {g_after:6.1f} rps "
          f"({100 * g_after / OFFERED_RPS:.0f}% of offered)")
    return g_before, g_after


def verdict(before: float, after: float) -> str:
    """COMPUTED from the run that just happened, never asserted here.
    Sufficient means "goodput came back", not "the intervention did something
    measurable" -- that distinction is the whole of step 5 in the README, and
    it is the difference between an escape and a comfort."""
    if before <= 1:
        return "baseline never established -- see README"
    pct = 100.0 * after / before
    if pct >= 70:
        return f"SUFFICIENT   (recovered to {pct:.0f}% of pre-trigger goodput)"
    if pct >= 20:
        return f"partial      (only {pct:.0f}% of pre-trigger goodput)"
    return f"not sufficient ({pct:.0f}% of pre-trigger goodput)"


async def main() -> None:
    print("Metastable failure: a cache flush that stops mattering long before "
          "the outage does.")
    print(f"Offered load is constant at {OFFERED_RPS:.0f} rps and is never "
          f"raised. Cache hit rate {100 - 100 * EVICT_PER_SEC / OFFERED_RPS:.0f}% "
          f"when warm.")
    print(f"Database capacity is {POOL_SIZE}/{DB_SERVICE:.3f} = "
          f"{POOL_SIZE / DB_SERVICE:.0f} queries per second. Warm, the miss "
          f"rate needs {OFFERED_RPS * EVICT_PER_SEC / OFFERED_RPS:.0f} of them "
          f"({100 * EVICT_PER_SEC / (POOL_SIZE / DB_SERVICE):.0f}% utilised).")
    print(f"Cold, it needs all {OFFERED_RPS:.0f} -- "
          f"{OFFERED_RPS / (POOL_SIZE / DB_SERVICE):.0f}x capacity, before a "
          f"single retry. Client timeout {CLIENT_TIMEOUT * 1000:.0f}ms, "
          f"{ATTEMPTS} attempts, no jitter, no budget, no shedding.")
    print(f"FLUSHALL at t={TRIGGER_AT:.0f}s. Escapes, where a scenario has "
          f"one, at t={ESCAPE_AT:.0f}s.")

    scenarios = [
        ("0 no escape: remove the trigger and wait",
         "The trigger was over in a millisecond. Watch the next 24 seconds.", ""),
        ("a drop offered load to zero, then ramp it back slowly",
         f"The one nobody wants to authorise. {DROP_SECONDS:.0f}s of zero, "
         f"then {RAMP_BACK_SECONDS:.0f}s of ramp. Watch the ramp, not the drop.", "a"),
        ("b enable topic 3's 10% retry budget, load unchanged",
         "Removes the amplification. Does not remove the sustaining effect.", "b"),
        ("c enable topic 5's load shedder, load unchanged",
         f"Admit at most {SHED_LIMIT} in flight; 503 the rest, immediately.", "c"),
        ("d restart the app, load unchanged",
         "Clears the queue, the in-flight work and the pool. Not the cache.", "d"),
    ]
    results = []
    for title, note, escape in scenarios:
        m = await run_scenario(escape)
        before, after = render(title, note, m)
        results.append((title, before, after))

    print("\n" + "=" * 78)
    print(f"{'scenario':<52}{'goodput before':>15}{'after':>11}")
    print("-" * 78)
    for title, before, after in results:
        print(f"{title:<52}{before:>14.1f}{after:>11.1f}")

    print()
    print("Scenario 0 is the whole topic. The trigger -- one FLUSHALL -- was")
    print("over instantly and reversibly, offered load never changed by a")
    print(f"single request, and goodput half a minute later is "
          f"{results[0][2]:.1f} rps -- which")
    print("is what THIS run measured, not a sentence written before it. If it")
    print("is not near zero, read the README's 'what would mean the experiment")
    print("is broken' before reading anything else. Nothing is broken. Nothing")
    print("needs rolling back. The system has simply settled into a second")
    print("stable state, where the cache cannot refill because the database is")
    print("saturated and the database is saturated because the cache is empty.")
    print()
    print("Escapes, judged against THIS run rather than against a story:")
    for title, before, after in results[1:]:
        print(f"  {title[:2]} {verdict(before, after)}")
    print(f"  (scenario 0 finished at {results[0][2]:.1f} rps of goodput, "
          f"for comparison)")
    print()
    print("What each escape actually touches, which is why they do not rank the")
    print("way intuition ranks them:")
    print("  (a) drop and ramp    removes load, not the loop. The drop always")
    print("      works -- the queue empties, in-flight goes to nothing -- and")
    print("      then the RAMP is the experiment: full load returning to a cache")
    print("      that is still empty walks straight back into the same state. So")
    print("      'let it back slowly' is a QUANTITATIVE claim -- the ramp has to")
    print(f"      be slower than the cache can refill, which here is "
          f"{POOL_SIZE / DB_SERVICE:.0f} keys per")
    print(f"      second against {KEYS} keys. Raise RAMP_BACK_SECONDS from "
          f"{RAMP_BACK_SECONDS:.0f} and find the")
    print("      threshold yourself; the number you find is the real answer to")
    print("      'how slowly?', and it is not a matter of taste.")
    print("  (b) retry budget     removes topic 3's amplification and leaves the")
    print("      sustaining effect untouched. 'We turned the retries off' is a")
    print("      sentence people say in incidents that are still ongoing twenty")
    print("      minutes later.")
    print("  (c) load shedding    is the one that breaks the FEEDBACK LOOP: it is")
    print("      the only intervention that lets the ADMITTED requests finish")
    print("      inside their deadline, which is the exact condition the cache")
    print("      needs to refill. Watch its `hit%` column climb while `retry/req`")
    print("      falls -- that is the feedback loop running backwards.")
    print("  (d) restart the app  clears everything the process owns and nothing")
    print("      the clients own. The amplifier is in the clients. They did not")
    print("      restart.")
    print()
    print("In HotOS '25 vocabulary, and worth writing down in three sentences")
    print("for your own system before you need it:")
    print("  trigger                 a cache flush, over in one millisecond")
    print("  amplification mechanism naive retries, plus the miss rate going")
    print("                          from 10% to 100% -- a 10x rise in load on")
    print("                          a database that was 60% utilised")
    print("  sustaining effect       fills only happen on completions that beat")
    print("                          the caller's deadline, and under overload")
    print("                          none of them do")
    print()
    print("And the operational sentence that follows from all five scenarios:")
    print("the escape has to break the FEEDBACK LOOP, not the trigger and not")
    print("the amplifier. Scenario (c) is the only one that does, which is why")
    print("topic 5 exists and why it is worth building before you need it.")


if __name__ == "__main__":
    asyncio.run(main())
