"""
Lost update: the anomaly that lives in your round trip, not in the database.

    python3 01-isolation-levels/python/lost_update.py

WHAT IT DEMONSTRATES: 500 concurrent withdrawals of the same amount from one
account, five ways. Read-modify-write in Python loses updates at READ COMMITTED
even though every individual transaction is correct. The single-statement
UPDATE, at the same isolation level, does not lose a single one -- because at
READ COMMITTED an UPDATE that collides with a just-committed writer does not
abort: it waits, RE-EVALUATES its WHERE clause against the new row version, and
proceeds. The difference is not the isolation level. It is whether the decision
was made in the database or in your process.

WHAT TO LOOK FOR: the `lost` column, which is money that vanished from the
ledger's point of view (final balance higher than it should be, because writes
overwrote each other). Then look at what happens to the SAME read-modify-write
code at REPEATABLE READ: the losses become 40001 aborts. The bug does not go
away, it becomes visible -- which is the entire argument for a higher isolation
level plus a retry loop.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402

ACCOUNT = 1
START_BALANCE = 100_000
AMOUNT = 100
WITHDRAWALS = int(os.environ.get("WITHDRAWALS", 500))
WORKERS = int(os.environ.get("WORKERS", 20))
MAX_RETRIES = 8


def rmw(conn, lock_reads: bool) -> str:
    """SELECT, decide in Python, UPDATE with an absolute value. The shape of
    every "check the balance then subtract" handler ever written."""
    sql = "SELECT balance_cents FROM accounts WHERE id = %s"
    if lock_reads:
        sql += " FOR UPDATE"
    balance = conn.execute(sql, (ACCOUNT,)).fetchone()[0]
    if balance < AMOUNT:
        return "declined"
    conn.execute(
        "UPDATE accounts SET balance_cents = %s WHERE id = %s",
        (balance - AMOUNT, ACCOUNT),
    )
    return "ok"


def atomic(conn, lock_reads: bool) -> str:
    """One statement. The guard and the arithmetic are both inside the database,
    so no snapshot of yours can go stale between them."""
    updated = conn.execute(
        "UPDATE accounts SET balance_cents = balance_cents - %s "
        "WHERE id = %s AND balance_cents >= %s",
        (AMOUNT, ACCOUNT, AMOUNT),
    ).rowcount
    return "ok" if updated else "declined"


VARIANTS = [
    ("read-modify-write", "read committed", rmw, False),
    ("single-statement UPDATE", "read committed", atomic, False),
    ("read-modify-write", "repeatable read", rmw, False),
    ("single-statement UPDATE", "repeatable read", atomic, False),
    ("read-modify-write + FOR UPDATE", "read committed", rmw, True),
]


def run_variant(label: str, isolation: str, body, lock_reads: bool, setup) -> dict:
    setup.execute("UPDATE accounts SET balance_cents = %s WHERE id = %s", (START_BALANCE, ACCOUNT))
    per_worker = WITHDRAWALS // WORKERS

    def worker_body(worker: "lab_db.Worker", index: int) -> dict:
        counters = {"ok": 0, "declined": 0, "aborts": 0, "retries": 0, "gave_up": 0}
        latencies = []
        for _ in range(per_worker):
            t0 = time.perf_counter()
            for attempt in range(MAX_RETRIES + 1):
                try:
                    with worker.conn.transaction():
                        outcome = body(worker.conn, lock_reads)
                    counters[outcome] += 1
                    break
                except Exception as exc:  # noqa: BLE001
                    if lab_db.sqlstate(exc) != "40001":
                        raise
                    counters["aborts"] += 1
                    if attempt == MAX_RETRIES:
                        counters["gave_up"] += 1
                        break
                    counters["retries"] += 1
            latencies.append((time.perf_counter() - t0) * 1000)
        counters["latencies"] = latencies
        return counters

    t0 = time.perf_counter()
    results = lab_db.run_workers(WORKERS, worker_body, isolation=isolation)
    elapsed = time.perf_counter() - t0

    totals = {k: sum(r[k] for r in results) for k in ("ok", "declined", "aborts", "retries", "gave_up")}
    latencies = [ms for r in results for ms in r["latencies"]]
    final = setup.execute("SELECT balance_cents FROM accounts WHERE id = %s", (ACCOUNT,)).fetchone()[0]
    expected = START_BALANCE - totals["ok"] * AMOUNT
    return {
        "label": f"{label} @ {isolation}",
        "ok": totals["ok"],
        "final": final,
        "expected": expected,
        "lost": (final - expected) // AMOUNT,
        "aborts": totals["aborts"],
        "gave_up": totals["gave_up"],
        "p99": lab_db.percentile(latencies, 99),
        "rps": len(latencies) / elapsed,
    }


def main() -> None:
    lab_db.ensure_database()
    with lab_db.connect() as setup:
        lab_db.ensure_core_tables(setup)
        if setup.execute("SELECT count(*) FROM accounts").fetchone()[0] == 0:
            lab_db.reset_accounts(setup)
        lab_db.banner(f"Lost update -- {lab_db.describe_server(setup)}")
        print(f"account {ACCOUNT} starts at {START_BALANCE} cents; {WITHDRAWALS} withdrawals "
              f"of {AMOUNT} cents across {WORKERS} connections.\n")

        rows = [run_variant(*v, setup) for v in VARIANTS]

        header = (f"{'variant':<48}{'ok':>6}{'final':>9}{'expected':>10}{'lost':>7}"
                  f"{'40001':>7}{'gave up':>9}{'p99 ms':>9}{'req/s':>9}")
        print(header)
        print("-" * len(header))
        for r in rows:
            print(f"{r['label']:<48}{r['ok']:>6}{r['final']:>9}{r['expected']:>10}{r['lost']:>7}"
                  f"{r['aborts']:>7}{r['gave_up']:>9}{r['p99']:>9.1f}{r['rps']:>9.1f}")
        print()
        print("`ok` = withdrawals the caller was told succeeded; `gave up` = requests that exhausted")
        print(f"their {MAX_RETRIES} retries and returned an error -- at REPEATABLE READ those are the")
        print("withdrawals that never happened, which is why `expected` moves between rows.")
        print()
        print("`lost` = withdrawals that were confirmed to a caller and then overwritten by another")
        print("transaction's absolute UPDATE. `expected` is computed from the number of successful")
        print("withdrawals this run actually reported, so it is not a guess.")
        print()
        print("If the single-statement variant ever shows lost > 0, the experiment is broken, not")
        print("Postgres: one statement cannot lose an update, so you have two (or autocommit split")
        print("them). Check that the UPDATE really is a single round trip.")


if __name__ == "__main__":
    main()
