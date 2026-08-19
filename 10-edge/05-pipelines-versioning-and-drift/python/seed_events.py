"""
Layer 10 - Topic 5: deterministic event log, so three languages can be
given byte-identical input.

What this demonstrates
    Nothing on its own. It exists so that when golang/features.go and
    nodejs/features.js disagree with python/features.py, the input cannot
    be the explanation. Same file, same bytes, same rows, same order.

    The generator is seeded and pure. Re-running it overwrites the same
    content, so the three-way diff is reproducible across machines and
    across days.

What it writes
    data/events.csv   user_id, ts_ms, amount_cents
    data/labels.csv   user_id, label   (1 = the outcome the model predicts)

    Labels are generated from the TRUE 7-day spend at the prediction
    timestamp, plus noise, so a model trained on correctly-computed
    features has real signal to find -- which is what makes the offline vs
    online AUC gap in offline_online_skew.py mean something.

    Amounts are integer cents on purpose. Money in floats is its own
    Layer 3 topic; this topic has enough failure modes already.

Runs with no arguments:
    python3 python/seed_events.py
    python3 python/seed_events.py --days 60 --users 5000
"""

from __future__ import annotations

import argparse
import csv
import math
import pathlib
import random

DAY_MS = 86_400_000
SEED = 20260818
# T, the prediction timestamp: 2026-08-01T00:00:00Z, in epoch ms.
AS_OF_MS = 1_785_542_400_000


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--users", type=int, default=5000)
    ap.add_argument("--out", type=pathlib.Path,
                    default=pathlib.Path(__file__).resolve().parents[1] / "data")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    rng = random.Random(SEED)
    start_ms = AS_OF_MS - args.days * DAY_MS

    events: list[tuple[int, int, int]] = []
    for uid in range(1, args.users + 1):
        # Heterogeneous activity, so the 7-day window is sometimes empty,
        # sometimes one event, sometimes dozens. The empty case is the one
        # the three implementations disagree about most loudly.
        rate = rng.choice([0.0, 0.2, 1.0, 3.0])
        n = int(rng.expovariate(1 / max(rate, 0.01)) * args.days) if rate else 0
        for _ in range(min(n, 400)):
            ts = start_ms + rng.randrange(args.days * DAY_MS)
            amount = rng.randrange(50, 50_000)
            events.append((uid, ts, amount))

        # Deliberate boundary cases, one user in fifty: an event exactly on
        # the lower bound of the window and one exactly on T. The spec says
        # the first counts and the second does not, and every
        # reimplementation has to decide that for itself.
        if uid % 50 == 0:
            events.append((uid, AS_OF_MS - 7 * DAY_MS, 1000))
            events.append((uid, AS_OF_MS, 2000))

    events.sort(key=lambda e: (e[1], e[0]))

    events_path = args.out / "events.csv"
    with events_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["user_id", "ts_ms", "amount_cents"])
        w.writerows(events)

    # Labels from the TRUE window spend, so there is real signal.
    spend: dict[int, int] = {}
    for uid, ts, amount in events:
        if AS_OF_MS - 7 * DAY_MS <= ts < AS_OF_MS:
            spend[uid] = spend.get(uid, 0) + amount

    labels_path = args.out / "labels.csv"
    lrng = random.Random(SEED + 1)
    with labels_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["user_id", "label"])
        for uid in range(1, args.users + 1):
            s = spend.get(uid, 0)
            p = 1 / (1 + math.exp(-(s / 40_000 - 1.5)))
            w.writerow([uid, 1 if lrng.random() < p else 0])

    print("Deterministic event log written")
    print(f"  seed              : {SEED}")
    print(f"  prediction time T : {AS_OF_MS} ms  (2026-08-01T00:00:00Z)")
    print(f"  users             : {args.users}")
    print(f"  days of history   : {args.days}")
    print(f"  events            : {len(events)}")
    print(f"  {events_path}")
    print(f"  {labels_path}")
    print()
    print("  Every implementation in this topic reads this exact file, so when")
    print("  they disagree, the input is not the explanation.")


if __name__ == "__main__":
    main()
