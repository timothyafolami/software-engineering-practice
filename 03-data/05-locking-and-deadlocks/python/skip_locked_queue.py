"""
A work queue in one table: FOR UPDATE SKIP LOCKED, and what it costs.

    python3 05-locking-and-deadlocks/python/skip_locked_queue.py

WHAT IT DEMONSTRATES: three workers claiming jobs from the same table, run twice.

  A. SELECT ... FOR UPDATE SKIP LOCKED -- each worker claims rows nobody else
     has locked and never waits for anyone. Three workers do three workers'
     worth of work.
  B. SELECT ... FOR UPDATE (no SKIP LOCKED) -- every worker's claim query wants
     the same first N rows, ordered by id, so two of them block on the row the
     third is holding. They take turns instead of working in parallel, and
     throughput collapses toward what a single worker would manage on its own.
     Nothing errors and nothing warns.

Both variants are checked for duplicate processing: every claimed id is
collected and counted, so "no job ran twice" is verified rather than asserted.

WHAT TO LOOK FOR:
  * throughput, and the mean time per claim transaction. The per-worker claim
    counts stay roughly EVEN in both variants, which is the thing that hides
    this in production: the workers take turns fairly, so no single worker looks
    starved on a dashboard. What changed is that each transaction now spends
    most of its time waiting, and the mean-transaction-time line is where that
    shows up.
  * index size on `jobs`, before and after sustained churn, and again after
    VACUUM. A queue table is the most update-heavy table you will ever own:
    every job is inserted, updated and deleted, and the index pays for all
    three. Postgres' bottom-up index deletion improved this a lot and did not
    eliminate it -- which is Topic 2 arriving from a different direction.

AT-LEAST-ONCE, NOT EXACTLY-ONCE: this program claims and completes a job in ONE
transaction, which is why the duplicate count is zero. Real workers do the WORK
between the claim and the completion, and if the process dies in that window the
job's claim rolls back and another worker takes it -- correctly, and for the
second time. That is not a Postgres bug to fix; it is the reason the work itself
has to be idempotent.

Knobs: JOBS, WORKERS, BATCH, CHURN_S.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402
import psycopg  # noqa: E402

JOBS = int(os.environ.get("JOBS", "3000"))
WORKERS = int(os.environ.get("WORKERS", "3"))
BATCH = int(os.environ.get("BATCH", "10"))
CHURN_S = float(os.environ.get("CHURN_S", "15"))
# What a worker DOES with a claimed batch, inside the same transaction. Setting
# this to zero is the most common way this experiment is set up wrong: with no
# work between the claim and the commit, the row lock is held for microseconds
# and blocking barely registers. Real workers hold it for as long as the job
# takes, and that is exactly the window SKIP LOCKED is protecting.
WORK_MS = float(os.environ.get("WORK_MS", "5"))
QUEUE_INDEX = "idx_jobs_ready"

CLAIM = """
UPDATE jobs SET state = 'done', claimed_by = %s, claimed_at = now()
WHERE id IN (
    SELECT id FROM jobs
    WHERE state = 'ready'
    ORDER BY id
    FOR UPDATE {skip} LIMIT %s
)
RETURNING id
"""

_lock = threading.Lock()


def refill(conn, n: int) -> None:
    conn.execute("TRUNCATE jobs RESTART IDENTITY")
    conn.execute(
        "INSERT INTO jobs (payload, state) "
        "SELECT 'job payload ' || g, 'ready' FROM generate_series(1, %s) g", (n,))
    conn.execute("ANALYZE jobs")


def worker_body(skip_locked: bool, deadline: float, claimed: Counter):
    sql = CLAIM.format(skip="SKIP LOCKED" if skip_locked else "")

    def body(w: lab_db.Worker, index: int) -> dict:
        stats = {"claims": 0, "batches": 0, "empty": 0, "wait_ms": 0.0}
        while time.time() < deadline:
            t0 = time.perf_counter()
            try:
                with w.conn.transaction():
                    ids = [r[0] for r in w.conn.execute(sql, (w.name, BATCH)).fetchall()]
                    if ids:
                        time.sleep(WORK_MS / 1000.0)   # doing the job, lock still held
            except psycopg.Error as exc:
                if lab_db.sqlstate(exc) == "40P01":     # deadlock: possible in B
                    continue
                raise
            stats["wait_ms"] += (time.perf_counter() - t0) * 1000
            stats["batches"] += 1
            if not ids:
                stats["empty"] += 1
                break                                   # queue drained
            stats["claims"] += len(ids)
            with _lock:
                claimed.update(ids)
        return stats
    return body


def run_variant(label: str, skip_locked: bool) -> dict:
    with lab_db.connect() as conn:
        refill(conn, JOBS)

    claimed: Counter = Counter()
    deadline = time.time() + 120                        # a ceiling, not a target
    t0 = time.perf_counter()
    results = lab_db.run_workers(WORKERS, worker_body(skip_locked, deadline, claimed))
    elapsed = time.perf_counter() - t0

    with lab_db.connect() as conn:
        left = conn.execute("SELECT count(*) FROM jobs WHERE state = 'ready'").fetchone()[0]

    duplicates = sum(v - 1 for v in claimed.values() if v > 1)
    per_worker = [r["claims"] for r in results]
    return {
        "label": label,
        "claimed": sum(per_worker),
        "duplicates": duplicates,
        "left": left,
        "elapsed": elapsed,
        "rate": sum(per_worker) / elapsed if elapsed else 0,
        "per_worker": per_worker,
        "mean_batch_ms": sum(r["wait_ms"] for r in results) /
                         max(1, sum(r["batches"] for r in results)),
    }


def churn(conn) -> None:
    """Sustained insert/claim churn, to measure what a queue table does to its
    own index. Every job is inserted once, updated once, deleted once."""
    print(f"\n  churning the queue for {CHURN_S:.0f}s to measure index growth...")
    conn.execute(f"DROP INDEX IF EXISTS {QUEUE_INDEX}")
    refill(conn, 1000)
    conn.execute(f"CREATE INDEX {QUEUE_INDEX} ON jobs (state, id)")
    conn.execute("VACUUM (ANALYZE) jobs")

    def sizes():
        return (lab_db.table_bytes(conn, "jobs"),
                conn.execute("SELECT pg_relation_size(%s)", (QUEUE_INDEX,)).fetchone()[0])

    start_table, start_index = sizes()
    stop = time.time() + CHURN_S
    cycles = 0
    while time.time() < stop:
        conn.execute(CLAIM.format(skip="SKIP LOCKED"), ("churn", 500)).fetchall()
        conn.execute("DELETE FROM jobs WHERE state = 'done'")
        conn.execute("INSERT INTO jobs (payload, state) "
                     "SELECT 'job payload ' || g, 'ready' FROM generate_series(1, 500) g")
        cycles += 1
    end_table, end_index = sizes()
    stats = lab_db.table_stats(conn, "jobs")

    conn.execute("VACUUM jobs")
    vac_table, vac_index = sizes()

    print(f"    {cycles} claim/delete/insert cycles, {cycles * 500:,} jobs through the table")
    print(f"    {'':<22}{'table':>12}{'index':>12}")
    for label, (t, i) in (("before churn", (start_table, start_index)),
                          ("after churn", (end_table, end_index)),
                          ("after VACUUM", (vac_table, vac_index))):
        print(f"    {label:<22}{lab_db.human_bytes(t):>12}{lab_db.human_bytes(i):>12}")
    print(f"    dead tuples right after the churn: {stats['dead']:,}")
    print("    Plain VACUUM returns pages to the free space map, not to the OS, so the")
    print("    file does not shrink -- it stops growing. On a queue table that is usually")
    print("    enough; when it is not, the answer is REINDEX CONCURRENTLY on the index,")
    print("    not VACUUM FULL on the table.")
    conn.execute(f"DROP INDEX IF EXISTS {QUEUE_INDEX}")


def main() -> None:
    lab_db.ensure_database()
    with lab_db.connect() as conn:
        lab_db.ensure_core_tables(conn)
        lab_db.banner(f"SKIP LOCKED as a work queue -- {lab_db.describe_server(conn)}")
        print(f"{WORKERS} workers, {JOBS:,} jobs, batch of {BATCH} per transaction,")
        print(f"and {WORK_MS:.0f}ms of simulated work per batch INSIDE the transaction --")
        print("which is what makes the row lock exist for long enough to matter.")
        print("Both variants claim and complete in ONE transaction, so duplicates are")
        print("expected to be zero. Anything else would be a bug in the claim query.")

    print(f"\n  {'variant':<34}{'claimed':>9}{'dups':>7}{'left':>7}"
          f"{'seconds':>9}{'jobs/s':>10}{'per worker':>22}")
    print("  " + "-" * 100)
    results = []
    for label, skip in (("A. FOR UPDATE SKIP LOCKED", True),
                        ("B. FOR UPDATE (no SKIP LOCKED)", False)):
        r = run_variant(label, skip)
        results.append(r)
        split = "/".join(f"{n:,}" for n in r["per_worker"])
        print(f"  {r['label']:<34}{r['claimed']:>9,}{r['duplicates']:>7}{r['left']:>7}"
              f"{r['elapsed']:>9.2f}{r['rate']:>10.0f}{split:>22}")

    a, b = results
    print(f"\n  mean time per claim transaction: "
          f"{a['mean_batch_ms']:.2f}ms with SKIP LOCKED, {b['mean_batch_ms']:.2f}ms without.")
    print(f"  throughput ratio: {a['rate'] / max(b['rate'], 1e-9):.1f}x")
    print("  In B the extra time is not work -- it is two of the three workers blocked on")
    print("  a row lock held by the third. `ORDER BY id` makes every worker want the same")
    print("  rows, and without SKIP LOCKED wanting the same row means waiting for it.")
    print("  Note that the per-worker split stays roughly even. That is what makes this")
    print("  hard to see in production: the workers take turns fairly, so none of them")
    print("  looks starved. The loss is entirely in the mean transaction time, and the")
    print("  only symptom is that adding workers stops adding throughput.")

    if a["duplicates"] or b["duplicates"]:
        print("\n  DUPLICATES DETECTED. That is not a race in Postgres -- it means a claim")
        print("  was committed in a different transaction from the completion. Check that")
        print("  the UPDATE and the SELECT ... FOR UPDATE are in the same transaction.")

    with lab_db.connect() as conn:
        churn(conn)

    print("\n  Two things to take to a real queue:")
    print("    1. SKIP LOCKED gives you AT-LEAST-ONCE. A worker that dies between claiming")
    print("       and finishing hands its job back, and someone runs it again. Make the")
    print("       work idempotent -- a unique key on the effect, not on the job.")
    print("    2. Watch the index size, not just the table size. A queue table's index is")
    print("       where the churn shows up first, and it is the thing that silently makes")
    print("       your claim query slower month over month.")


if __name__ == "__main__":
    main()
