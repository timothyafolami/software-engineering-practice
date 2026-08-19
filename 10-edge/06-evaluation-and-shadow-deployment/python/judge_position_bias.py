"""
Layer 10 - Topic 6: is your judge scoring the answer, or the position?

What this demonstrates
    A pairwise LLM judge is asked "which response is better, A or B?".
    Present the same two responses in both orders and the judge should
    make the same call. Position bias is the extent to which it does not,
    and it is measurable without any ground truth at all:

        position_bias = mean over both orders of P(judge picks slot 1) - 0.5

    Averaging the two orders is what makes this an estimator of the JUDGE
    rather than of the models: if B is genuinely better a fraction p of the
    time, an unbiased judge picks slot 1 with probability (1-p) in the AB
    order and p in the BA order, which averages to exactly 0.5 for any p.
    Model quality cancels. Whatever is left above 0.5 is the instrument's
    own preference for the slot, leaking into your model comparison -- and
    it matters most when the two systems are close, which is exactly when
    you are running the comparison.

    Two derived numbers matter as much as the bias itself:
      - the CONSISTENCY rate: how often the two orderings agree. This is
        the ceiling on how much the judge can tell you.
      - the corrected win rate from averaging the two orderings, which is
        the number to report instead of a single-order win rate.

    With no arguments this runs a simulated judge with a known injected
    bias, so the estimator can be checked against a truth. That is the
    point of the no-argument mode: validate the measurement before
    pointing it at a real judge that cannot be ground-truthed.

What to look for
    - Recovered bias rising monotonically with the injected lean. It is
      roughly half the lean, because half the time the lean picks the slot
      the judge would have picked anyway.
    - The consistency rate falling as bias rises: a biased judge is an
      inconsistent judge, and inconsistency caps the resolution of every
      comparison you run through it.
    - The single-order win rate against the order-averaged one. The gap is
      how far a comparison run in one order can be wrong before any model
      quality is involved.

Standard library only. Runs with no arguments:
    python3 python/judge_position_bias.py
    python3 python/judge_position_bias.py --judgments both_orders.jsonl

Judgment files are JSON Lines:
    {"item_id": ..., "order": "AB"|"BA", "picked": "A"|"B"}
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import random

SEED = 20260818
N_ITEMS = 300


def analyse(judgments: list[dict]) -> None:
    ab = [j for j in judgments if j["order"] == "AB"]
    ba = [j for j in judgments if j["order"] == "BA"]
    if not ab or not ba:
        raise SystemExit("need judgments in both orders")

    # "picks first" means: picked whichever model was shown in slot 1.
    first_ab = sum(1 for j in ab if j["picked"] == "A") / len(ab)
    first_ba = sum(1 for j in ba if j["picked"] == "B") / len(ba)
    # Averaging the two orders cancels genuine model quality: if B really is
    # better p of the time, the judge picks slot 1 (1-p) of the time in the
    # AB order and p of the time in the BA order, which averages to 0.5 for
    # any p. Whatever is left above 0.5 is preference for the slot itself.
    picks_first = (first_ab + first_ba) / 2
    bias = picks_first - 0.5

    by_item: dict[str, dict[str, str]] = {}
    for j in judgments:
        by_item.setdefault(str(j["item_id"]), {})[j["order"]] = j["picked"]
        both = {k: v for k, v in by_item.items() if len(v) == 2}
    consistent = sum(1 for v in both.values() if v["AB"] == v["BA"])
    consistency = consistent / len(both) if both else float("nan")

    win_ab = sum(1 for j in ab if j["picked"] == "B") / len(ab)
    win_ba = sum(1 for j in ba if j["picked"] == "B") / len(ba)
    corrected = (win_ab + win_ba) / 2

    se = 1.96 * math.sqrt(0.25 / len(ab)) * 100

    print(f"  judgments            : {len(judgments)} ({len(ab)} AB, {len(ba)} BA)")
    print(f"  picks slot 1 in AB   : {100 * first_ab:.1f}%")
    print(f"  picks slot 1 in BA   : {100 * first_ba:.1f}%")
    print(f"  picks slot 1, avg    : {100 * picks_first:.1f}%   "
          f"(50% if the judge has no positional preference)")
    print(f"  position bias        : {100 * bias:+.1f} points  "
          f"(+/-{se:.1f} at this n)")
    print(f"  consistency          : {100 * consistency:.1f}% of items judged the "
          f"same both ways")
    print(f"  B win rate, AB order : {100 * win_ab:.1f}%")
    print(f"  B win rate, BA order : {100 * win_ba:.1f}%")
    print(f"  order-averaged       : {100 * corrected:.1f}%   <- report this one")
    print(f"  single-order error   : up to {100 * abs(win_ab - win_ba) / 2:.1f} "
          f"points, before any model quality is involved")


def simulate(true_b_quality: float, injected_bias: float) -> list[dict]:
    """A judge that prefers the better response, and also prefers slot 1.

    `true_b_quality` is P(B is genuinely better). `injected_bias` is how
    much the judge additionally leans toward whatever it sees first.
    """
    rng = random.Random(SEED)
    out = []
    for i in range(N_ITEMS):
        b_better = rng.random() < true_b_quality
        for order in ("AB", "BA"):
            first = "A" if order == "AB" else "B"
            # Start from the truth, then let the positional lean pull the
            # verdict toward slot 1 some of the time.
            verdict = "B" if b_better else "A"
            if rng.random() < injected_bias:
                verdict = first
            out.append({"item_id": i, "order": order, "picked": verdict})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--judgments", type=pathlib.Path)
    args = ap.parse_args()

    if args.judgments:
        with args.judgments.open() as fh:
            judgments = [json.loads(l) for l in fh if l.strip()]
        print(f"Position bias -- {args.judgments.name}")
        analyse(judgments)
        return

    print("Position bias -- simulated judges with known injected bias, so the")
    print("estimator can be checked before it is pointed at a real one.")
    print(f"  {N_ITEMS} items, each judged in both orders, "
          f"P(B genuinely better) = 0.55\n")
    for injected in (0.0, 0.2, 0.5):
        print(f"  injected positional lean: {injected:.0%}")
        analyse(simulate(0.55, injected))
        print()
    print("  The recovered bias should track the injected lean, and consistency")
    print("  should fall as it rises. Once that holds, run the same analysis on")
    print("  a real judge -- where there is no truth to check against, which is")
    print("  precisely why the estimator had to be checked here first.")
    print()
    print("  Report the order-averaged win rate. A single-order comparison is")
    print("  measuring your judge's seating preference alongside your model.")


if __name__ == "__main__":
    main()
