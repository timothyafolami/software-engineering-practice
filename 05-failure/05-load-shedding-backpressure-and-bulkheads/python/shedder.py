"""
Layer 5 - Topic 5: load shedding, backpressure and bulkheads, in one Python
process.

You cannot serve more than capacity. The only choice you have is whether the
excess is rejected in one millisecond or times out after thirty seconds
having consumed a connection, a thread and a query. This file runs the same
ramp seven times and changes only the admission decision.

PYTHON'S ADMISSION STORY, precisely, because the crude version is worth
knowing exactly:
  * `uvicorn --limit-concurrency N` returns an immediate 503 with NO
    queueing above N. Its default is None -- no limit at all.
  * It applies only AFTER the connection has been accepted, so the kernel
    accept queue (`--backlog`, default 2048) fills first, and everything
    sitting in it is invisible to your metrics while it burns the caller's
    budget. The queue you did not configure is the one that hurts you.
  * Anything smarter is middleware you write: an `asyncio.Semaphore` sized
    to the knee you measured in topic 1, a wait deadline on acquisition, a
    503 with `Retry-After`, and a priority tier derived from the route or
    the caller. That is what `Admission` below is, and it is about fifty
    lines.

WHAT THIS DEMONSTRATES
  A backend with 8 concurrent servers at 40ms each -- 200 requests/second of
  capacity, measured the way topic 1 measures it -- behind six different
  admission policies, at 80% and 130% of that capacity.

    none rho=0.8      the healthy baseline. Everything looks fine.
    none rho=1.3      an UNBOUNDED queue. Nothing is rejected, everything is
                      accepted, and p99 leaves the building. This is the
                      latency bomb: an availability problem converted into a
                      latency problem and hidden until it exceeds every
                      timeout in the system at once.
    static rho=1.3    a semaphore sized to the measured knee plus a 50ms
                      queue-wait deadline -> 503.
    priority rho=1.3  the same limit, but /checkout (tier 0) may use all of
                      it and /search (tier 3) may not. Shed the same USERS
                      everywhere rather than giving everyone a partly broken
                      experience.
    adaptive rho=1.3  no configured number at all: a gradient controller
                      infers the limit from latency, TCP-congestion-control
                      style. Service time triples half way through, on
                      purpose.
    bulkhead          one pool of 8 shared between checkout and a slow
                      /report endpoint, then the SAME EIGHT split 6 + 2.
                      Nothing is added; the boundary is the whole change.

WHAT TO LOOK FOR IN THE OUTPUT
  1. `p99_acc` in `none rho=1.3` against `static rho=1.3`. The claim under
     test is that p99 of ACCEPTED requests stays roughly flat past 100%
     offered while rejections absorb the excess.
  2. `goodput` in those same two rows. Rejecting work should INCREASE the
     number of requests answered in time, which is the counter-intuitive
     part and the one to check rather than believe.
  3. `tier0%` in the priority row: tier 0 keeps its success rate while tier
     3 absorbs every rejection.
  4. `limit` in the adaptive row, before and after the service time triples
     at t=6s. Reason about Little's law before you decide the controller is
     broken: L = lambda * W, and the ideal in-flight limit for 8 servers is
     about 8 no matter how long each request takes. What must fall is the
     RATE, not the limit -- so what you should see is the limit dipping
     while min_rtt is stale and returning once it re-baselines.
  5. `reject_ms`, the cost of saying no. If a 503 runs authentication, a
     database lookup and full serialisation, you saved nothing.

RUN
    python3 shedder.py

Roughly two and a half minutes: seven scenarios of twenty seconds.
"""
from __future__ import annotations

import asyncio
import math
import random
import time

# ---------------------------------------------------------------- config

WORKERS = 8                # the real resource: 8 concurrent servers
SERVICE_S = 0.040          # 8 / 0.040 = 200 rps of capacity
CAPACITY = WORKERS / SERVICE_S

RHO_LOW = 0.8
RHO_HIGH = 1.3

SLO_S = 0.500              # a response later than this is not goodput
DURATION_S = 20.0        # PERTURB_AT_S + MIN_RTT_RESET_S + room to watch
                           # the adaptive limit come back. At 12s the run
                           # ended during the dip and the return -- the half
                           # that shows the reset working -- was invisible.
REPORT_EVERY = 2.0

SHED_LIMIT = 12            # in-flight limit: the knee's concurrency, which is
                           # the pool plus a little queue. Derive it from
                           # topic 1's MEASURED knee, never from a guess.
SHED_WAIT_S = 0.050        # queue-wait deadline before a 503 + Retry-After
TIER3_LIMIT = 10           # priority mode: tier 3 may not use the last two
TIER0_SHARE = 0.20         # /checkout is a fifth of the traffic

ADAPT_MIN = 2.0            # gradient controller bounds
ADAPT_MAX = 64.0
ADAPT_START = 10.0
ADAPT_WINDOW_S = 0.25      # how often the controller may change its mind
ADAPT_SMOOTHING = 0.2
MIN_RTT_RESET_S = 5.0      # re-baseline, or a stale minimum drives the limit
                           # to the floor forever after a genuine slowdown
PERTURB_AT_S = 6.0         # ... which is exactly what happens here
PERTURB_FACTOR = 3.0

CHECKOUT_RPS = 120.0       # bulkhead scenarios
REPORT_RPS = 6.0
REPORT_SERVICE_S = 0.800   # 6 rps x 0.8s = 4.8 servers' worth of demand
BULKHEAD_CHECKOUT_WORKERS = 6   # the same 8, split. Nothing is added.
BULKHEAD_REPORT_WORKERS = 2


# ----------------------------------------------------------- the backend

class Backend:
    """The resource being protected. `asyncio.Semaphore` with no admission
    control in front of it is an UNBOUNDED queue: every waiter is a pending
    future the runtime is happy to hold, and nothing tells the producer to
    stop. That is mode `none`, and it is also every service anybody ships by
    accident."""

    def __init__(self, workers: int) -> None:
        self.sem = asyncio.Semaphore(workers)
        self.in_use = 0
        self.queued = 0

    async def call(self, service_s: float) -> None:
        self.queued += 1
        try:
            async with self.sem:
                self.queued -= 1
                self.in_use += 1
                try:
                    await asyncio.sleep(service_s)
                finally:
                    self.in_use -= 1
                    return
        finally:
            pass


# ------------------------------------------------------ the gradient limit

class GradientLimit:
    """Netflix `concurrency-limits` in miniature, and the idea is borrowed
    from TCP congestion control rather than from queueing theory: sample
    latency continuously, remember the minimum you have seen, and raise the
    in-flight limit while current latency stays near that minimum, lower it
    when latency climbs.

    You never configure a number. The system discovers it, and rediscovers
    it when your code changes -- which matters because the hand-measured
    number from topic 1 goes stale the day someone adds a join.

    The one parameter that is not obvious is the min-RTT RESET. Without it a
    single fast sample from a quiet moment is remembered forever, so after a
    genuine, permanent slowdown the gradient is stuck near zero and the limit
    collapses to the floor and stays there. Vegas-style controllers all
    re-baseline; this one does it every MIN_RTT_RESET_S."""

    def __init__(self) -> None:
        self.limit = ADAPT_START
        self.min_rtt = math.inf
        self.samples: list[float] = []
        self.last_update = 0.0
        self.last_reset = 0.0

    def observe(self, rtt: float) -> None:
        self.samples.append(rtt)

    def update(self, now: float) -> None:
        if now - self.last_update < ADAPT_WINDOW_S:
            return
        self.last_update = now
        if not self.samples:
            return
        window_min = min(self.samples)
        self.samples.sort()
        median = self.samples[len(self.samples) // 2]
        self.samples.clear()

        if now - self.last_reset >= MIN_RTT_RESET_S or self.min_rtt is math.inf:
            self.min_rtt = window_min
            self.last_reset = now
        else:
            self.min_rtt = min(self.min_rtt, window_min)

        # gradient < 1 means "we are queueing"; the limit comes down in
        # proportion. The sqrt term is the allowance for a queue you are
        # willing to keep -- it is what stops the limit collapsing to 1 the
        # moment a single request is slow.
        gradient = max(0.5, min(1.0, self.min_rtt / max(median, 1e-6)))
        target = self.limit * gradient + math.sqrt(self.limit)
        self.limit = max(ADAPT_MIN, min(ADAPT_MAX,
                                        self.limit * (1 - ADAPT_SMOOTHING)
                                        + ADAPT_SMOOTHING * target))


# ---------------------------------------------------------- the admission

class Admission:
    """The fifty lines. Everything above the backend and below the router.

    `asyncio.Semaphore` is the primitive; the interesting part is what
    happens when you cannot have a permit immediately, and there are exactly
    three honest answers: wait a BOUNDED time (static, tier 0), refuse now
    (priority's tier 3, adaptive), or wait forever (mode `none`, which is
    what you ship when you do not decide)."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.sem = asyncio.Semaphore(SHED_LIMIT)
        self.inflight = 0
        self.limiter = GradientLimit() if mode == "adaptive" else None

    async def admit(self, tier: int) -> tuple[bool, float]:
        """Returns (admitted, seconds spent deciding). The second element is
        the cost of a rejection, and it is a number worth putting on a
        dashboard: a shedder that takes 50ms to say no has spent 10% of a
        500ms budget on nothing."""
        t0 = time.perf_counter()

        if self.mode == "none":
            # No admission control at all. Every request is accepted, waits
            # in the backend's queue for as long as that takes, and the queue
            # has no bound because nobody gave it one.
            self.inflight += 1
            return True, 0.0

        if self.mode == "adaptive":
            # Limit-based, no queueing: the controller's whole job is to keep
            # the limit at the value where waiting is unnecessary.
            if self.inflight >= self.limiter.limit:
                return False, time.perf_counter() - t0
            self.inflight += 1
            return True, time.perf_counter() - t0

        if self.mode == "priority" and tier > 0:
            # Tier 3 gets `try_acquire` semantics against a LOWER limit: the
            # last four permits are reserved for tier 0, and tier 3 does not
            # get to queue for them. Shedding the same users everywhere beats
            # giving everybody a service that half works.
            if self.inflight >= TIER3_LIMIT:
                return False, time.perf_counter() - t0
            self.inflight += 1
            return True, time.perf_counter() - t0

        # static, and priority's tier 0: a BOUNDED wait. This is the CoDel
        # idea in its simplest form -- reject on how long you have waited,
        # not on how many are waiting, because length tells you nothing about
        # how long anything takes.
        try:
            await asyncio.wait_for(self.sem.acquire(), SHED_WAIT_S)
        except TimeoutError:
            return False, time.perf_counter() - t0
        self.inflight += 1
        return True, time.perf_counter() - t0

    def release(self, admitted_via_sem: bool) -> None:
        self.inflight -= 1
        if admitted_via_sem:
            self.sem.release()

    def uses_sem(self, tier: int) -> bool:
        if self.mode in ("none", "adaptive"):
            return False
        if self.mode == "priority" and tier > 0:
            return False
        return True


# ------------------------------------------------------------- the metrics

class Metrics:
    def __init__(self) -> None:
        self.offered = 0
        self.accepted = 0
        self.rejected = 0
        self.goodput = 0
        self.latencies: list[float] = []
        self.lat_tier0: list[float] = []
        self.reject_cost: list[float] = []
        self.tier0_offered = 0
        self.tier0_goodput = 0
        self.rows: list[tuple] = []
        self.window = self._blank()

    @staticmethod
    def _blank() -> dict:
        return {"offered": 0, "accepted": 0, "rejected": 0, "goodput": 0,
                "lat": []}


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(math.ceil(q * len(ordered))) - 1))
    return ordered[idx]


# ------------------------------------------------------------ the server

class Server:
    def __init__(self, mode: str, m: Metrics) -> None:
        self.mode = mode
        self.m = m
        self.admission = Admission("none" if mode.startswith("bulkhead") else mode)
        self.checkout_backend = Backend(
            BULKHEAD_CHECKOUT_WORKERS if mode == "bulkhead_split" else WORKERS)
        # The bulkhead. In `bulkhead_shared` the slow endpoint uses the SAME
        # object as checkout, so a report and a checkout compete for one
        # connection; in `bulkhead_split` it has its own, smaller pool and is
        # structurally incapable of touching checkout's connections.
        self.report_backend = (Backend(BULKHEAD_REPORT_WORKERS)
                               if mode == "bulkhead_split"
                               else self.checkout_backend)
        self.service_s = SERVICE_S

    async def handle(self, tier: int, is_report: bool) -> None:
        t0 = time.perf_counter()
        self.m.offered += 1
        self.m.window["offered"] += 1
        if tier == 0:
            self.m.tier0_offered += 1

        via_sem = self.admission.uses_sem(tier)
        admitted, cost = await self.admission.admit(tier)
        if not admitted:
            self.m.rejected += 1
            self.m.window["rejected"] += 1
            self.m.reject_cost.append(cost)
            # A 503 with Retry-After, in one millisecond, having touched
            # nothing. That is the entire product.
            return

        self.m.accepted += 1
        self.m.window["accepted"] += 1
        try:
            backend = self.report_backend if is_report else self.checkout_backend
            service = REPORT_SERVICE_S if is_report else self.service_s
            await backend.call(service)
        finally:
            self.admission.release(via_sem)

        latency = time.perf_counter() - t0
        self.m.latencies.append(latency)
        if tier == 0:
            self.m.lat_tier0.append(latency)
        self.m.window["lat"].append(latency)
        if self.admission.limiter is not None:
            self.admission.limiter.observe(latency)
        if latency <= SLO_S:
            self.m.goodput += 1
            self.m.window["goodput"] += 1
            if tier == 0:
                self.m.tier0_goodput += 1


# ------------------------------------------------------------- the harness

class Scenario:
    def __init__(self, key: str, mode: str, label: str, note: str,
                 rate: float, tier0_share: float = TIER0_SHARE,
                 report_rps: float = 0.0) -> None:
        self.key = key
        self.mode = mode
        self.label = label
        self.note = note
        self.rate = rate
        self.tier0_share = tier0_share
        self.report_rps = report_rps


async def run_scenario(sc: Scenario) -> Metrics:
    m = Metrics()
    server = Server(sc.mode, m)
    rng = random.Random(20250505)
    loop = asyncio.get_running_loop()
    begin = loop.time()
    last_report = begin
    at = begin
    next_report_req = begin
    tasks: list[asyncio.Task] = []
    perturbed = False

    while True:
        t = at - begin
        if t > DURATION_S:
            break
        at += rng.expovariate(sc.rate)
        delay = at - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        now = loop.time()
        t = now - begin

        if sc.mode == "adaptive" and not perturbed and t >= PERTURB_AT_S:
            # "Then change service time by 3x at runtime and watch it
            # re-converge." Nobody redeployed. Nobody changed the limit.
            server.service_s = SERVICE_S * PERTURB_FACTOR
            perturbed = True

        tier = 0 if rng.random() < sc.tier0_share else 3
        tasks.append(asyncio.create_task(server.handle(tier, False)))

        # The slow endpoint, offered as its own open-model stream rather than
        # as a fraction of checkout: reports do not arrive because checkouts
        # do. Note `+=` and the `while`, not `= now +` and an `if`: this is
        # an ABSOLUTE schedule, exactly like `at` above. Rescheduling from
        # `now` throws away the lateness of every arrival, and since the
        # check only runs when a checkout arrives, the lateness is real and
        # it grows with load -- so the relative version quietly offers LESS
        # /report the more overloaded the server gets, which is backwards
        # and hides the very effect this scenario exists to show.
        while sc.report_rps > 0 and now >= next_report_req:
            next_report_req += rng.expovariate(sc.report_rps)
            tasks.append(asyncio.create_task(server.handle(3, True)))

        if server.admission.limiter is not None:
            server.admission.limiter.update(now)

        if now - last_report >= REPORT_EVERY:
            span = now - last_report
            w = m.window
            limit = (server.admission.limiter.limit
                     if server.admission.limiter is not None else float(SHED_LIMIT))
            m.rows.append((
                t,
                sc.rate,
                w["accepted"] / span,
                100.0 * w["rejected"] / max(1, w["offered"]),
                w["goodput"] / span,
                1000 * percentile(w["lat"], 0.99),
                server.admission.inflight,
                limit,
                server.checkout_backend.in_use,
            ))
            m.window = Metrics._blank()
            last_report = now

    # Let the tail drain: requests still in flight at the end of the window
    # are neither goodput nor rejections, and counting them either way would
    # be a lie about the run.
    await asyncio.sleep(1.0)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return m


# -------------------------------------------------------------- reporting

HEADER = ("      t   offered  accepted  reject%   goodput  p99_acc  inflight  "
          "limit   busy")


def render(sc: Scenario, m: Metrics) -> dict:
    print(f"\n=== {sc.label} ===")
    print(f"    {sc.note}")
    print(HEADER)
    print("-" * len(HEADER))
    for (t, offered, acc, rej, good, p99, inflight, limit, busy) in m.rows:
        mark = ""
        if sc.mode == "adaptive" and abs(t - PERTURB_AT_S) < REPORT_EVERY / 2:
            mark = "  <-- service time x3"
        print(f"  {t:5.1f} {offered:9.1f} {acc:9.1f} {rej:8.0f} {good:9.1f} "
              f"{p99:8.0f} {inflight:9d} {limit:6.1f} {busy:6d}{mark}")

    span = DURATION_S
    out = {
        "key": sc.key,
        "offered": m.offered / span,
        "accepted": m.accepted / span,
        "rejected": 100.0 * m.rejected / max(1, m.offered),
        "goodput": m.goodput / span,
        "p99": 1000 * percentile(m.latencies, 0.99),
        "p99_t0": 1000 * percentile(m.lat_tier0, 0.99),
        "tier0": 100.0 * m.tier0_goodput / max(1, m.tier0_offered),
        "reject_ms": 1000 * (sum(m.reject_cost) / len(m.reject_cost)
                             if m.reject_cost else 0.0),
    }
    print(f"mode={sc.key}  offered={out['offered']:.0f}  "
          f"accepted={out['accepted']:.0f}  rejected={out['rejected']:.0f}%  "
          f"goodput={out['goodput']:.0f}  p99_accepted={out['p99']:.0f}ms  "
          f"tier0_success={out['tier0']:.0f}%  p99_tier0={out['p99_t0']:.0f}ms  "
          f"reject_ms={out['reject_ms']:.1f}")
    return out


async def main() -> None:
    print("Load shedding, backpressure and bulkheads: the same ramp, seven "
          "admission policies.")
    print(f"Backend capacity is {WORKERS}/{SERVICE_S:.3f} = {CAPACITY:.0f} rps, "
          f"measured the way topic 1 measures it. Anything above that is not "
          f"servable by anybody.")
    print(f"Offered load is {RHO_LOW:.1f}x and {RHO_HIGH:.1f}x that number. "
          f"Goodput counts responses inside a {SLO_S * 1000:.0f}ms SLO; "
          f"p99_acc is the p99 of ACCEPTED requests only.")
    print(f"The static limit is {SHED_LIMIT} in flight with a "
          f"{SHED_WAIT_S * 1000:.0f}ms queue-wait deadline. The adaptive one "
          f"is not configured at all.")

    scenarios = [
        Scenario("none_0.8", "none", "1 none, rho=0.8",
                 "The healthy baseline. Nothing is rejected because nothing "
                 "needs to be.", RHO_LOW * CAPACITY),
        Scenario("none_1.3", "none", "2 none, rho=1.3",
                 "An unbounded queue at 130% of capacity. Watch p99_acc, and "
                 "watch that reject% stays at zero the whole way down.",
                 RHO_HIGH * CAPACITY),
        Scenario("static_1.3", "static", "3 static shedding, rho=1.3",
                 f"A semaphore of {SHED_LIMIT} plus a {SHED_WAIT_S * 1000:.0f}ms "
                 f"wait deadline -> 503 Retry-After.", RHO_HIGH * CAPACITY),
        Scenario("priority_1.3", "priority", "4 priority shedding, rho=1.3",
                 f"/checkout is tier 0 ({TIER0_SHARE * 100:.0f}% of traffic) and "
                 f"may use all {SHED_LIMIT}; /search is tier 3 and may use "
                 f"{TIER3_LIMIT}.", RHO_HIGH * CAPACITY),
        Scenario("adaptive_1.3", "adaptive", "5 adaptive shedding, rho=1.3",
                 f"No configured limit. Service time triples at t="
                 f"{PERTURB_AT_S:.0f}s with nobody redeploying anything.",
                 RHO_HIGH * CAPACITY),
        Scenario("bulk_shared", "bulkhead_shared", "6 bulkhead: one shared pool",
                 f"{CHECKOUT_RPS:.0f} rps of checkout plus {REPORT_RPS:.0f} rps of "
                 f"{REPORT_SERVICE_S * 1000:.0f}ms /report, all {WORKERS} servers "
                 f"shared.", CHECKOUT_RPS, 1.0, REPORT_RPS),
        Scenario("bulk_split", "bulkhead_split", "7 bulkhead: the same 8, split "
                 f"{BULKHEAD_CHECKOUT_WORKERS} + {BULKHEAD_REPORT_WORKERS}",
                 "Nothing is added. /report is now structurally incapable of "
                 "touching checkout's connections.",
                 CHECKOUT_RPS, 1.0, REPORT_RPS),
    ]

    results = []
    for sc in scenarios:
        m = await run_scenario(sc)
        results.append((sc, render(sc, m)))

    print("\n" + "=" * 104)
    print(f"{'mode':<38}{'offered':>8}{'accepted':>9}{'goodput':>8}"
          f"{'p99_acc':>8}{'p99_t0':>8}{'reject%':>9}{'tier0_ok%':>10}{'reject_ms':>10}")
    print("-" * 104)
    for sc, r in results:
        print(f"{sc.label:<38}{r['offered']:>8.0f}{r['accepted']:>9.0f}"
              f"{r['goodput']:>8.0f}{r['p99']:>8.0f}{r['p99_t0']:>8.0f}"
              f"{r['rejected']:>9.0f}{r['tier0']:>10.0f}{r['reject_ms']:>10.1f}")

    by_key = {r["key"]: r for _, r in results}
    print()
    print("Read rows 2 and 3 as one comparison and everything else is "
          "commentary:")
    print(f"  none     rho=1.3   goodput {by_key['none_1.3']['goodput']:6.0f} rps   "
          f"p99 {by_key['none_1.3']['p99']:6.0f} ms   rejected "
          f"{by_key['none_1.3']['rejected']:.0f}%")
    print(f"  static   rho=1.3   goodput {by_key['static_1.3']['goodput']:6.0f} rps   "
          f"p99 {by_key['static_1.3']['p99']:6.0f} ms   rejected "
          f"{by_key['static_1.3']['rejected']:.0f}%")
    print("Same offered load, same backend, same 200 rps of capacity. The only")
    print("difference is that one of them said no.")
    print()
    print("The bulkhead pair is the other comparison worth making, and it is the")
    print("one that adds nothing at all:")
    print(f"  shared pool   checkout goodput {by_key['bulk_shared']['goodput']:6.0f} rps   "
          f"checkout p99 {by_key['bulk_shared']['p99_t0']:6.0f} ms")
    print(f"  split 6 + 2   checkout goodput {by_key['bulk_split']['goodput']:6.0f} rps   "
          f"checkout p99 {by_key['bulk_split']['p99_t0']:6.0f} ms")
    print("The split pool has FEWER servers available to checkout, and the")
    print("boundary is worth more than the two servers it costs -- because /report")
    print(f"at {REPORT_RPS:.0f} rps x {REPORT_SERVICE_S * 1000:.0f}ms wants "
          f"{REPORT_RPS * REPORT_SERVICE_S:.1f} servers' worth of the shared pool and")
    print("takes them from whoever asks last. Note what it costs: /report itself")
    print(f"can now only ever get {BULKHEAD_REPORT_WORKERS / REPORT_SERVICE_S:.1f} rps "
          f"through. That is the bargain, and you should be able to say it out loud")
    print("before you make it.")
    print()
    print("Three things to carry out of this file:")
    print("  1. An unbounded queue does not smooth load. It converts an")
    print("     availability problem into a latency problem and hides it until")
    print("     latency exceeds every timeout in the system at once.")
    print("  2. Shed on WAIT TIME, not on queue length. Length is meaningless")
    print("     without a service time attached to it: the same length is a")
    print("     healthy queue for a 1ms handler and a catastrophe for a 500ms one.")
    print("  3. Rejection has to be cheap, and `reject_ms` above is how you know")
    print("     it is. A 503 that runs auth, a database lookup and full")
    print("     serialisation has saved you nothing at all.")


if __name__ == "__main__":
    asyncio.run(main())
