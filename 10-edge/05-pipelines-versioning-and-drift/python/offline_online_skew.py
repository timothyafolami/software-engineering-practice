"""
Layer 10 - Topic 5: the classic skew, and what it costs in AUC.

What this demonstrates
    The most common training/serving skew in production, reproduced
    exactly: the offline path includes the partial current day, and the
    online path does not, because what the online path actually reads is a
    cached aggregate that was materialised at midnight.

    Nobody implemented anything wrong. Both paths are correct with respect
    to their own definition of "the last seven days". The model is trained
    on one of them and served the other.

    A small logistic regression is trained on the offline features and then
    scored on both, so the gap is reported the only way that matters: as a
    change in the metric the model is judged on, not as a change in a
    feature histogram.

What to look for
    - `offline AUC` against `online AUC on the same users`. The rows are
      the same users, the same labels, the same model weights. Only the
      feature computation moved.
    - `mean feature shift`. It is small. That is why this survives review:
      a 4% shift in a mean looks like noise, and the metric it costs you
      is somewhere else entirely.
    - The third row, where the model is retrained on the ONLINE definition.
      How much of the AUC that recovers is itself the finding: retraining
      removes a mismatch, but it cannot restore information the online
      path never had.

Requires NumPy. Runs with no arguments, after seed_events.py:
    python3 python/offline_online_skew.py
"""

from __future__ import annotations

import csv
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "python"))
from features import DAY_MS, compute_all, load_events  # noqa: E402

AS_OF_MS = 1_785_542_400_000
# The online path reads an aggregate materialised at the last midnight, so
# its window ends 14 hours before the prediction. Everything else is equal.
MATERIALISATION_LAG_MS = 14 * 3600 * 1000


def design_matrix(rows) -> np.ndarray:
    """Three features, scaled so plain gradient descent converges."""
    return np.array([[r.spend_7d / 1e5, r.txn_count_7d / 10.0,
                      r.recency_days / 7.0, 1.0] for r in rows])


def fit_logistic(x: np.ndarray, y: np.ndarray, steps: int = 4000,
                 lr: float = 0.5) -> np.ndarray:
    w = np.zeros(x.shape[1])
    for _ in range(steps):
        p = 1.0 / (1.0 + np.exp(-x @ w))
        w -= lr * (x.T @ (p - y)) / len(y)
    return w


def auc(scores: np.ndarray, labels: np.ndarray) -> float:
    """Rank-based AUC: P(score of a positive > score of a negative).

    Computed from ranks rather than by sweeping thresholds, so ties are
    handled correctly and there is no bin-count parameter to get wrong.
    """
    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(len(scores), dtype=float)
    ranks[order] = np.arange(1, len(scores) + 1)
    # Average ranks within ties.
    unique, inverse, counts = np.unique(scores, return_inverse=True,
                                        return_counts=True)
    sums = np.zeros(len(unique))
    np.add.at(sums, inverse, ranks)
    ranks = (sums / counts)[inverse]

    n_pos = labels.sum()
    n_neg = len(labels) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    return float((ranks[labels == 1].sum() - n_pos * (n_pos + 1) / 2) /
                 (n_pos * n_neg))


def main() -> None:
    events_path = DATA / "events.csv"
    labels_path = DATA / "labels.csv"
    if not events_path.exists():
        print("BLOCKED: run python3 python/seed_events.py first")
        raise SystemExit(2)

    events = load_events(str(events_path))
    with labels_path.open(newline="") as fh:
        label_map = {int(r["user_id"]): int(r["label"]) for r in csv.DictReader(fh)}

    offline_rows = compute_all(events, AS_OF_MS)
    user_ids = [r.user_id for r in offline_rows]
    online_rows = compute_all(events, AS_OF_MS - MATERIALISATION_LAG_MS,
                              user_ids=user_ids)

    y = np.array([label_map[u] for u in user_ids], dtype=float)
    x_offline = design_matrix(offline_rows)
    x_online = design_matrix(online_rows)

    print("Offline/online skew -- the partial current day")
    print(f"  users                  : {len(user_ids)}")
    print(f"  offline window         : [T-7d, T)")
    print(f"  online window          : [T-7d-{MATERIALISATION_LAG_MS // 3600000}h, "
          f"T-{MATERIALISATION_LAG_MS // 3600000}h)   "
          f"(a cached aggregate materialised at midnight)")
    print(f"  positives              : {int(y.sum())} of {len(y)}")

    shift = np.abs(x_online[:, :3] - x_offline[:, :3]).mean(axis=0)
    means = np.abs(x_offline[:, :3]).mean(axis=0)
    print(f"\n  mean absolute feature shift (offline -> online):")
    for name, s, m in zip(("spend_7d", "txn_count_7d", "recency_days"), shift, means):
        print(f"    {name:<14} {s:>10.4f}  ({100 * s / max(m, 1e-9):>5.1f}% of the "
              f"feature's own mean)")

    w_offline = fit_logistic(x_offline, y)
    auc_offline = auc(x_offline @ w_offline, y)
    auc_served = auc(x_online @ w_offline, y)

    w_online = fit_logistic(x_online, y)
    auc_retrained = auc(x_online @ w_online, y)

    print(f"\n  {'scenario':<46} {'AUC':>8}  {'Δ vs offline':>13}")
    print("  " + "-" * 72)
    print(f"  {'trained offline, scored offline (the notebook)':<46} "
          f"{auc_offline:>8.4f}  {'-':>13}")
    print(f"  {'trained offline, scored online (production)':<46} "
          f"{auc_served:>8.4f}  {auc_served - auc_offline:>+13.4f}")
    print(f"  {'retrained on the online definition':<46} "
          f"{auc_retrained:>8.4f}  {auc_retrained - auc_offline:>+13.4f}")

    print("\n  Same users, same labels, same weights in row 2 -- only the feature")
    print("  computation moved, by a fraction of a day. The offline number is the")
    print("  one that got written in the launch document.")
    print()
    print("  Row 3 is the fix people reach for -- retrain on what production")
    print("  actually computes -- and it is worth reading carefully, because how")
    print("  much it recovers depends on WHY the online feature is different. If")
    print("  the two paths merely disagree, retraining removes the mismatch. If")
    print("  the online path has genuinely less information about the label (here")
    print("  it is missing the most recent hours of behaviour, which is the part")
    print("  the label depends on most), retraining cannot restore what was never")
    print("  measured. Compare rows 2 and 3 in YOUR run before deciding which")
    print("  case you are in.")
    print()
    print("  Either way, retraining encodes the cache materialisation schedule")
    print("  into the model weights, so the next time somebody moves the nightly")
    print("  job by two hours this returns with no code change at all. The durable")
    print("  fix is the contract test in test_feature_contract.py, which fails")
    print("  when the two paths disagree regardless of why.")


if __name__ == "__main__":
    main()
