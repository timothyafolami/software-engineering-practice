"""
Deadlocks under load, and the one line that makes them impossible.

    python3 05-locking-and-deadlocks/python/deadlock_ordering.py

WHAT IT DEMONSTRATES: the same `transfer(from_id, to_id, amount)` written twice.

  A. locks the two accounts rows in ARGUMENT order -- which is the order the
     caller happened to pass them in, i.e. no order at all.
  B. locks them in `ORDER BY id` -- one line different, and a cycle in the
     waits-for graph becomes impossible rather than rare.

Both run against the same rows, at the same concurrency, for the same number of
transfers. The only variable is the order two rows get locked in.

WHY A DEADLOCK IS EXPENSIVE EVEN WHEN YOU HANDLE IT: Postgres does not prevent
deadlocks, it DETECTS them, and detection is on a timer -- `deadlock_timeout`,
default one second. The victim waits that entire second before anything happens.
So a deadlock costs a second of latency even when your retry works perfectly,
and the p99 column below is where you see that rather than in the error count.

WHAT TO LOOK FOR:
  * `40P01` from the client and `deadlocks` from pg_stat_database. They should
    agree; if they do not, something is swallowing errors.
  * p99 latency in variant A -- look for ~1s, and recognise the number.
  * the server's own deadlock report, printed verbatim at the end. It names both
    processes and both statements, and it is one of the most useful messages
    Postgres emits. Learn to read it now rather than at 3am.

Knobs: ACCOUNTS (shrink to raise the collision rate), TRANSFERS, WORKERS.
"""
from __future__ import annotations

import os
import random
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402
import psycopg  # noqa: E402

ACCOUNTS = int(os.environ.get("ACCOUNTS", "16"))
TRANSFERS = int(os.environ.get("TRANSFERS", "240"))
WORKERS = int(os.environ.get("WORKERS", "12"))
MAX_RETRIES = 5

# What the handler does between locking the first row and locking the second.
# Pretending this is zero is what makes deadlocks look rare in a test and common
# in production: the wider the window, the more likely two transactions overlap.
THINK_S = 0.005

_lock = threading.Lock()
_reports: list[str] = []


def transfer(conn, a: int, b: int, cents: int, ordered: bool) -> None:
    """One transfer, inside one transaction.

    `ordered` decides the only thing that differs between the two variants: with
    it, both rows are locked by a single statement whose ORDER BY fixes the
    sequence for every transaction in the system. Without it, each transaction
    locks in whatever order its own arguments arrived in.
    """
    with conn.transaction():
        if ordered:
            # The whole fix, in one statement. Every transaction that touches
            # these two rows now acquires them low id first, so no two
            # transactions can hold what the other one wants.
            conn.execute(
                "SELECT id FROM accounts WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                ([a, b],),
            ).fetchall()
        else:
            conn.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (a,)).fetchone()
            time.sleep(THINK_S)
            conn.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (b,)).fetchone()
        conn.execute("UPDATE accounts SET balance_cents = balance_cents - %s WHERE id = %s",
                     (cents, a))
        conn.execute("UPDATE accounts SET balance_cents = balance_cents + %s WHERE id = %s",
                     (cents, b))


def worker_body(ordered: bool, per_worker: int):
    def body(w: lab_db.Worker, index: int) -> dict:
        rng = random.Random(1000 + index)
        stats = {"done": 0, "deadlocks": 0, "gave_up": 0, "latencies": []}
        for _ in range(per_worker):
            a, b = rng.sample(range(1, ACCOUNTS + 1), 2)
            t0 = time.perf_counter()
            for attempt in range(MAX_RETRIES):
                try:
                    transfer(w.conn, a, b, 1, ordered)
                    stats["done"] += 1
                    break
                except psycopg.errors.DeadlockDetected as exc:
                    stats["deadlocks"] += 1
                    with _lock:
                        if len(_reports) < 1 and exc.diag.message_detail:
                            _reports.append(
                                f"{exc.diag.message_primary}\nDETAIL: {exc.diag.message_detail}"
                                + (f"\nHINT: {exc.diag.message_hint}" if exc.diag.message_hint else "")
                            )
                    # Back off with jitter before retrying. Retrying instantly
                    # into the same contention is how a deadlock becomes a
                    # deadlock storm.
                    time.sleep(rng.uniform(0.001, 0.01))
                except psycopg.Error as exc:
                    if lab_db.sqlstate(exc) != "40001":
                        raise
                    time.sleep(rng.uniform(0.001, 0.01))
            else:
                stats["gave_up"] += 1
            stats["latencies"].append((time.perf_counter() - t0) * 1000)
        return stats
    return body


def run_variant(label: str, ordered: bool) -> dict:
    with lab_db.connect() as admin:
        lab_db.reset_accounts(admin, n=ACCOUNTS)
        before = admin.execute(
            "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()"
        ).fetchone()[0]
        total_before = admin.execute("SELECT sum(balance_cents) FROM accounts").fetchone()[0]

    per_worker = max(1, TRANSFERS // WORKERS)
    t0 = time.perf_counter()
    results = lab_db.run_workers(WORKERS, worker_body(ordered, per_worker))
    elapsed = time.perf_counter() - t0

    with lab_db.connect() as admin:
        after = admin.execute(
            "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()"
        ).fetchone()[0]
        total_after = admin.execute("SELECT sum(balance_cents) FROM accounts").fetchone()[0]

    latencies = [ms for r in results for ms in r["latencies"]]
    return {
        "label": label,
        "done": sum(r["done"] for r in results),
        "deadlocks": sum(r["deadlocks"] for r in results),
        "gave_up": sum(r["gave_up"] for r in results),
        "server_deadlocks": after - before,
        "elapsed": elapsed,
        "rate": sum(r["done"] for r in results) / elapsed,
        "p50": lab_db.percentile(latencies, 50),
        "p99": lab_db.percentile(latencies, 99),
        "conserved": total_before == total_after,
    }


def main() -> None:
    lab_db.ensure_database()
    with lab_db.connect() as conn:
        lab_db.ensure_core_tables(conn)
        lab_db.banner(f"Deadlocks and lock ordering -- {lab_db.describe_server(conn)}")
        dt = conn.execute("SHOW deadlock_timeout").fetchone()[0]
        print(f"deadlock_timeout = {dt}  (a DETECTION delay, not a limit: the victim waits")
        print("                        this long before Postgres even looks for a cycle)")
        print(f"{WORKERS} workers, {TRANSFERS} transfers, {ACCOUNTS} accounts "
              f"-- a small pool on purpose, so pairs collide")

    print(f"\n  {'variant':<26}{'ok':>7}{'40P01':>8}{'server':>8}{'gave up':>9}"
          f"{'txn/s':>9}{'p50 ms':>9}{'p99 ms':>10}{'balanced':>10}")
    print("  " + "-" * 96)
    results = []
    for label, ordered in (("A. argument order", False), ("B. ORDER BY id", True)):
        r = run_variant(label, ordered)
        results.append(r)
        print(f"  {r['label']:<26}{r['done']:>7,}{r['deadlocks']:>8,}{r['server_deadlocks']:>8,}"
              f"{r['gave_up']:>9,}{r['rate']:>9.0f}{r['p50']:>9.1f}{r['p99']:>10.1f}"
              f"{('yes' if r['conserved'] else 'NO'):>10}")

    a, b = results
    print()
    if a["deadlocks"] == 0:
        print("  Variant A produced no deadlocks. That is a BROKEN EXPERIMENT, not a result:")
        print("  the transfers are not overlapping. Re-run with a smaller pool and more")
        print("  workers, e.g. ACCOUNTS=20 WORKERS=16 TRANSFERS=800.")
    else:
        print(f"  Variant A: {a['deadlocks']} deadlocks. Variant B: {b['deadlocks']}.")
        print("  Not 'fewer' -- the ordering makes a waits-for CYCLE unconstructible, because")
        print("  a transaction can only ever wait on a row with a higher id than the ones it")
        print("  already holds. There is nothing left to be lucky about.")
    print(f"\n  p99: {a['p50']:.0f}ms -> {a['p99']:.0f}ms in A, "
          f"{b['p50']:.0f}ms -> {b['p99']:.0f}ms in B.")
    print("  If A's p99 is around 1000ms, that is deadlock_timeout, visible in your latency")
    print("  distribution. Every deadlock costs a full second of waiting before detection,")
    print("  and a retry that succeeds instantly does not give that second back.")

    if _reports:
        print("\n  the server's own deadlock report -- read it closely:")
        for line in _reports[0].splitlines():
            print(f"    {line}")
        print("    It names both processes and both statements. `log_lock_waits = on` puts")
        print("    the same information in the log for waits that do NOT become deadlocks.")
    else:
        print("\n  (no deadlock report captured -- variant A did not deadlock this run)")

    print("\n  One thing variant B does NOT protect you from: a second lock source you did")
    print("  not choose. A foreign-key check, a trigger, or a unique index insertion takes")
    print("  locks in an order you did not write. If ordered transfers still deadlock, the")
    print("  report above names the other source -- that is a finding, not a failure.")


if __name__ == "__main__":
    main()
