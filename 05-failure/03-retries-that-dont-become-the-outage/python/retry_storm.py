"""
Layer 5 - Topic 3: retry amplification, in one Python process.

Python is where this topic's ecosystem lesson is sharpest: the language
gives you the backoff and never the budget. `tenacity` ships
`wait_exponential_jitter`; `urllib3.util.Retry` gained `backoff_jitter` in
2.x, which older code almost certainly does not set, so a `requests`
HTTPAdapter configured a few years ago is retrying in synchronised waves
right now; and `httpx` has no retry logic beyond connection-level retries,
which is honest -- it makes you supply the policy. Nothing in the ecosystem
ships a retry BUDGET. It is about thirty lines, it is in this file, and it
is the highest-value thirty lines in the topic.

WHAT THIS DEMONSTRATES
  A three-hop chain, gateway -> service_b -> service_c, each hop retrying
  up to 3 times. C's database fails hard for a window in the middle of the
  run. The leaf counter counts DATABASE CALLS, so the theoretical worst
  case is 3 hops x 3 attempts = 27x the offered rate.

  Four variants:
    A naive        exponential backoff, no jitter, no budget
    B + jitter     full jitter: sleep = random(0, min(cap, base * 2**n))
    C + budget     a 10% token bucket at every hop, Envoy-style
    D edge only    only the hop adjacent to the database retries, and it
                   marks the error non-retryable on the way up

WHAT TO LOOK FOR IN THE OUTPUT
  1. The `amp` column during the fault window. It will not reach 27x, and
     the reason it does not is worth more than the number: the per-attempt
     timeout and the caller's budget both expire before the deepest
     retries can be attempted.
  2. What amp does AFTER the fault clears -- the column that matters.
     Once the retries have built a queue, the queue causes the next round
     of retries, and that loop can sustain itself with the fault long
     gone. Read YOUR run: `mean amp 16s onward` and `success after` are
     the two numbers, and this file is not going to promise you which way
     they land. The chain is BISTABLE at these constants -- 150 rps
     offered against 200 rps of leaf capacity -- so whether the backlog is
     small enough to work off when the fault clears decides it. Rerunning,
     or running the same policy in another language in this folder, can
     land in the other basin. That is the finding, not flakiness, and it
     is topic 4 arriving early and uninvited. Variant C is the one that is
     not bistable; look at it before concluding anything about runtimes.
  3. Variant B's peak versus variant A's. Jitter changes the SHAPE, not
     the area: it spreads a synchronised cohort across the whole interval
     instead of stacking it at one instant.
  4. Variant C: retry traffic goes to zero automatically as failures
     climb, without anybody deciding it should. That is the property, and
     it is the only one of the four that bounds load rather than delaying
     it.
  5. Variant D's peak is roughly the attempts of ONE hop rather than the
     product of three.

RUN
    python3 retry_storm.py
"""
from __future__ import annotations

import asyncio
import random
import time

# ---------------------------------------------------------------- config

OFFERED_RPS = 150.0
DURATION = 24.0
FAULT_ON = 5.0            # database starts refusing connections
FAULT_OFF = 12.0          # ... and stops. The interesting part is after this.
BUCKET = 2.0              # reporting interval

ATTEMPTS = 3              # per hop, total attempts including the first
BASE_BACKOFF = 0.050
BACKOFF_CAP = 0.400
ATTEMPT_TIMEOUT = 0.300   # one attempt's patience
REQUEST_BUDGET = 1.500    # topic 2: the whole request's budget, all hops

LEAF_POOL = 8             # the leaf's connection pool
LEAF_SERVICE = 0.040      # 8 / 0.040 = 200 rps of real capacity

BUDGET_RATIO = 0.10       # Envoy's budget_percent, as a fraction
BUDGET_MIN_TOKENS = 3.0   # Envoy's min_retry_concurrency floor


# ------------------------------------------------------------ retry budget


class RetryBudget:
    """
    A token bucket that permits retries only while retries stay under some
    fraction of successes. Envoy calls this budget_percent (default 20%
    with a min_retry_concurrency floor of 3), gRPC calls it retryThrottling,
    Yandex reported settling on 10%. Nothing in the Python ecosystem ships
    one, which is why it is written out here in full.

    The property that matters is qualitative rather than numeric: at low
    failure rates this behaves exactly like a normal retrying client, and
    as failures climb its retry traffic goes to ZERO automatically. Backoff
    delays amplification. Only this bounds it.
    """

    def __init__(self, ratio: float = BUDGET_RATIO, floor: float = BUDGET_MIN_TOKENS):
        self.ratio = ratio
        self.floor = floor
        self.tokens = floor
        self.denied = 0

    def deposit(self) -> None:
        # Refill on SUCCESSES, not on wall-clock. A bucket that refills with
        # time gives a service with no traffic free retries it never earned,
        # and hands a service in total outage a steady drip of amplification
        # forever.
        self.tokens = min(self.tokens + self.ratio, self.floor + 100)

    def withdraw(self) -> bool:
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        self.denied += 1
        return False


# ------------------------------------------------------------- the leaf


class Unavailable(Exception):
    """Retryable: the dependency refused or timed out."""


class NonRetryable(Exception):
    """Do not try again. Variant D's whole mechanism is raising this."""


class Leaf:
    """
    The database at the bottom of the chain, with a real bounded pool, and
    a fault window during which it refuses connections outright.
    """

    def __init__(self, metrics: "Metrics") -> None:
        self.sem = asyncio.Semaphore(LEAF_POOL)
        self.m = metrics
        self.faulty = False

    async def call(self) -> None:
        # THE COUNTER THAT MATTERS. Requests RECEIVED, not requests
        # succeeded. Divided by the client's offered rate it is the live
        # amplification factor, and it is the one number in this topic
        # worth putting on a dashboard.
        self.m.leaf_received += 1

        if self.faulty:
            # Connection refused: fast, cheap, and therefore the worst kind
            # of failure for a retrying client, because the retry arrives
            # almost immediately.
            raise Unavailable("connection refused")

        async with self.sem:
            await asyncio.sleep(LEAF_SERVICE)


# ------------------------------------------------------------ the policy


async def with_retries(call, budget: RetryBudget | None, jitter: bool,
                       deadline: float, m: "Metrics"):
    """
    One hop's retry loop. This is the shape `tenacity` gives you, minus the
    decorator and plus the two pieces it does not have: a deadline it
    refuses to outlive, and a budget.
    """
    delay = BASE_BACKOFF
    last: Exception = Unavailable("never attempted")

    for attempt in range(ATTEMPTS):
        if attempt > 0:
            # (4) The budget. Checked BEFORE the sleep, so a denied retry
            # costs nothing at all -- not even the wait.
            if budget is not None and not budget.withdraw():
                m.budget_denied += 1
                raise last
            m.retries += 1

            if jitter:
                # Full jitter, the AWS Builders' Library recommendation.
                # Spreads a synchronised cohort across the WHOLE interval
                # rather than around a common centre, which is the entire
                # point -- see how differently variants A and B fail.
                sleep_for = random.uniform(0, min(BACKOFF_CAP, delay))
            else:
                sleep_for = min(BACKOFF_CAP, delay)
            delay *= 2

            # (3) A hard cap that fits inside the caller's budget. A retry
            # policy allowed to outlive its caller's deadline is generating
            # topic 2's zombie work on purpose.
            if time.perf_counter() + sleep_for > deadline:
                raise last
            await asyncio.sleep(sleep_for)

        if time.perf_counter() >= deadline:
            raise last
        try:
            remaining = min(ATTEMPT_TIMEOUT, deadline - time.perf_counter())
            result = await asyncio.wait_for(call(), timeout=remaining)
            if budget is not None:
                budget.deposit()
            return result
        except NonRetryable:
            # (1) The failure is not transient. Retrying it is pure waste,
            # and variant D uses this path deliberately.
            raise
        except (Unavailable, TimeoutError) as e:
            last = Unavailable(str(e) or "timeout")

    raise last


# -------------------------------------------------------------- the chain


class Chain:
    def __init__(self, leaf: Leaf, m: "Metrics", jitter: bool,
                 budgeted: bool, edge_only: bool) -> None:
        self.leaf = leaf
        self.m = m
        self.jitter = jitter
        self.edge_only = edge_only
        # One bucket per hop, shared across every request that hop handles.
        # Per-request state would defeat the entire idea: the budget exists
        # to make one client's retries visible to the next client's.
        self.budgets = [RetryBudget() if budgeted else None for _ in range(3)]

    async def service_c(self, deadline: float) -> None:
        # The hop adjacent to the failure. In every variant this one retries.
        try:
            await with_retries(self.leaf.call, self.budgets[2], self.jitter,
                               deadline, self.m)
        except Unavailable as e:
            if self.edge_only:
                # THE STRUCTURAL FIX. The hop next to the failure has already
                # spent its attempts; telling the truth about that upward
                # turns the worst case from 3**3 back into 3. It composes
                # cleanly with topic 2, and it is easier to reason about than
                # any amount of tuning.
                raise NonRetryable(str(e)) from e
            raise

    async def service_b(self, deadline: float) -> None:
        await with_retries(lambda: self.service_c(deadline), self.budgets[1],
                           self.jitter, deadline, self.m)

    async def gateway(self) -> None:
        deadline = time.perf_counter() + REQUEST_BUDGET
        try:
            await with_retries(lambda: self.service_b(deadline), self.budgets[0],
                               self.jitter, deadline, self.m)
            self.m.ok += 1
        except Exception:
            self.m.failed += 1


# ------------------------------------------------------------- reporting


class Metrics:
    def __init__(self) -> None:
        self.leaf_received = 0
        self.ok = 0
        self.failed = 0
        self.retries = 0
        self.budget_denied = 0
        self.samples: list[tuple[float, float, float, float]] = []


async def run_variant(label: str, jitter: bool, budgeted: bool,
                      edge_only: bool) -> Metrics:
    m = Metrics()
    leaf = Leaf(m)
    chain = Chain(leaf, m, jitter, budgeted, edge_only)
    rng = random.Random(20250503)
    random.seed(20250503)      # jitter draws too, so variants stay comparable

    loop = asyncio.get_running_loop()
    begin = loop.time()
    end = begin + DURATION

    last_bucket = begin
    last_received = 0
    last_ok = 0
    last_total = 0

    at = begin
    tasks: list[asyncio.Task] = []
    while True:
        at += rng.expovariate(OFFERED_RPS)
        if at > end:
            break
        delay = at - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)

        t = loop.time() - begin
        leaf.faulty = FAULT_ON <= t < FAULT_OFF

        tasks.append(asyncio.create_task(chain.gateway()))

        if loop.time() - last_bucket >= BUCKET:
            span = loop.time() - last_bucket
            received = (m.leaf_received - last_received) / span
            done = (m.ok + m.failed) - last_total
            ok = m.ok - last_ok
            m.samples.append((
                t,
                received,
                received / OFFERED_RPS,
                100.0 * ok / done if done else 0.0,
            ))
            last_bucket = loop.time()
            last_received = m.leaf_received
            last_ok = m.ok
            last_total = m.ok + m.failed

    await asyncio.gather(*tasks, return_exceptions=True)
    return m


def render(label: str, m: Metrics) -> None:
    print(f"\n=== {label} ===")
    print("     t   leaf rps      amp   success                 amplification")
    peak = max((s[2] for s in m.samples), default=0.0)
    scale = max(peak, 1.0)
    for t, received, amp, success in m.samples:
        bar = "#" * max(0, round(34 * amp / scale))
        fault = " FAULT" if FAULT_ON <= t < FAULT_OFF else "      "
        print(f"  {t:5.1f} {received:10.1f} {amp:8.2f} {success:8.1f}%{fault} |{bar}")
    after = [s for s in m.samples if s[0] >= FAULT_OFF + 4]
    tail = sum(s[2] for s in after) / len(after) if after else 0.0
    tail_success = sum(s[3] for s in after) / len(after) if after else 0.0
    print(f"  peak amp {peak:.2f}x   mean amp {FAULT_OFF + 4:.0f}s onward "
          f"{tail:.2f}x   success after {tail_success:.1f}%   "
          f"retries {m.retries}   budget-denied {m.budget_denied}")


def synchronised_cohort() -> None:
    """
    Why the table above makes jitter look useless, and why it is not.

    In the sweep, arrivals are a Poisson process: every client fails at a
    different moment already, so their retries were never going to collide.
    Jitter has nothing to decorrelate, and full jitter's shorter average
    wait actually lets MORE attempts fit inside the budget -- which is why
    variant B can amplify harder than variant A.

    Production is not that. Production is a thousand clients that were all
    talking to the same dependency when it fell over at the same instant.
    Below, 1000 clients fail simultaneously and we bucket the arrival time
    of their first retry. Nothing is measured here; it is arithmetic on the
    two formulas, and it is the whole answer to "why jitter".
    """
    rng = random.Random(20250503)
    clients = 1000
    delay = min(BACKOFF_CAP, BASE_BACKOFF * 2)      # everyone's second attempt

    def histogram(title: str, draw) -> None:
        buckets = [0] * 10
        width = BACKOFF_CAP / len(buckets)
        for _ in range(clients):
            b = min(int(draw() / width), len(buckets) - 1)
            buckets[b] += 1
        print(f"\n  {title}")
        for i, count in enumerate(buckets):
            lo = i * width * 1000
            hi = (i + 1) * width * 1000
            bar = "#" * round(48 * count / clients)
            print(f"   {lo:5.0f}-{hi:<5.0f}ms |{bar} {count}")
        print(f"   peak instantaneous retry rate: {max(buckets) / width:.0f} rps "
              f"from {clients} clients")

    print("\n" + "=" * 78)
    print("Why the table above makes jitter look pointless: 1000 clients, one")
    print("simultaneous failure, arrival times of their first retry.")
    histogram("no jitter -- sleep = min(cap, base * 2**n)", lambda: delay)
    histogram("full jitter -- sleep = random(0, min(cap, base * 2**n))",
              lambda: rng.uniform(0, delay))
    print("\n  Same number of retries either way. Jitter does not reduce the")
    print("  area, it reduces the PEAK, and the peak is what a service trying")
    print("  to recover actually has to survive. The benefit is about")
    print("  correlation, not about randomness, which is exactly why it is")
    print("  invisible in a single-process test with independent arrivals.")


async def main() -> None:
    print("Retry amplification through gateway -> service_b -> service_c -> database.")
    print(f"Offered {OFFERED_RPS:.0f} rps for {DURATION:.0f}s, database refuses "
          f"connections from t={FAULT_ON:.0f}s to t={FAULT_OFF:.0f}s.")
    print(f"{ATTEMPTS} attempts per hop over 3 hops = {ATTEMPTS ** 3}x worst case "
          f"at the leaf; the leaf's real capacity is "
          f"{LEAF_POOL}/{LEAF_SERVICE:.3f} = {LEAF_POOL / LEAF_SERVICE:.0f} rps.")
    print("amp = database calls per second / offered rps. Watch what it does "
          "AFTER the fault clears.")

    variants = [
        ("A naive: exponential backoff, no jitter", False, False, False),
        ("B + full jitter", True, False, False),
        ("C + 10% retry budget at every hop", True, True, False),
        ("D retry at the edge only", True, False, True),
    ]
    summary = []
    for label, jitter, budgeted, edge in variants:
        m = await run_variant(label, jitter, budgeted, edge)
        render(label, m)
        peak = max((s[2] for s in m.samples), default=0.0)
        after = [s for s in m.samples if s[0] >= FAULT_OFF + 4]
        summary.append((
            label,
            peak,
            sum(s[2] for s in after) / len(after) if after else 0.0,
            sum(s[3] for s in after) / len(after) if after else 0.0,
        ))

    print("\n" + "=" * 78)
    print(f"{'variant':<44}{'peak amp':>10}{'amp after':>11}{'success after':>14}")
    print("-" * 78)
    for label, peak, tail, tail_success in summary:
        print(f"{label:<44}{peak:>9.2f}x{tail:>10.2f}x{tail_success:>13.1f}%")

    print()
    print("The 27x worst case does not appear, and the reason is the useful")
    print("part: the per-attempt timeout and the request budget from topic 2")
    print("expire before the deepest retries can be attempted. Timeouts cap")
    print("amplification by accident. Do not rely on an accident.")
    print()
    print("Compare A and B in the AFTER column rather than at the peak. Jitter")
    print("changes the shape of the wave; it does not change how much water")
    print("there is. Past a certain outage length backoff has spread retries as")
    print("far as its cap allows and steady-state amplification returns -- you")
    print("converge on attempts/backoff_cap extra requests per second, forever.")
    print()
    print("Variant B amplifying HARDER than A is not a bug in the experiment.")
    print("Arrivals here are a Poisson process, so nothing was synchronised for")
    print("jitter to decorrelate -- and full jitter's shorter average wait lets")
    print("more attempts fit inside the same budget. Keep reading for what")
    print("jitter is actually for.")
    print()
    print("C is the only variant whose retry traffic falls as failures climb,")
    print("and it is the only one that is a bound rather than a delay. D gets")
    print("most of the same benefit structurally, by making sure the answer to")
    print("'which layer owns retries' is a single layer.")

    synchronised_cohort()


if __name__ == "__main__":
    asyncio.run(main())
