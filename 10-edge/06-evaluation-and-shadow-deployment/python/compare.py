"""
Layer 10 - Topic 6: the comparison, with error bars, per slice.

What this demonstrates
    Most model-versus-model decisions are made on a 2-4 point aggregate
    difference over about 200 items. For a binary score near 50% the
    standard error is sqrt(0.25/200) ~= 3.5 points, so a 95% interval is
    about +/- 7 points. The decision was noise.

    This tool does three things that turn a score into a measurement:

      1. a PAIRED bootstrap, resampling items rather than scores, so the
         interval is on the DIFFERENCE and the pairing (both models saw
         the same items) is preserved. Unpaired intervals on two means are
         wider and answer a question nobody asked;
      2. per-slice reporting, because the failure lives in a slice that is
         2% of the set and the aggregate absorbs it;
      3. the decision rule, applied mechanically, printed next to the
         numbers. The rule is an input to this program, not an output --
         write it down in PREDICTIONS.md before you look.

    With no arguments it runs a synthetic comparison built so the
    aggregate is flat while one small slice regresses badly. That is the
    scenario the tool exists to catch, and building it deliberately is how
    you check the tool works before trusting it on a real decision.

What to look for
    - `aggregate` looking like a tie, with a CI that straddles zero, while
      the `adversarial` slice has a large negative delta and a CI that
      does not.
    - The `min detectable effect` column. On a slice of 20 items nothing
      short of a landslide is detectable, and the honest report of a small
      slice is "underpowered", not a number with three decimal places.
    - `n_items` next to every interval. A CI without its n is decoration.

Standard library only. Runs with no arguments:
    python3 python/compare.py
    python3 python/compare.py --a scores_fp16.jsonl --b scores_q4.jsonl \\
        --bootstrap 10000

Score files are JSON Lines: {"item_id": ..., "slice": ..., "score": 0 or 1}.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import random
import statistics

SEED = 20260818

# The decision rule. An INPUT, written before any result exists.
# Copy it into PREDICTIONS.md so it cannot be revised quietly afterwards.
PROMOTION_RULE = {
    "max_slice_regression_points": 3.0,
    "require_ci_excludes_zero": True,
    "min_slice_n_for_a_verdict": 30,
}


def paired_bootstrap(pairs: list[tuple[float, float]], iterations: int,
                     rng: random.Random) -> tuple[float, float, float]:
    """(observed delta, lo, hi) for a 95% percentile interval on B - A.

    Resamples ITEMS, not scores. Both models saw the same items, so the
    pairing carries information and throwing it away makes the interval
    wider for no reason.
    """
    n = len(pairs)
    observed = statistics.fmean(b - a for a, b in pairs)
    deltas = []
    for _ in range(iterations):
        sample = [pairs[rng.randrange(n)] for _ in range(n)]
        deltas.append(statistics.fmean(b - a for a, b in sample))
    deltas.sort()
    lo = deltas[int(0.025 * iterations)]
    hi = deltas[min(iterations - 1, int(0.975 * iterations))]
    return observed, lo, hi


def min_detectable_effect(n: int, p: float = 0.5) -> float:
    """Roughly the smallest difference a two-sided 95% test could resolve.

    1.96 * sqrt(2 * p(1-p) / n) in proportion points -- the unpaired form,
    which is conservative. Pairing usually does better, and that is the
    direction you want an honesty check to err in.
    """
    return 100.0 * 1.96 * math.sqrt(2 * p * (1 - p) / max(n, 1))


def synthetic_scores() -> tuple[dict, dict]:
    """A comparison built so the aggregate is a tie and one slice is not.

    Model B is very slightly better on the three big slices and much worse
    on the small adversarial one -- which is exactly what quantization
    tends to do, and exactly what an aggregate hides.
    """
    rng = random.Random(SEED)
    slices = {
        "short": (80, 0.78, 0.80),
        "long": (45, 0.70, 0.72),
        "code": (40, 0.65, 0.67),
        "nonenglish": (12, 0.60, 0.58),   # deliberately too small for a verdict
        "adversarial": (35, 0.62, 0.28),  # deliberately powered enough for one
    }
    a_scores, b_scores = {}, {}
    for name, (n, pa, pb) in slices.items():
        for i in range(n):
            item = f"{name}-{i}"
            # Correlated pair: a shared item difficulty, so the pairing is
            # real rather than two independent coin flips.
            # A shared item difficulty plus independent per-model noise, so
            # the pairing is real without being degenerate.
            difficulty = rng.random()
            draw_a = 0.6 * difficulty + 0.4 * rng.random()
            draw_b = 0.6 * difficulty + 0.4 * rng.random()
            a_scores[item] = (name, 1 if draw_a < pa else 0)
            b_scores[item] = (name, 1 if draw_b < pb else 0)
    return a_scores, b_scores


def load_scores(path: pathlib.Path) -> dict:
    out = {}
    with path.open() as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                out[str(r["item_id"])] = (r.get("slice", "all"), float(r["score"]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", type=pathlib.Path, help="baseline scores (jsonl)")
    ap.add_argument("--b", type=pathlib.Path, help="candidate scores (jsonl)")
    ap.add_argument("--bootstrap", type=int, default=10000)
    args = ap.parse_args()

    if args.a and args.b:
        a_scores, b_scores = load_scores(args.a), load_scores(args.b)
        source = f"{args.a.name} vs {args.b.name}"
    else:
        a_scores, b_scores = synthetic_scores()
        source = ("synthetic: aggregate flat by construction, adversarial "
                  "slice regressed by construction")

    shared = sorted(set(a_scores) & set(b_scores))
    if not shared:
        raise SystemExit("no item_ids in common")

    rng = random.Random(SEED)
    print("Paired comparison with bootstrap confidence intervals")
    print(f"  source : {source}")
    print(f"  items  : {len(shared)}, bootstrap iterations {args.bootstrap}")
    print(f"\n  decision rule, fixed BEFORE the numbers existed:")
    for k, v in PROMOTION_RULE.items():
        print(f"    {k:<32} {v}")

    by_slice: dict[str, list[tuple[float, float]]] = {}
    for item in shared:
        name, sa = a_scores[item]
        _, sb = b_scores[item]
        by_slice.setdefault(name, []).append((sa, sb))
    all_pairs = [p for pairs in by_slice.values() for p in pairs]

    print(f"\n  {'slice':<14} {'n':>5} {'A':>7} {'B':>7} {'Δ (B-A)':>9} "
          f"{'95% CI':>18} {'min detectable':>15} {'verdict':>14}")
    print("  " + "-" * 96)

    rows = [("aggregate", all_pairs)] + sorted(by_slice.items())
    regressions = []
    for name, pairs in rows:
        n = len(pairs)
        mean_a = 100 * statistics.fmean(a for a, _ in pairs)
        mean_b = 100 * statistics.fmean(b for _, b in pairs)
        delta, lo, hi = paired_bootstrap(pairs, args.bootstrap, rng)
        delta, lo, hi = 100 * delta, 100 * lo, 100 * hi
        mde = min_detectable_effect(n)

        if n < PROMOTION_RULE["min_slice_n_for_a_verdict"]:
            verdict = "underpowered"
        elif lo > 0:
            verdict = "improvement"
        elif hi < 0:
            verdict = "REGRESSION"
        else:
            verdict = "no signal"
        if verdict == "REGRESSION" and -delta > PROMOTION_RULE[
                "max_slice_regression_points"]:
            regressions.append((name, delta))

        print(f"  {name:<14} {n:>5} {mean_a:>7.1f} {mean_b:>7.1f} {delta:>+9.1f} "
              f"{f'[{lo:+.1f}, {hi:+.1f}]':>18} {mde:>14.1f}p {verdict:>14}")

    underpowered = [name for name, pairs in rows
                    if len(pairs) < PROMOTION_RULE["min_slice_n_for_a_verdict"]]
    print()
    if regressions:
        print("  DO NOT PROMOTE. Slices regressed beyond the rule's threshold:")
        for name, delta in regressions:
            print(f"    {name}: {delta:+.1f} points")
    else:
        print("  No slice regressed beyond the rule's threshold.")
    if underpowered:
        print(f"  Slices with no verdict available: {', '.join(underpowered)}.")
        print("  That is not the same as 'no regression there'. It is a request")
        print("  for more items in those slices before the next comparison.")
    print()
    print("  Read the aggregate row and the worst slice row together. The")
    print("  aggregate is the number that goes in the launch post; the slice is")
    print("  the number that generates the support tickets.")
    print()
    print("  Any slice marked `underpowered` has too few items for a verdict at")
    print("  all. Reporting a delta for it anyway -- in either direction -- is")
    print("  the most common way an eval lies without anyone intending it to.")


if __name__ == "__main__":
    main()
