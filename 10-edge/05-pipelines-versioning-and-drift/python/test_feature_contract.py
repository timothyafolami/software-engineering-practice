"""
Layer 10 - Topic 5: the contract test. THIS IS THE DELIVERABLE.

What this demonstrates
    The test you can paste into a real repository on Monday. It samples
    logged production feature vectors, recomputes them from the offline
    path at the LOGGED prediction timestamp, and asserts equality within a
    stated tolerance. Training/serving skew is not a thing you notice; it
    is a thing you assert against, or it is a thing that quietly costs you
    a model.

    Three properties make it a real test rather than a ritual:

      1. It recomputes at the logged prediction timestamp, not "now".
         Recomputing at now() compares two different questions and passes
         for the wrong reason.
      2. The tolerance is stated, per field, in the source. An unstated
         tolerance is an argument waiting to happen at 3am.
      3. When it fails it reports the TOP OFFENDERS by absolute
         difference, with the user ids, so the first debugging step is
         reading three rows rather than reproducing the pipeline.

What to look for
    - Against the `native` production log it FAILS, and the failure names
      the worst rows. That is the incident, caught.
    - Against the `conform` log it PASSES. Run both; a test that has never
      failed has not been shown to work.

Standard library only. Runs with no arguments, exits non-zero on skew:
    python3 python/test_feature_contract.py                 # native: should FAIL
    python3 python/test_feature_contract.py --log conform   # should PASS

Also collects under pytest, if you would rather wire it into CI:
    pytest python/test_feature_contract.py
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "python"))

from features import compute_all, load_events  # noqa: E402

AS_OF_MS = 1_785_542_400_000
SAMPLE_SIZE = 500
SEED = 20260818

# The tolerance, stated per field, in the source, where an argument about
# it can be settled by reading rather than by remembering.
TOLERANCE = {
    "spend_7d": 0,          # integer cents: exact, no excuses
    "txn_count_7d": 0,      # a count: exact
    "avg_amount_7d": 0.01,  # one cent, because it is rounded to two places
    "recency_days": 0,      # whole days: exact
}


def load_production_log(mode: str) -> dict[int, dict[str, str]]:
    """Stand-in for 'the feature vectors your serving path actually logged'.

    In a real repository this reads your feature store or your request log.
    Here it reads whichever implementation you are treating as production;
    `native` is the skewed one, `conform` is the fixed one.
    """
    path = DATA / f"features_go_{mode}.csv"
    with path.open(newline="") as fh:
        return {int(r["user_id"]): r for r in csv.DictReader(fh)}


def offline_recompute(user_ids: list[int]) -> dict[int, dict[str, float]]:
    """The offline path, at the LOGGED prediction timestamp."""
    events = load_events(str(DATA / "events.csv"))
    rows = compute_all(events, AS_OF_MS, user_ids=user_ids)
    return {r.user_id: {
        "spend_7d": float(r.spend_7d),
        "txn_count_7d": float(r.txn_count_7d),
        "avg_amount_7d": float(r.avg_amount_7d),
        "recency_days": float(r.recency_days),
    } for r in rows}


def check(mode: str = "native", sample_size: int = SAMPLE_SIZE):
    """Return (mismatches, sampled_ids). A mismatch is (uid, field, online,
    offline, abs_diff)."""
    production = load_production_log(mode)
    rng = random.Random(SEED)
    ids = sorted(production)
    sampled = sorted(rng.sample(ids, min(sample_size, len(ids))))
    offline = offline_recompute(sampled)

    mismatches = []
    for uid in sampled:
        online_row = production[uid]
        offline_row = offline[uid]
        for field, tol in TOLERANCE.items():
            online = float(online_row[field])
            expected = offline_row[field]
            diff = abs(online - expected)
            if diff > tol:
                mismatches.append((uid, field, online, expected, diff))
    return mismatches, sampled


def assert_no_skew(mode: str = "conform") -> None:
    """The assertion a real PR would add, in the form CI would run it."""
    mismatches, sampled = check(mode)
    assert not mismatches, (
        f"training/serving skew: {len(mismatches)} field mismatches across "
        f"{len(sampled)} sampled production vectors. Worst: "
        + "; ".join(f"user {u} {f} online={o} offline={e}"
                    for u, f, o, e, _ in
                    sorted(mismatches, key=lambda m: -m[4])[:3])
    )


def test_conformed_serving_path_matches_offline():
    assert_no_skew("conform")


def test_the_guard_can_actually_fail():
    # Documents that the assertion above is capable of failing. A test that
    # cannot fail is not a test.
    try:
        assert_no_skew("native")
    except AssertionError:
        return
    raise AssertionError("the native serving path should have shown skew")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", choices=("native", "conform"), default="native",
                    help="which serving log to treat as production")
    ap.add_argument("--sample", type=int, default=SAMPLE_SIZE)
    args = ap.parse_args()

    path = DATA / f"features_go_{args.log}.csv"
    if not path.exists():
        print(f"BLOCKED: {path} not found. Generate the inputs first:")
        print("  python3 python/seed_events.py")
        print("  cd golang && go run features.go && "
              "go run features.go -mode conform -out ../data/features_go_conform.csv")
        return 2

    mismatches, sampled = check(args.log, args.sample)

    print("Feature contract test -- serving log vs offline recompute")
    print(f"  production log     : {path.name}")
    print(f"  sampled vectors    : {len(sampled)} (seed {SEED})")
    print(f"  recomputed as of   : {AS_OF_MS} ms, the LOGGED prediction time")
    print(f"  tolerances         : " +
          ", ".join(f"{k}={v}" for k, v in TOLERANCE.items()))
    print()

    if not mismatches:
        print(f"  PASS -- {len(sampled)} vectors agree within tolerance.")
        print("  Wire assert_no_skew() into CI and run it against a fresh sample")
        print("  of production vectors on every deploy.")
        return 0

    by_field: dict[str, int] = {}
    users = set()
    for uid, field, _, _, _ in mismatches:
        by_field[field] = by_field.get(field, 0) + 1
        users.add(uid)

    print(f"  FAIL -- {len(mismatches)} field mismatches across {len(users)} of "
          f"{len(sampled)} sampled vectors ({100 * len(users) / len(sampled):.1f}%)")
    print(f"  by field: " + ", ".join(f"{k} x{v}" for k, v in
                                      sorted(by_field.items(), key=lambda kv: -kv[1])))
    print()
    print("  top offenders by absolute difference:")
    print(f"    {'user':>8} {'field':>16} {'online':>14} {'offline':>14} {'abs diff':>12}")
    for uid, field, online, expected, diff in sorted(
            mismatches, key=lambda m: -m[4])[:10]:
        print(f"    {uid:>8} {field:>16} {online:>14.2f} {expected:>14.2f} "
              f"{diff:>12.2f}")
    print()
    print("  That list is the deliverable. Not 'the model degraded' -- three")
    print("  user ids, a field name, and two numbers that should have matched.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
