"""
Head-of-line blocking through a shared pool, and the bulkhead that stops it.

    python3 07-connection-pools/python/bulkhead.py

WHAT IT DEMONSTRATES: one slow endpoint taking a fifth of the traffic, and what
it does to the p99 of the FAST endpoints -- whose queries did not change, whose
data did not change, and which are now slow because of something else entirely.

The mechanism is not subtle once you see it: a pool is a fixed number of slots.
A 500ms request holds a slot for 500ms. At 20% of traffic, slow requests occupy
most of the pool most of the time, and the fast requests -- which need a slot for
two milliseconds -- queue behind them. Every fast endpoint in the service now has
the slow endpoint's latency in its tail.

THE FIX IS A BULKHEAD: give the slow endpoint its OWN pool, sized for it. It can
then exhaust its own pool without touching anybody else's. Add a
`statement_timeout` on that pool so a slow query cannot become an unbounded one.
The name comes from ships: compartments that flood independently.

WHAT TO LOOK FOR: the fast endpoint's p99 across the three scenarios. The
absolute numbers are yours; the shape is what transfers. Note also that the
bulkhead makes the SLOW endpoint's numbers worse -- it now queues against a
smaller pool. That is the trade, made deliberately: you decided which traffic
gets protected instead of letting the pool decide by arrival order.

Knobs: FAST_RATE, SLOW_RATE, SLOW_SECONDS, DURATION_S, POOL_SIZE, SLOW_POOL_SIZE,
STATEMENT_TIMEOUT_MS.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pool_lab  # noqa: E402
from sqlalchemy import text  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402

FAST_RATE = float(os.environ.get("FAST_RATE", "160"))
SLOW_RATE = float(os.environ.get("SLOW_RATE", "40"))
SLOW_SECONDS = float(os.environ.get("SLOW_SECONDS", "0.5"))
DURATION_S = float(os.environ.get("DURATION_S", "8"))
POOL_SIZE = int(os.environ.get("POOL_SIZE", "10"))
SLOW_POOL_SIZE = int(os.environ.get("SLOW_POOL_SIZE", "3"))
STATEMENT_TIMEOUT_MS = int(os.environ.get("STATEMENT_TIMEOUT_MS", "300"))

FAST_SQL = text("SELECT id, status, total_cents FROM orders WHERE id = :id")


def fast_handler(engine, scheduled_at, result) -> None:
    try:
        with engine.connect() as conn:
            conn.execute(FAST_SQL, {"id": 424242}).fetchone()
        result.ok(scheduled_at)
    except Exception as exc:  # noqa: BLE001
        result.fail(pool_lab.classify(exc), scheduled_at)


def slow_handler_factory(statement_timeout_ms: int | None):
    def slow_handler(engine, scheduled_at, result) -> None:
        try:
            with engine.connect() as conn:
                if statement_timeout_ms:
                    # SET LOCAL would need a transaction; this is a plain session
                    # setting, reset when the connection returns to the pool
                    # because SQLAlchemy issues a rollback on checkin.
                    conn.execute(text(f"SET statement_timeout = {statement_timeout_ms}"))
                pool_lab.do_slow(conn, SLOW_SECONDS)
            result.ok(scheduled_at)
        except Exception as exc:  # noqa: BLE001
            result.fail(pool_lab.classify(exc), scheduled_at)
    return slow_handler


def run_scenario(label: str, shared: bool, statement_timeout_ms: int | None) -> dict:
    """Run fast and slow traffic together, against one pool or two."""
    import threading

    fast_engine = pool_lab.make_engine(POOL_SIZE, 0, 2, app_name="sep-fast")
    slow_engine = (fast_engine if shared
                   else pool_lab.make_engine(SLOW_POOL_SIZE, 0, 2, app_name="sep-slow"))

    fast_result, slow_result = pool_lab.Result(), pool_lab.Result()
    slow_handler = slow_handler_factory(statement_timeout_ms)

    threads = [
        threading.Thread(target=pool_lab.open_loop,
                         args=(fast_engine, fast_handler, FAST_RATE, DURATION_S, fast_result),
                         daemon=True),
        threading.Thread(target=pool_lab.open_loop,
                         args=(slow_engine, slow_handler, SLOW_RATE, DURATION_S, slow_result),
                         daemon=True),
    ]
    import time
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(DURATION_S + 60)
    elapsed = time.perf_counter() - t0

    fast_engine.dispose()
    if not shared:
        slow_engine.dispose()
    return {"label": label, "fast": fast_result.summary(elapsed),
            "slow": slow_result.summary(elapsed)}


def baseline() -> dict:
    """Fast traffic alone. The number the other scenarios are a regression from."""
    import time
    engine = pool_lab.make_engine(POOL_SIZE, 0, 2, app_name="sep-fast")
    result = pool_lab.Result()
    t0 = time.perf_counter()
    pool_lab.open_loop(engine, fast_handler, FAST_RATE, DURATION_S, result)
    elapsed = time.perf_counter() - t0
    engine.dispose()
    return {"label": "fast traffic only (baseline)", "fast": result.summary(elapsed),
            "slow": None}


def show(row: dict) -> None:
    f, s = row["fast"], row["slow"]
    errs = ", ".join(f"{k}={v}" for k, v in sorted(f["errors"].items())) or "-"
    print(f"  {row['label']:<34}{f['rate']:>8.0f}{f['p50']:>9.0f}{f['p99']:>9.0f}"
          f"  {errs[:24]:<26}", end="")
    serrs = "-"
    if s:
        serrs = ", ".join(f"{k}={v}" for k, v in sorted(s["errors"].items())) or "-"
        print(f"{s['rate']:>7.0f}{s['p99']:>9.0f}  {serrs[:22]}")
    else:
        print(f"{'-':>7}{'-':>9}")
    # `statement timeout` vs `pool timeout` is the distinction the third row is
    # for, so the column is repeated in full rather than cut off at the margin.
    if len(errs) > 24 or len(serrs) > 22:
        print(f"  {'':<34}fast errors in full: {errs}")
        if s:
            print(f"  {'':<34}slow errors in full: {serrs}")


def main() -> None:
    pool_lab.prepare()
    lab_db.banner("Head-of-line blocking, and the bulkhead")
    print(f"  fast endpoint: a primary-key lookup, {FAST_RATE:.0f} req/s")
    print(f"  slow endpoint: {SLOW_SECONDS * 1000:.0f}ms of pg_sleep, {SLOW_RATE:.0f} req/s "
          f"({SLOW_RATE / (FAST_RATE + SLOW_RATE) * 100:.0f}% of traffic)")
    print(f"  shared pool: {POOL_SIZE} connections. Separate slow pool: {SLOW_POOL_SIZE}.")
    print(f"\n  Arithmetic to do first: {SLOW_RATE:.0f} req/s x {SLOW_SECONDS:.1f}s = "
          f"{SLOW_RATE * SLOW_SECONDS:.0f} connections' worth of slow work,")
    print(f"  offered to a pool of {POOL_SIZE}. Predict the fast endpoint's p99 before you run it.")

    print(f"\n  {'scenario':<34}{'fast/s':>8}{'p50':>9}{'p99':>9}  {'fast errors':<26}"
          f"{'slow/s':>7}{'slow p99':>9}  slow errors")
    print("  " + "-" * 116)

    rows = [
        baseline(),
        run_scenario("shared pool", shared=True, statement_timeout_ms=None),
        run_scenario(f"separate pools + {STATEMENT_TIMEOUT_MS}ms timeout",
                     shared=False, statement_timeout_ms=STATEMENT_TIMEOUT_MS),
    ]
    for row in rows:
        show(row)

    base, shared, split = (r["fast"] for r in rows)
    print(f"\n  fast-endpoint p99: {base['p99']:.0f}ms alone, "
          f"{shared['p99']:.0f}ms sharing a pool, {split['p99']:.0f}ms behind a bulkhead.")
    if shared["p99"] > base["p99"] * 2:
        print(f"  Sharing the pool multiplied the fast endpoint's p99 by "
              f"{shared['p99'] / max(base['p99'], 1e-9):.0f}x. Its query did not change. Its")
        print("  data did not change. It is queueing behind a slot held by somebody else.")
    print("  The bulkhead gives that back by capping how much of the resource the slow")
    print("  endpoint can hold at once -- and the slow endpoint's own numbers get worse,")
    print("  which is the trade, made on purpose rather than by arrival order.")
    print()
    print("  statement_timeout is the second half and does something different: it bounds")
    print("  how long ONE slow query can hold its slot. Without it a query that goes from")
    print("  500ms to 30 seconds under load takes the slow pool with it, and a bulkhead")
    print("  around an unbounded thing is just a smaller unbounded thing.")
    print()
    print("  Note which errors appeared where. Under the shared pool the FAST endpoint")
    print("  starts failing -- an endpoint that is not slow, returning errors, because of")
    print("  a resource it shares with one that is. That is the incident, and 'the database")
    print("  is slow' is what it will be reported as.")


if __name__ == "__main__":
    main()
