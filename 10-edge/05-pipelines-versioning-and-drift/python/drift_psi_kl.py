"""
Layer 10 - Topic 5: PSI, KL, and the false-alarm rate that decides whether
anyone still reads your drift alerts.

What this demonstrates
    Two distribution-shift measures computed properly, and then the thing
    that actually matters: whether a shift came with a quality drop.

        KL(P || Q) = sum_i p_i * ln(p_i / q_i)
        PSI        = sum_i (p_i - q_i) * ln(p_i / q_i)

    PSI is the symmetrised cousin. Its conventional thresholds -- >0.1
    investigate, >0.2 act -- are rules of thumb from credit-risk practice,
    not theory, and this file treats them as such.

    Two interventions are applied to the live window, and the whole point
    is that they look opposite to a drift monitor and to a metric:

      unit change      every amount scaled by 2.5, because an upstream
                       system started reporting the same money in a
                       different unit. A large, obvious distribution shift.
                       The model ranks by a monotone function of that
                       feature, so the ranking -- and therefore AUC -- is
                       unchanged. This is a FALSE ALARM, and it is the most
                       common one there is.
      relationship     amounts untouched, but the link between the feature
                       and the outcome is degraded. Barely any distribution
                       shift, and the model is genuinely worse. This is the
                       alert you wanted, and PSI does not raise it.

What to look for
    - Bin edges are derived ONCE from the training window and reused. Re-
      deriving them per window makes PSI approximately zero forever, which
      is the most common way this monitor is silently broken.
    - The bin-count sensitivity table. The same data gives materially
      different PSI at 5, 10 and 20 bins. A PSI reported without its bin
      count is not a number.
    - The joint-alert row: shift AND quality drop. Count how many PSI > 0.2
      alerts had no quality impact -- that is your false-alarm rate, and it
      is the entire argument for alerting on the conjunction.

Requires NumPy. Runs with no arguments, after seed_events.py:
    python3 python/drift_psi_kl.py
"""

from __future__ import annotations

import csv
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "python"))
from features import compute_all, load_events  # noqa: E402
from offline_online_skew import auc, design_matrix, fit_logistic  # noqa: E402

AS_OF_MS = 1_785_542_400_000
SMOOTH = 1e-6  # KL is undefined where q = 0, so every implementation smooths


def bin_edges(train: np.ndarray, bins: int) -> np.ndarray:
    """Quantile edges from the TRAINING window, derived once and frozen.

    Re-deriving these from each live window is the single most common way
    to build a drift monitor that can never fire: quantile bins of any
    distribution are uniform by construction, so PSI against them is
    always ~0.
    """
    qs = np.linspace(0, 100, bins + 1)
    edges = np.percentile(train, qs)
    edges[0] = -np.inf
    edges[-1] = np.inf
    return np.unique(edges)


def histogram(values: np.ndarray, edges: np.ndarray) -> np.ndarray:
    counts, _ = np.histogram(values, bins=edges)
    p = counts.astype(float) / max(1, counts.sum())
    return np.clip(p, SMOOTH, None)


def psi(p: np.ndarray, q: np.ndarray) -> float:
    return float(np.sum((p - q) * np.log(p / q)))


def kl(p: np.ndarray, q: np.ndarray) -> float:
    return float(np.sum(p * np.log(p / q)))


def main() -> None:
    if not (DATA / "events.csv").exists():
        print("BLOCKED: run python3 python/seed_events.py first")
        raise SystemExit(2)

    events = load_events(str(DATA / "events.csv"))
    with (DATA / "labels.csv").open(newline="") as fh:
        label_map = {int(r["user_id"]): int(r["label"]) for r in csv.DictReader(fh)}

    rows = compute_all(events, AS_OF_MS)
    ids = [r.user_id for r in rows]
    y = np.array([label_map[u] for u in ids], dtype=float)
    x = design_matrix(rows)

    # Train on the first half of the users; the rest is "live".
    split = len(ids) // 2
    w = fit_logistic(x[:split], y[:split])
    train_feature = np.array([r.spend_7d for r in rows[:split]], dtype=float)
    live_feature = np.array([r.spend_7d for r in rows[split:]], dtype=float)
    x_live, y_live = x[split:], y[split:]
    baseline_auc = auc(x_live @ w, y_live)

    print("Drift, and whether anyone should be woken up for it")
    print(f"  training window : {split} users")
    print(f"  live window     : {len(ids) - split} users")
    print(f"  baseline AUC on the live window: {baseline_auc:.4f}")

    edges = bin_edges(train_feature, 10)
    q = histogram(train_feature, edges)

    rng = np.random.default_rng(20260818)
    scenarios = {
        "none (same distribution)": (live_feature.copy(), x_live.copy()),
    }

    # A unit change: an upstream system starts reporting the same money in
    # a different unit. Large, obvious distribution shift; strictly
    # monotone, so a ranking metric cannot notice.
    scaled = live_feature * 2.5
    x_scaled = x_live.copy()
    x_scaled[:, 0] = scaled / 1e5
    scenarios["unit change (x2.5 on every amount)"] = (scaled, x_scaled)

    # The relationship degrades: the feature distribution is untouched, but
    # it is shuffled against the labels for a third of users -- a data
    # source silently going stale, a join key drifting, an upstream bug.
    shuffled = live_feature.copy()
    idx = rng.permutation(len(shuffled))[: len(shuffled) // 3]
    shuffled[idx] = shuffled[rng.permutation(idx)]
    x_shuffled = x_live.copy()
    x_shuffled[:, 0] = shuffled / 1e5
    scenarios["relationship degrades (stale join)"] = (shuffled, x_shuffled)

    print(f"\n  {'scenario':<38} {'PSI':>8} {'KL':>8} {'AUC':>8} "
          f"{'ΔAUC':>8} {'PSI verdict':>13} {'act?':>6}")
    print("  " + "-" * 96)
    false_alarms = 0
    psi_alerts = 0
    for name, (feature, design) in scenarios.items():
        p = histogram(feature, edges)
        psi_v = psi(p, q)
        kl_v = kl(p, q)
        a = auc(design @ w, y_live)
        delta = a - baseline_auc
        verdict = ("act" if psi_v > 0.2 else
                   "investigate" if psi_v > 0.1 else "quiet")
        quality_drop = delta < -0.01
        if psi_v > 0.2:
            psi_alerts += 1
            if not quality_drop:
                false_alarms += 1
        joint = "YES" if (psi_v > 0.2 and quality_drop) else "no"
        print(f"  {name:<38} {psi_v:>8.3f} {kl_v:>8.3f} {a:>8.4f} {delta:>+8.4f} "
              f"{verdict:>13} {joint:>6}")

    print(f"\n  PSI > 0.2 alerts: {psi_alerts}.  With no measurable quality impact: "
          f"{false_alarms}.")
    if psi_alerts:
        print(f"  False-alarm rate on this sample: "
              f"{100 * false_alarms / psi_alerts:.0f}%")
    print("  The `act?` column is the joint condition -- distribution shift AND")
    print("  an eval decline. It is the only column that should page anyone.")
    print("  Drift without a measurable quality drop is a false alarm, and false")
    print("  alarms burn on-call until nobody reads the channel.")
    print("  Note which scenario PSI missed entirely: the one that actually cost")
    print("  you AUC. A drift monitor is not a quality monitor and cannot become")
    print("  one -- it never looks at a label.")

    print("\n  Bin-count sensitivity (unit change, same data, same window):")
    print(f"    {'bins':>6} {'PSI':>8} {'KL':>8}")
    for bins in (5, 10, 20, 50):
        e = bin_edges(train_feature, bins)
        print(f"    {bins:>6} {psi(histogram(scaled, e), histogram(train_feature, e)):>8.3f} "
              f"{kl(histogram(scaled, e), histogram(train_feature, e)):>8.3f}")
    print("\n  Same shift, different numbers. Fix the bin count and the edges once,")
    print("  from the training window, and record both next to every PSI you")
    print("  report -- otherwise the threshold you are comparing against is")
    print("  meaningless.")


if __name__ == "__main__":
    main()
