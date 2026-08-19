"""
Layer 10 - Topic 6: agreement, chance-corrected, and why raw percent
agreement is worthless.

What this demonstrates
    Cohen's kappa and Krippendorff's alpha, written out rather than
    imported, plus the demonstration that makes the case for them:

        If 95% of items are "fine" and two raters both always say "fine",
        they agree 95% of the time and have measured nothing at all.

    kappa = (p_observed - p_chance) / (1 - p_chance), so that pair scores
    0.00 -- correctly, because they carry no information beyond the base
    rate. Krippendorff's alpha handles more than two raters and missing
    labels, which is what you actually have once a third person helps.

    Working thresholds, quoted as the conventions they are and not as
    theory: below 0.4 means the RUBRIC is broken rather than the raters --
    rewrite it; 0.4-0.6 weak but fixable; above 0.6 acceptable; above 0.8
    strong.

What to look for
    - The `raw agreement` and `kappa` columns disagreeing violently in the
      unbalanced rows. That gap is the entire argument.
    - The `judge` rows. A judge's kappa against you is only meaningful
      next to your kappa against another human: the human-human number is
      the ceiling, and a judge at 0.55 against a human-human ceiling of
      0.60 is doing well, while the same 0.55 against a ceiling of 0.90 is
      not.
    - Intra-rater kappa -- you against yourself a week later. If you do
      not agree with yourself, no judge can agree with you, and no rubric
      built on those labels means anything.

Standard library only. With no arguments it runs a synthetic demonstration
whose ground truth is known, so the estimator can be checked. With files it
scores real labels:

    python3 python/agreement.py
    python3 python/agreement.py --a labels_me_r1.jsonl --b labels_rater2.jsonl

Label files are JSON Lines with at least {"item_id": ..., "label": ...}.
"""

from __future__ import annotations

import argparse
import itertools
import json
import pathlib
import random


def cohen_kappa(a: list, b: list) -> tuple[float, float, float]:
    """Return (kappa, observed agreement, chance agreement)."""
    assert len(a) == len(b) and a, "need equal-length, non-empty label lists"
    n = len(a)
    labels = sorted(set(a) | set(b))
    observed = sum(1 for x, y in zip(a, b) if x == y) / n
    chance = sum((a.count(l) / n) * (b.count(l) / n) for l in labels)
    if chance >= 1.0:
        # Every rater used exactly one label: agreement is entirely chance
        # and kappa is undefined. Saying so beats printing 1.0.
        return float("nan"), observed, chance
    return (observed - chance) / (1 - chance), observed, chance


def krippendorff_alpha(ratings: dict[str, dict[str, object]]) -> float:
    """Nominal-scale Krippendorff's alpha.

    ratings: {item_id: {rater: label}}. Items with fewer than two labels
    are skipped, which is exactly the missing-data case alpha exists for
    and kappa cannot handle at all.
    """
    units = [list(v.values()) for v in ratings.values() if len(v) >= 2]
    if not units:
        return float("nan")

    labels = sorted({l for u in units for l in u}, key=str)
    index = {l: i for i, l in enumerate(labels)}
    k = len(labels)

    coincidence = [[0.0] * k for _ in range(k)]
    for unit in units:
        m = len(unit)
        for x, y in itertools.permutations(unit, 2):
            coincidence[index[x]][index[y]] += 1.0 / (m - 1)

    n_total = sum(sum(row) for row in coincidence)
    observed_disagreement = sum(
        coincidence[i][j] for i in range(k) for j in range(k) if i != j)
    marginals = [sum(coincidence[i]) for i in range(k)]
    expected_disagreement = sum(
        marginals[i] * marginals[j] / (n_total - 1)
        for i in range(k) for j in range(k) if i != j)
    if expected_disagreement == 0:
        return float("nan")
    return 1 - observed_disagreement / expected_disagreement


def load_labels(path: pathlib.Path) -> dict[str, object]:
    out = {}
    with path.open() as fh:
        for line in fh:
            if line.strip():
                row = json.loads(line)
                out[str(row["item_id"])] = row["label"]
    return out


def synthetic_demo() -> None:
    rng = random.Random(20260818)
    n = 200

    print("Chance-corrected agreement -- a synthetic demonstration whose")
    print("ground truth is known, so the estimator can be checked.\n")
    print(f"  {'scenario':<44} {'raw agree':>10} {'p_chance':>10} {'kappa':>8}")
    print("  " + "-" * 76)

    # 1. Unbalanced, both raters lazy: the case that motivates kappa.
    truth = ["fine" if rng.random() < 0.95 else "bad" for _ in range(n)]
    lazy_a = ["fine"] * n
    lazy_b = ["fine"] * n
    k, obs, ch = cohen_kappa(lazy_a, lazy_b)
    print(f"  {'both raters always say fine (95% base rate)':<44} {obs:>10.3f} "
          f"{ch:>10.3f} {k:>8}")
    print("      kappa is undefined here: with one label in use there is no")
    print("      non-chance agreement left to measure. 95% raw agreement, and")
    print("      not one bit of information.")

    # 2. Both raters slightly informative on the same unbalanced data.
    def noisy(truth: list[str], flip: float) -> list[str]:
        return [t if rng.random() > flip else ("bad" if t == "fine" else "fine")
                for t in truth]

    for flip, label in ((0.02, "both raters good (2% error)"),
                        (0.10, "both raters mediocre (10% error)"),
                        (0.25, "both raters poor (25% error)")):
        a, b = noisy(truth, flip), noisy(truth, flip)
        k, obs, ch = cohen_kappa(a, b)
        print(f"  {label:<44} {obs:>10.3f} {ch:>10.3f} {k:>8.3f}")

    # 3. Balanced data, same error rates: kappa and raw agreement converge.
    balanced = [rng.choice(["fine", "bad"]) for _ in range(n)]
    for flip, label in ((0.02, "balanced data, 2% error"),
                        (0.25, "balanced data, 25% error")):
        a, b = noisy(balanced, flip), noisy(balanced, flip)
        k, obs, ch = cohen_kappa(a, b)
        print(f"  {label:<44} {obs:>10.3f} {ch:>10.3f} {k:>8.3f}")

    print("\n  Same raters, same error rate, wildly different kappa depending on")
    print("  the base rate. Report kappa AND the base rate, or the number is not")
    print("  interpretable.")

    # 4. A judge against a human-human ceiling.
    print("\n  A judge is only interpretable against the human ceiling:")
    human_a = noisy(balanced, 0.10)
    human_b = noisy(balanced, 0.10)
    judge = noisy(balanced, 0.18)
    hh, _, _ = cohen_kappa(human_a, human_b)
    jh, _, _ = cohen_kappa(judge, human_a)
    print(f"    human-human kappa      {hh:>6.3f}   <- the ceiling")
    print(f"    judge-human kappa      {jh:>6.3f}   "
          f"({100 * jh / hh:.0f}% of the ceiling)")
    print("    A judge at 0.55 is good against a ceiling of 0.60 and poor")
    print("    against a ceiling of 0.90. The ratio is the number to quote.")

    # 5. Krippendorff's alpha with three raters and missing labels.
    ratings: dict[str, dict[str, object]] = {}
    third = noisy(balanced, 0.12)
    for i in range(n):
        item = {"me": human_a[i], "rater2": human_b[i]}
        if i % 3 == 0:                     # the third rater only did a third
            item["rater3"] = third[i]
        if i % 17 == 0:                    # and rater2 skipped some
            item.pop("rater2")
        ratings[str(i)] = item
    print(f"\n  Krippendorff's alpha, 3 raters with missing labels: "
          f"{krippendorff_alpha(ratings):.3f}")
    print("  Cohen's kappa cannot be computed on that data at all -- it needs")
    print("  two raters who both labelled every item. Alpha is what you reach")
    print("  for the moment a third person helps out for an afternoon.")

    print("\n  Thresholds, as conventions rather than theory:")
    print("    < 0.4   the RUBRIC is broken, not the raters. Rewrite it and")
    print("            relabel -- that is the experiment succeeding.")
    print("    0.4-0.6 weak but fixable")
    print("    > 0.6   acceptable        > 0.8   strong")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", type=pathlib.Path, help="first rater's labels (jsonl)")
    ap.add_argument("--b", type=pathlib.Path, help="second rater's labels (jsonl)")
    args = ap.parse_args()

    if not (args.a and args.b):
        synthetic_demo()
        return

    la, lb = load_labels(args.a), load_labels(args.b)
    shared = sorted(set(la) & set(lb))
    if not shared:
        raise SystemExit("no item_ids in common between the two files")
    a = [la[i] for i in shared]
    b = [lb[i] for i in shared]
    k, obs, ch = cohen_kappa(a, b)
    print(f"items in common : {len(shared)}")
    print(f"raw agreement   : {obs:.3f}")
    print(f"chance agreement: {ch:.3f}")
    print(f"Cohen's kappa   : {k:.3f}")
    print(f"alpha (2 raters): "
          f"{krippendorff_alpha({i: {'a': la[i], 'b': lb[i]} for i in shared}):.3f}")
    if k < 0.4:
        print("\nBelow 0.4: rewrite the rubric, then relabel. This is the rubric")
        print("failing its test, not the raters failing theirs.")


if __name__ == "__main__":
    main()
