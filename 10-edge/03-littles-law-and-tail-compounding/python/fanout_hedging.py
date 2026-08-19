"""
Layer 10 - Topic 3(b): tail compounding under fan-out, and what hedging
actually buys.

What this demonstrates
    One logical request that depends on n backend calls is only as fast as
    its slowest call, so a 1% chance of being slow becomes 1 - 0.99^n:
    9.6% at n=10, 63% at n=100. At n=100, your p99 is their p63.

    Three things are computed here, all from the same measured single-call
    distribution:

      1. the independence prediction for fan-out p99 at each n, taken as
         the single-call quantile at 0.99^(1/n) -- which is the same
         statement as 1 - (1-p)^n, rearranged into something you can read
         off a distribution you already have;
      2. what actually happens when the calls are NOT independent, because
         they share a queue, a network, a GC pause or a noisy neighbour;
      3. what hedging at the measured p95 recovers, and what it costs, with
         a hard 5% budget -- a hedge with no budget is a retry storm with
         better manners.

What this is NOT
    This is a Monte Carlo over a service-time distribution, not a
    measurement of a server. It is here because the distributional
    arithmetic is the part people get wrong, and because it gives you the
    prediction to check the real run against. The real run is
    `../lab/scripts/fanout.js` against the `api` service; run it at N=1
    first to get the single-call distribution, then at N=10 and N=20.

What to look for
    - Independent arm: measured tracks predicted closely. That is the
      harness agreeing with the arithmetic, and it is what makes the next
      row interpretable.
    - Correlated arm: measured is WORSE than predicted, and by how much.
      Independence is the optimistic assumption, so the prediction is a
      FLOOR, not an estimate. The gap is the correlation, quantified.
    - Hedging: p99 improves, p50 does not, and the request count rises.
      Hedging is a latency fix, not a capacity fix -- if the backend is
      saturated, hedges add load exactly where there is none to spare.

No dependencies. Runs with no arguments in a few seconds:
    python3 python/fanout_hedging.py
"""

from __future__ import annotations

import random
import statistics

SEED = 20260818
SINGLE_CALL_DRAWS = 200_000
FANOUTS = 40_000
FAN_WIDTHS = (1, 2, 5, 10, 20, 100)
HEDGE_BUDGET = 0.05  # at most 5% of calls may be hedged

# A backend with a real tail: most calls are quick, a tenth are not. Any
# distribution with a tail works; this one is shaped like a service with a
# cache and a slow path behind it.
FAST_MEAN_MS, FAST_SD_MS = 20.0, 3.0
SLOW_MEAN_MS, SLOW_SD_MS = 150.0, 50.0
SLOW_FRACTION = 0.10

# Correlation model: with this probability an entire fan-out lands in a bad
# moment -- a GC pause, a busy neighbour, a queue everybody shares -- and
# every call in it is multiplied. This is the mechanism, not a fudge factor.
CORRELATED_EVENT_P = 0.05
CORRELATED_MULTIPLIER = 3.0


def draw(rng: random.Random) -> float:
    if rng.random() < SLOW_FRACTION:
        return max(1.0, rng.gauss(SLOW_MEAN_MS, SLOW_SD_MS))
    return max(1.0, rng.gauss(FAST_MEAN_MS, FAST_SD_MS))


def quantile(sorted_values: list[float], q: float) -> float:
    idx = min(len(sorted_values) - 1, int(q * len(sorted_values)))
    return sorted_values[idx]


def independence_prediction(single_sorted: list[float], n: int) -> float:
    """Fan-out p99 if the n calls were independent.

    P(max <= T) = P(one <= T)^n, so the 99th percentile of the max is the
    single-call quantile at 0.99^(1/n). At n=100 that is the single call's
    p99.99 -- which is exactly why fan-out services care about quantiles
    nobody else measures.
    """
    return quantile(single_sorted, 0.99 ** (1.0 / n))


def fanout_p99(rng: random.Random, n: int, correlated: bool) -> tuple[float, float]:
    """(p50, p99) of the slowest of n calls, over FANOUTS repetitions."""
    totals = []
    for _ in range(FANOUTS):
        multiplier = 1.0
        if correlated and rng.random() < CORRELATED_EVENT_P:
            multiplier = CORRELATED_MULTIPLIER
        totals.append(max(draw(rng) * multiplier for _ in range(n)))
    totals.sort()
    return quantile(totals, 0.50), quantile(totals, 0.99)


def fanout_with_hedging(rng: random.Random, n: int, hedge_after_ms: float,
                        correlated: bool) -> tuple[float, float, float]:
    """(p50, p99, extra request fraction) with budgeted hedging.

    Any call still outstanding at `hedge_after_ms` gets a second copy; the
    winner is whichever finishes first. A token bucket caps hedges at
    HEDGE_BUDGET of all calls issued, because an unbudgeted hedge is a
    retry storm.
    """
    totals = []
    calls = 0
    hedges = 0
    for _ in range(FANOUTS):
        multiplier = 1.0
        if correlated and rng.random() < CORRELATED_EVENT_P:
            multiplier = CORRELATED_MULTIPLIER
        slowest = 0.0
        for _ in range(n):
            first = draw(rng) * multiplier
            calls += 1
            effective = first
            if first > hedge_after_ms and hedges < HEDGE_BUDGET * calls:
                hedges += 1
                calls += 1
                # The hedge starts at hedge_after_ms and races the original.
                second = draw(rng) * multiplier
                effective = min(first, hedge_after_ms + second)
            slowest = max(slowest, effective)
        totals.append(slowest)
    totals.sort()
    return quantile(totals, 0.50), quantile(totals, 0.99), hedges / max(1, calls)


def main() -> None:
    rng = random.Random(SEED)

    print("Fan-out tail compounding -- Monte Carlo over a measured-shape "
          "service distribution")
    print(f"  seed {SEED}, {SINGLE_CALL_DRAWS:,} single-call draws, "
          f"{FANOUTS:,} fan-outs per row")
    print(f"  backend: {int((1 - SLOW_FRACTION) * 100)}% ~N({FAST_MEAN_MS:.0f}, "
          f"{FAST_SD_MS:.0f})ms, {int(SLOW_FRACTION * 100)}% ~N({SLOW_MEAN_MS:.0f}, "
          f"{SLOW_SD_MS:.0f})ms")

    single = sorted(draw(rng) for _ in range(SINGLE_CALL_DRAWS))
    p50, p95, p99 = (quantile(single, q) for q in (0.50, 0.95, 0.99))
    cs = statistics.stdev(single) / statistics.fmean(single)
    print(f"\n  single call: p50 {p50:.1f}ms   p95 {p95:.1f}ms   p99 {p99:.1f}ms   "
          f"c_s {cs:.2f}")

    print("\nIndependent calls -- the arithmetic, and the harness agreeing with it")
    print("-" * 78)
    print(f"  {'n':>4} {'P(>=1 slow)':>12} {'predicted p99':>15} "
          f"{'measured p99':>14} {'ratio':>7}")
    for n in FAN_WIDTHS:
        predicted = independence_prediction(single, n)
        _, measured = fanout_p99(rng, n, correlated=False)
        print(f"  {n:>4} {(1 - 0.99 ** n) * 100:>11.1f}% {predicted:>14.1f}ms "
              f"{measured:>13.1f}ms {measured / predicted:>7.2f}")

    print("\nCorrelated calls -- 5% of fan-outs land in a bad moment, all n slowed 3x")
    print("-" * 78)
    print("  Independence is the OPTIMISTIC assumption. The prediction is a floor.")
    print(f"  {'n':>4} {'predicted p99':>15} {'measured p99':>14} {'excess':>9}")
    for n in FAN_WIDTHS:
        predicted = independence_prediction(single, n)
        _, measured = fanout_p99(rng, n, correlated=True)
        print(f"  {n:>4} {predicted:>14.1f}ms {measured:>13.1f}ms "
              f"{(measured / predicted - 1) * 100:>8.0f}%")
    print("\n  That excess is the correlation, and it is a number rather than a")
    print("  hand-wave. When your measured fan-out p99 beats the independence")
    print("  prediction, suspect the measurement; when it loses to it, you have")
    print("  found shared queueing and can go looking for the shared thing.")

    print(f"\nHedging at the measured p95 ({p95:.1f}ms), budget "
          f"{HEDGE_BUDGET * 100:.0f}% of calls")
    print("-" * 78)
    print(f"  {'n':>4} {'p50 before':>12} {'p50 after':>11} {'p99 before':>12} "
          f"{'p99 after':>11} {'change':>8} {'hedge rate':>11}")
    for n in FAN_WIDTHS:
        b50, b99 = fanout_p99(rng, n, correlated=True)
        a50, a99, rate = fanout_with_hedging(rng, n, p95, correlated=True)
        print(f"  {n:>4} {b50:>11.1f}ms {a50:>10.1f}ms {b99:>11.1f}ms "
              f"{a99:>10.1f}ms {(a99 / b99 - 1) * 100:>7.0f}% {rate * 100:>10.1f}%")
    print("\n  p50 barely moves and p99 does. That asymmetry is the whole case for")
    print("  hedging, and also its limit: it spends extra requests to buy tail")
    print("  latency, so it works when you have spare capacity and makes things")
    print("  worse when you do not. Dean & Barroso, The Tail at Scale (CACM 2013)")
    print("  is still the primary source, and the budget is still the part people")
    print("  leave out.")


if __name__ == "__main__":
    main()
