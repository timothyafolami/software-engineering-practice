"""
Layer 10 - Topic 5: the transform, and its one home.

THE SPEC. Everything in this topic is an argument about this docstring.

    Given an event log of (user_id, ts, amount_cents) and a prediction
    timestamp T (UTC), the feature vector for a user is:

        window        events with  T - 7 days <= ts < T
                      half-open, lower bound INCLUSIVE, upper EXCLUSIVE
        spend_7d      sum of amount_cents in the window, in cents (integer)
        txn_count_7d  number of events in the window (integer)
        avg_amount_7d spend_7d / txn_count_7d, in cents, rounded to 2 decimal
                      places using round-half-to-EVEN. Zero when the count
                      is zero -- not null, not NaN.
        recency_days  floor((T - ts_of_latest_event_in_window) / 1 day).
                      -1 when the window is empty.

    Timestamps are epoch milliseconds UTC throughout. There is no local
    time anywhere in this pipeline and there never will be.

Four decisions in there are the entire topic: which end of the window is
inclusive, which rounding mode, what an empty window returns, and what
timezone the arithmetic happens in. Each is individually arbitrary. Each is
individually reasonable to decide the other way. And each is decided
independently, and differently, by every reimplementation.

This module is the ONE HOME. golang/features.go and nodejs/features.js
exist to reproduce what happens when it is not -- and each of them also
ships a `conform` mode that follows this docstring to the letter, so you
can see that the gap closes only when somebody writes the spec down.

Runs with no arguments to self-check against a hand-computed case:
    python3 python/features.py
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, asdict
from decimal import ROUND_HALF_EVEN, Decimal

DAY_MS = 86_400_000
WINDOW_MS = 7 * DAY_MS


@dataclass(frozen=True)
class Event:
    user_id: int
    ts_ms: int
    amount_cents: int


@dataclass(frozen=True)
class Features:
    user_id: int
    spend_7d: int
    txn_count_7d: int
    avg_amount_7d: float
    recency_days: int


def round_half_even(value: Decimal, places: int = 2) -> float:
    """Round-half-to-even, as the spec says and as Python's round() does.

    Named and explicit rather than implicit, because the whole reason the
    Go and Node implementations disagree is that each of them reached for
    its own language's default and never wrote down which one that was.
    """
    quantum = Decimal(1).scaleb(-places)
    return float(value.quantize(quantum, rounding=ROUND_HALF_EVEN))


def compute(events: list[Event], user_id: int, as_of_ms: int) -> Features:
    lower = as_of_ms - WINDOW_MS
    window = [e for e in events
              if e.user_id == user_id and lower <= e.ts_ms < as_of_ms]
    if not window:
        return Features(user_id, 0, 0, 0.0, -1)
    spend = sum(e.amount_cents for e in window)
    count = len(window)
    avg = round_half_even(Decimal(spend) / Decimal(count))
    latest = max(e.ts_ms for e in window)
    return Features(user_id, spend, count, avg, (as_of_ms - latest) // DAY_MS)


def compute_all(events: list[Event], as_of_ms: int,
                user_ids: list[int] | None = None) -> list[Features]:
    if user_ids is None:
        user_ids = sorted({e.user_id for e in events})
    by_user: dict[int, list[Event]] = {}
    for e in events:
        by_user.setdefault(e.user_id, []).append(e)
    out = []
    for uid in user_ids:
        out.append(compute(by_user.get(uid, []), uid, as_of_ms))
    return out


def load_events(path: str) -> list[Event]:
    with open(path, newline="") as fh:
        return [Event(int(r["user_id"]), int(r["ts_ms"]), int(r["amount_cents"]))
                for r in csv.DictReader(fh)]


def write_features(path: str, rows: list[Features]) -> None:
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        for r in rows:
            d = asdict(r)
            d["avg_amount_7d"] = f"{d['avg_amount_7d']:.2f}"
            writer.writerow(d)


def _self_check() -> None:
    """Three cases chosen because each is a boundary the other
    implementations get wrong."""
    as_of = 7 * DAY_MS  # T = day 7
    cases = [
        # exactly on the lower bound: INCLUSIVE, so this counts
        ("lower bound inclusive",
         [Event(1, 0, 100)], Features(1, 100, 1, 100.0, 7)),
        # exactly on T: EXCLUSIVE, so this does not
        ("upper bound exclusive",
         [Event(1, as_of, 100)], Features(1, 0, 0, 0.0, -1)),
        # 5/2 = 2.5 cents: round-half-to-EVEN gives 2.5 -> 2.5 at 2dp, but
        # 12.345 -> 12.34 while half-up would give 12.35
        ("half-to-even",
         [Event(1, DAY_MS, 1234), Event(1, DAY_MS, 1235)],
         Features(1, 2469, 2, 1234.5, 6)),
    ]
    failures = 0
    print("features.py self-check -- the boundaries the spec exists to pin down")
    for name, events, expected in cases:
        got = compute(events, 1, as_of)
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            print(f"        expected {expected}")
            print(f"        got      {got}")
    print(f"\n  {'all boundaries pinned' if not failures else f'{failures} FAILED'}")
    print("  These three cases are exactly where golang/features.go and")
    print("  nodejs/features.js diverge in their native modes. Run")
    print("  python3 python/three_way_diff.py to see it at scale.")


if __name__ == "__main__":
    _self_check()
