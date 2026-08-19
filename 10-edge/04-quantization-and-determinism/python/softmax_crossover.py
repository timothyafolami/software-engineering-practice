"""
Layer 10 - Topic 4: where naive softmax stops working, exactly.

What this demonstrates
    softmax(x)_i = exp(x_i) / sum_j exp(x_j) overflows as soon as max(x)
    exceeds the point where exp() leaves the format's range. The standard
    form subtracts the max first:

        softmax(x)_i = exp(x_i - max(x)) / sum_j exp(x_j - max(x))

    which is EXACT, not an approximation: multiplying numerator and
    denominator by exp(-max(x)) changes nothing mathematically and moves
    the largest exponent to exp(0) = 1. FlashAttention's online-softmax
    rescaling is that identity applied incrementally as tiles stream
    through, which is why it is numerically SAFER than the naive version it
    replaced, not merely faster.

What to look for
    - The crossover columns: the largest logit each dtype survives, found
      by bisection rather than quoted. float16 gives out first, by a lot.
    - `nan` and `inf` in the naive rows while the stable rows are still
      returning a valid distribution at logits of 800.
    - The last section: NumPy's `sum` uses pairwise summation, so it is
      MORE accurate than a naive Python loop, and its accuracy depends on
      array length. Same phenomenon as topic 4's determinism finding, in
      miniature: the answer depends on how the reduction was partitioned.

No dependencies beyond NumPy. Runs with no arguments:
    python3 python/softmax_crossover.py
"""

from __future__ import annotations

import numpy as np


def softmax_naive(x: np.ndarray) -> np.ndarray:
    e = np.exp(x)
    return e / e.sum()


def softmax_stable(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max())
    return e / e.sum()


def is_valid(p: np.ndarray) -> bool:
    return bool(np.all(np.isfinite(p)) and abs(float(p.sum()) - 1.0) < 1e-2)


def logits(peak: float, dtype, n: int = 1024) -> np.ndarray:
    """A vocabulary-sized logit vector whose maximum is `peak`."""
    rng = np.random.default_rng(20260818)
    x = rng.normal(0.0, 1.0, n).astype(dtype)
    x[0] = peak
    return x.astype(dtype)


def crossover(fn, dtype, lo: float = 0.0, hi: float = 100_000.0) -> float:
    """Largest peak logit for which `fn` still returns a valid distribution."""
    if not is_valid(fn(logits(lo, dtype))):
        return float("nan")
    for _ in range(60):
        mid = (lo + hi) / 2
        if is_valid(fn(logits(mid, dtype))):
            lo = mid
        else:
            hi = mid
    return lo


def main() -> None:
    print("Softmax stability -- naive vs max-subtracted (log-sum-exp)")
    print(f"  numpy {np.__version__}, vocabulary 1024, logits ~N(0,1) with one peak\n")

    print(f"  {'peak logit':>11} {'dtype':>9} {'naive sum':>14} {'naive max p':>13} "
          f"{'stable sum':>12} {'stable max p':>13}")
    print("  " + "-" * 78)
    for dtype, name in ((np.float16, "float16"), (np.float32, "float32"),
                        (np.float64, "float64")):
        for peak in (50.0, 200.0, 800.0):
            x = logits(peak, dtype)
            with np.errstate(over="ignore", invalid="ignore"):
                naive = softmax_naive(x)
                stable = softmax_stable(x)
            print(f"  {peak:>11.0f} {name:>9} {float(naive.sum()):>14.6g} "
                  f"{float(naive.max()):>13.6g} {float(stable.sum()):>12.6g} "
                  f"{float(stable.max()):>13.6g}")
    print()
    print("  A naive `sum` of nan is nan and a naive `max p` of nan is nan: the")
    print("  distribution is gone, and nothing downstream will tell you so. It")
    print("  will sample from garbage and produce text.")

    print("\n  Crossover points, found by bisection rather than quoted:")
    print(f"  {'dtype':>9} {'naive survives up to':>22} {'stable survives up to':>23}")
    print("  " + "-" * 56)
    for dtype, name in ((np.float16, "float16"), (np.float32, "float32"),
                        (np.float64, "float64")):
        with np.errstate(over="ignore", invalid="ignore"):
            n_cross = crossover(softmax_naive, dtype)
            s_cross = crossover(softmax_stable, dtype)
        print(f"  {name:>9} {n_cross:>22.1f} {s_cross:>23.1f}")
    print()
    print("  The stable form's limit is set by how far the SPREAD of the logits")
    print("  can go, not by how large they are, so raising the peak alone never")
    print("  breaks it. That is the whole benefit and it costs one subtraction.")

    print("\nPairwise summation -- the same phenomenon, in miniature")
    print("-" * 78)
    print("  NumPy's sum() is pairwise, not a left-to-right loop, so its error")
    print("  grows like log(n) instead of n -- and its exact result therefore")
    print("  depends on the array LENGTH, which is a partitioning decision.")
    print(f"\n  {'n':>10} {'naive loop':>22} {'numpy pairwise':>22} {'exact':>22}")
    for n in (1_000, 100_000, 10_000_000):
        rng = np.random.default_rng(7)
        a = (rng.random(n) + 1.0).astype(np.float32)
        exact = float(np.sum(a, dtype=np.float64))
        loop = np.float32(0.0)
        for v in a[:min(n, 1_000_000)]:
            loop = np.float32(loop + v)
        if n > 1_000_000:
            loop = float("nan")  # too slow to be worth it; the trend is clear
        pairwise = float(np.sum(a))
        print(f"  {n:>10} {float(loop):>22.6f} {pairwise:>22.6f} {exact:>22.6f}")
    print("\n  Neither column is 'wrong'. They summed the same numbers in different")
    print("  orders, and floating-point addition is not associative. Hold on to")
    print("  that sentence -- partition order changing a result is the entire")
    print("  determinism finding later in this topic, scaled up to a GPU kernel.")


if __name__ == "__main__":
    main()
