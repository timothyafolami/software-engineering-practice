"""
Layer 10 - Topic 5: three implementations, three answers, and which
decision caused each disagreement.

What this demonstrates
    python/features.py, golang/features.go and nodejs/features.js all read
    the same events.csv and compute the same documented feature vector.
    In `native` mode they disagree. This tool counts the disagreements per
    pair and attributes each differing row to ONE of the four decisions the
    spec pins down, so the finding is "the window boundary is wrong for
    2.1% of users" and not "it's a float thing".

    Then it does the same against the `conform` outputs, where every
    implementation follows the written spec. That comparison is the
    evidence for the fix -- and the fix is not "we wrote it correctly three
    times", it is "the transform has one home and everything else calls it".
    Three correct copies is three things to keep correct, and the conform
    columns will drift apart again the first time the spec changes.

What to look for
    - The pairwise counts are NOT equal, and Go-vs-Node is worse than
      either against Python. Independent mistakes compose; they do not
      cancel.
    - The cause column. Every differing row has exactly one attributable
      cause here because the divergences were introduced one at a time.
      Real skew is rarely that tidy, which is why the contract test in
      test_feature_contract.py reports top offenders by magnitude rather
      than trying to explain them.
    - `max abs diff` on spend_7d. A boundary error is not a rounding error:
      it can move a feature by an entire transaction.

Standard library only. Runs with no arguments after the three
implementations have written their outputs:
    python3 python/three_way_diff.py
"""

from __future__ import annotations

import csv
import itertools
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

sys.path.insert(0, str(ROOT / "python"))
from features import compute_all, load_events  # noqa: E402

AS_OF_MS = 1_785_542_400_000


def python_features(mode: str) -> dict[int, dict[str, str]]:
    """Python is the spec, so `native` and `conform` are the same file."""
    rows = compute_all(load_events(str(DATA / "events.csv")), AS_OF_MS)
    return {r.user_id: {
        "spend_7d": str(r.spend_7d),
        "txn_count_7d": str(r.txn_count_7d),
        "avg_amount_7d": f"{r.avg_amount_7d:.2f}",
        "recency_days": str(r.recency_days),
    } for r in rows}


def read_csv(path: pathlib.Path) -> dict[int, dict[str, str]]:
    with path.open(newline="") as fh:
        return {int(r["user_id"]): {k: v for k, v in r.items() if k != "user_id"}
                for r in csv.DictReader(fh)}


def attribute(a: dict[str, str], b: dict[str, str]) -> str:
    """Name the single decision that produced this disagreement."""
    if a["spend_7d"] != b["spend_7d"] or a["txn_count_7d"] != b["txn_count_7d"]:
        return "window boundary"
    if a["txn_count_7d"] == "0" and a["recency_days"] != b["recency_days"]:
        return "empty-window sentinel"
    if a["avg_amount_7d"] != b["avg_amount_7d"]:
        return "rounding mode"
    if a["recency_days"] != b["recency_days"]:
        return "recency floor vs round"
    return "unattributed"


def compare(name_a: str, rows_a, name_b: str, rows_b) -> None:
    shared = sorted(set(rows_a) & set(rows_b))
    differing = []
    causes: dict[str, int] = {}
    max_spend_diff = 0
    for uid in shared:
        a, b = rows_a[uid], rows_b[uid]
        if a == b:
            continue
        differing.append(uid)
        cause = attribute(a, b)
        causes[cause] = causes.get(cause, 0) + 1
        max_spend_diff = max(max_spend_diff,
                             abs(int(a["spend_7d"]) - int(b["spend_7d"])))

    pct = 100.0 * len(differing) / max(1, len(shared))
    cause_text = ", ".join(f"{k} x{v}" for k, v in
                           sorted(causes.items(), key=lambda kv: -kv[1])) or "-"
    print(f"  {name_a:>8} vs {name_b:<8} {len(differing):>7} / {len(shared):<7} "
          f"{pct:>6.2f}%  {max_spend_diff:>10}  {cause_text}")


def run(mode: str) -> None:
    sources = {
        "python": python_features(mode),
        "go": read_csv(DATA / f"features_go_{mode}.csv"),
        "node": read_csv(DATA / f"features_node_{mode}.csv"),
    }
    print(f"\n  {mode} mode")
    print(f"  {'pair':>20} {'differing':>17} {'':>7}  "
          f"{'max Δspend':>10}  causes")
    print("  " + "-" * 100)
    for a, b in itertools.combinations(sources, 2):
        compare(a, sources[a], b, sources[b])


def main() -> None:
    print("Three implementations of one transform, on byte-identical input")
    print(f"  events   : {DATA / 'events.csv'}")
    print(f"  as of    : {AS_OF_MS} ms (2026-08-01T00:00:00Z)")

    missing = [p for p in (
        DATA / "events.csv",
        DATA / "features_go_native.csv", DATA / "features_go_conform.csv",
        DATA / "features_node_native.csv", DATA / "features_node_conform.csv",
    ) if not p.exists()]
    if missing:
        print("\n  BLOCKED: missing inputs:")
        for p in missing:
            print(f"    {p}")
        print("\n  Generate them:")
        print("    python3 python/seed_events.py")
        print("    cd golang && go run features.go && "
              "go run features.go -mode conform -out ../data/features_go_conform.csv")
        print("    node nodejs/features.js && node nodejs/features.js --mode conform")
        raise SystemExit(2)

    run("native")
    run("conform")

    print("\n  The native rows are training/serving skew, measured. The conform")
    print("  rows are what it costs to make three implementations agree: a")
    print("  written spec and three careful rewrites, valid until the next time")
    print("  the feature changes.")
    print()
    print("  The fix is not in this table. The fix is that the transform has one")
    print("  home and everything else calls it across a boundary -- because the")
    print("  conform columns are only correct until somebody edits one of them.")


if __name__ == "__main__":
    main()
