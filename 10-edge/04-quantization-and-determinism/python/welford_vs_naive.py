"""
Layer 10 - Topic 4: Var(x) = E[x^2] - E[x]^2 is correct algebra and a
terrible algorithm.

What this demonstrates
    For a variable with a large mean, E[x^2] and E[x]^2 are nearly equal,
    so subtracting them destroys every significant digit the answer needed
    -- catastrophic cancellation. In float32 it can return a NEGATIVE
    variance, which is not a rounding error, it is a value outside the
    range the quantity is defined on. Welford's online algorithm computes
    the same quantity by accumulating the deviation from a running mean, so
    nothing large is ever subtracted from anything else large.

    This is not academic. It shows up in normalisation layers, in the drift
    monitors of topic 5, and in metric aggregation everywhere -- anywhere
    someone wrote the textbook identity because it is one pass.

What to look for
    - `naive var` going negative in float32 while `welford var` stays
      correct. sqrt() of that negative number is where it usually surfaces
      in production: a nan in a standard deviation, three layers away from
      the code that computed it.
    - The offset column. The data is the same shape every row -- N(0,1)
      shifted by a constant -- so the variance is 1.0 in every row by
      construction, and only the mean changes. Nothing about the DATA got
      harder.
    - float64 survives all of it, which is why "just use float64" is a
      real mitigation. Topic 1 tells you what it costs when the thing you
      are moving is model weights.

No dependencies beyond NumPy. Runs with no arguments:
    python3 python/welford_vs_naive.py
"""

from __future__ import annotations

import numpy as np

N = 1_000_000
OFFSETS = (0.0, 1e3, 1e5, 1e6, 1e8)


def naive_variance(x: np.ndarray, dtype) -> float:
    """The textbook identity: E[x^2] - E[x]^2, accumulated in `dtype`."""
    n = dtype(x.size)
    mean = dtype(np.sum(x, dtype=dtype) / n)
    mean_sq = dtype(np.sum(x.astype(dtype) * x.astype(dtype), dtype=dtype) / n)
    return float(mean_sq - mean * mean)


def welford_variance(x: np.ndarray, dtype) -> float:
    """Welford's online algorithm, accumulated in the same `dtype`.

    Each update moves the mean by delta/n and accumulates delta * delta2,
    so the running sum only ever sees DEVIATIONS. Nothing large is
    subtracted from anything else large, and there is nothing to cancel.

    Vectorised in chunks here (Chan's parallel merge) so it finishes in a
    second on a million points -- and the merge step is itself a nice
    reminder that a numerically careful algorithm can still be partitioned.
    """
    chunk = 4096
    count = dtype(0)
    mean = dtype(0)
    m2 = dtype(0)
    for start in range(0, x.size, chunk):
        block = x[start:start + chunk].astype(dtype)
        b_count = dtype(block.size)
        b_mean = dtype(block.mean(dtype=dtype))
        b_m2 = dtype(np.sum((block - b_mean) ** 2, dtype=dtype))
        delta = dtype(b_mean - mean)
        total = dtype(count + b_count)
        mean = dtype(mean + delta * b_count / total)
        m2 = dtype(m2 + b_m2 + delta * delta * count * b_count / total)
        count = total
    return float(m2 / count)


def main() -> None:
    print("Catastrophic cancellation -- naive variance vs Welford")
    print(f"  numpy {np.__version__}, {N:,} samples of N(0,1) + offset")
    print("  the true variance is 1.0 in EVERY row; only the mean changes\n")

    rng = np.random.default_rng(20260818)
    base = rng.normal(0.0, 1.0, N)

    for dtype, name in ((np.float32, "float32"), (np.float64, "float64")):
        print(f"  accumulating in {name}")
        print(f"  {'offset':>10} {'naive var':>18} {'welford var':>18} "
              f"{'naive rel err':>15} {'sqrt(naive)':>13}")
        print("  " + "-" * 78)
        for offset in OFFSETS:
            x = (base + offset).astype(dtype)
            with np.errstate(invalid="ignore"):
                naive = naive_variance(x, dtype)
                welford = welford_variance(x, dtype)
                sd = np.sqrt(naive) if naive >= 0 else float("nan")
            rel = abs(naive - 1.0)
            print(f"  {offset:>10.0e} {naive:>18.6f} {welford:>18.6f} "
                  f"{rel:>15.4g} {sd:>13.6f}")
        print()

    print("  In float32 the naive estimate degrades as the mean grows and then")
    print("  goes negative, at which point sqrt() hands a nan to whatever asked")
    print("  for a standard deviation. Welford holds far longer because it never")
    print("  forms E[x^2] at all -- but read the last float32 row before")
    print("  concluding it is immune: at an offset of 1e8, float32 cannot even")
    print("  REPRESENT the individual deviations any more (1e8 + 1 rounds back to")
    print("  1e8), so the data is gone before any algorithm sees it. A better")
    print("  algorithm does not rescue a format that cannot hold the input.")
    print("  Note also the float64 naive row at the same offset: same failure,")
    print("  same mechanism, just eight orders of magnitude further out.")
    print()
    print("  The fix costs nothing at runtime and is three lines longer. The")
    print("  reason the bad version keeps getting written is that it is the")
    print("  version in the textbook, and it is correct -- as algebra.")


if __name__ == "__main__":
    main()
