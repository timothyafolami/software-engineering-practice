"""
Layer 6 Topic 2 - Four separate ways a p99 number is wrong.

Why Python: every one of these errors is arithmetic done by a monitoring
system on your behalf. Reimplementing that arithmetic in ~40 lines is the only
way to stop treating histogram_quantile() as an oracle.

What this demonstrates
----------------------
The same 200,000 latency samples (a realistic bimodal service: most requests
fast, a small tail three orders of magnitude slower) run through:

  1. BUCKETS      - histogram_quantile over a bucket list whose top finite
                    boundary is below the real p99. Prometheus's documented
                    behaviour is to return the upper bound of the highest
                    finite bucket. Not an error. Just wrong, and always wrong
                    at a suspiciously round number.
  2. AGGREGATION  - avg(p99 per pod) versus summing bucket counters across
                    pods and then taking the quantile.
  3. SAMPLE COUNT - the spread of a p99 computed over 200 requests, versus the
                    same statistic over 100,000.
  4. RESOLUTION   - Prometheus 3 native histograms (exponential buckets) on
                    the identical data.

What to look for in the output
------------------------------
The "err" column against ground truth. One row will be off by a factor of
several while looking like a perfectly plausible dashboard number.

Run:  python3 histogram_lies.py
"""

import random
import statistics

SEED = 20260818
N = 200_000

# The bucket list a 2019 dashboard shipped with: fine at the bottom, stops at
# one second, because at the time nothing took longer than a second.
BUCKETS_GUESSED = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0]

# The OTel SDK's current default advice for http.server.request.duration, in
# SECONDS. Note it goes to 10s, which is the only reason it survives this test.
BUCKETS_OTEL = [0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75,
                1.0, 2.5, 5.0, 7.5, 10.0]


def sample_latency(rng, tail_ratio=0.03):
    """97% of requests hit warm cache + one index scan. 3% hit the pricing
    tail: an upstream that returns in 8ms 99 times and 4s on the 100th."""
    if rng.random() < tail_ratio:
        return rng.lognormvariate(1.30, 0.35)      # seconds, ~3.7s median
    return rng.lognormvariate(-3.5, 0.45)          # seconds, ~30ms median


def bucketize(samples, boundaries):
    """Cumulative ('le') counters, exactly as a Prometheus histogram stores."""
    counts = [0] * (len(boundaries) + 1)
    for s in samples:
        placed = False
        for i, b in enumerate(boundaries):
            if s <= b:
                counts[i] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    cumulative, running = [], 0
    for c in counts:
        running += c
        cumulative.append(running)
    return cumulative


def histogram_quantile(q, cumulative, boundaries):
    """Prometheus's histogram_quantile, including the rule people forget:
    if the quantile falls in the +Inf bucket, the upper bound of the highest
    finite bucket is returned. That is where the round numbers come from."""
    total = cumulative[-1]
    if total == 0:
        return None
    target = q * total
    for i, cum in enumerate(cumulative):
        if cum >= target:
            if i == len(boundaries):                  # landed in +Inf
                return boundaries[-1], True
            lower = boundaries[i - 1] if i > 0 else 0.0
            upper = boundaries[i]
            prev = cumulative[i - 1] if i > 0 else 0
            span = cum - prev
            frac = (target - prev) / span if span else 0.0
            return lower + (upper - lower) * frac, False
    return boundaries[-1], True


# --- Native histograms (Prometheus 3): exponential buckets, no guessing -----
# Bucket i covers (base**i, base**(i+1)] with base = 2 ** (2 ** -schema).
# schema=3 gives ~9% relative bucket width across the whole range.
def native_bucketize(samples, schema=3):
    import math
    base = 2.0 ** (2.0 ** -schema)
    buckets = {}
    for s in samples:
        idx = math.ceil(math.log(s, base))
        buckets[idx] = buckets.get(idx, 0) + 1
    return buckets, base


def native_quantile(q, buckets, base):
    total = sum(buckets.values())
    target = q * total
    running = 0
    for idx in sorted(buckets):
        running += buckets[idx]
        if running >= target:
            lower, upper = base ** (idx - 1), base ** idx
            prev = running - buckets[idx]
            frac = (target - prev) / buckets[idx]
            return lower + (upper - lower) * frac
    return None


def err(estimate, truth):
    return "%+.0f%%" % (100 * (estimate - truth) / truth)


def part_1_buckets(samples):
    truth99 = statistics.quantiles(samples, n=100, method="inclusive")[98]
    truth50 = statistics.median(samples)
    print("\n[1] BUCKET BOUNDARIES")
    print("    ground truth from raw samples: p50 = %.4fs   p99 = %.4fs" % (truth50, truth99))
    print("    %-28s %10s %10s %8s  %s" % ("bucket set", "p50", "p99", "err(p99)", "note"))
    for name, bounds in (("guessed, top le=1.0", BUCKETS_GUESSED),
                         ("OTel default, top le=10.0", BUCKETS_OTEL)):
        cum = bucketize(samples, bounds)
        p50, _ = histogram_quantile(0.50, cum, bounds)
        p99, clamped = histogram_quantile(0.99, cum, bounds)
        note = "clamped to top finite bucket" if clamped else "interpolated inside a bucket"
        print("    %-28s %9.4fs %9.4fs %8s  %s" % (name, p50, p99, err(p99, truth99), note))
    print("    The clamped row is the failure: a flat, round, believable number.")
    return truth99


def part_2_aggregation(rng):
    print("\n[2] AGGREGATION ACROSS PODS  (you cannot average percentiles)")
    # Four pods behind one service. Pod 3 is the one wedged behind the pricing
    # tail -- the shape you get when a single replica loses its connection pool.
    tail_ratios = [0.005, 0.005, 0.005, 0.11]
    pods = {"pod-%d" % i: [sample_latency(rng, tail_ratio=t) for _ in range(50_000)]
            for i, t in enumerate(tail_ratios)}
    all_samples = [s for v in pods.values() for s in v]
    truth = statistics.quantiles(all_samples, n=100, method="inclusive")[98]

    per_pod = []
    for (name, samples), tail in zip(pods.items(), tail_ratios):
        cum = bucketize(samples, BUCKETS_OTEL)
        p99, _ = histogram_quantile(0.99, cum, BUCKETS_OTEL)
        per_pod.append(p99)
        print("    %-8s p99 = %7.4fs  (tail ratio %.3f)" % (name, p99, tail))
    avg_of_p99 = sum(per_pod) / len(per_pod)

    merged = [0] * (len(BUCKETS_OTEL) + 1)
    for samples in pods.values():
        for i, c in enumerate(bucketize(samples, BUCKETS_OTEL)):
            merged[i] += c
    correct, _ = histogram_quantile(0.99, merged, BUCKETS_OTEL)

    print("    avg(p99 per pod)                = %7.4fs   err %s   <- meaningless"
          % (avg_of_p99, err(avg_of_p99, truth)))
    print("    quantile(sum(bucket counters))  = %7.4fs   err %s   <- correct"
          % (correct, err(correct, truth)))
    print("    ground truth over all pods      = %7.4fs" % truth)
    print("    Bucket counters are summable; percentiles are not. That is why")
    print("    histograms are shipped as counters instead of as computed p99s.")


def part_3_sample_count(rng):
    print("\n[3] SAMPLE COUNT  (a p99 over 200 requests is the 2nd-slowest request)")
    for n, trials in ((200, 500), (2_000, 500), (100_000, 20)):
        estimates = []
        for _ in range(trials):
            window = [sample_latency(rng) for _ in range(n)]
            estimates.append(statistics.quantiles(window, n=100, method="inclusive")[98])
        lo, hi = min(estimates), max(estimates)
        print("    n=%-7d over %3d windows: p99 ranged %.3fs .. %.3fs  (spread %.1fx, stdev %.3fs)"
              % (n, trials, lo, hi, hi / lo, statistics.pstdev(estimates)))
    print("    Same service, same distribution, unchanged. All that moved is n.")
    print("    Before believing a p99 moved, check how many requests it was over.")


def part_4_native(samples, truth99):
    print("\n[4] NATIVE HISTOGRAMS  (Prometheus 3: exponential buckets, no guessing)")
    for schema in (0, 3, 5):
        b, bs = native_bucketize(samples, schema)
        p99 = native_quantile(0.99, b, bs)
        print("    schema=%d  bucket width %5.1f%%  %5d buckets used  p99 = %.4fs  err %s"
              % (schema, (bs - 1) * 100, len(b), p99, err(p99, truth99)))
    print("    No boundary list to guess at deploy time, and the relative error")
    print("    is bounded by the schema rather than by how well you guessed.")


def main():
    rng = random.Random(SEED)
    samples = [sample_latency(rng) for _ in range(N)]
    print("=" * 74)
    print("FOUR WAYS A p99 IS WRONG   (n=%d, bimodal: 97%% fast, 3%% tail)" % N)
    print("=" * 74)
    truth99 = part_1_buckets(samples)
    part_2_aggregation(rng)
    part_3_sample_count(rng)
    part_4_native(samples, truth99)
    print("\nNone of the four requires the service to have changed. Each is the")
    print("measurement lying on its own.")


if __name__ == "__main__":
    main()
