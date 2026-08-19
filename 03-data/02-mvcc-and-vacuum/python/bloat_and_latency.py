"""
Vacuum starvation, watched in real time: bloat, heap fetches, and p99.

    python3 02-mvcc-and-vacuum/python/bloat_and_latency.py

WHAT IT DEMONSTRATES: the incident where latency climbs for hours on flat traffic
and nobody deployed anything. A writer churns rows continuously while one session
holds the xmin horizon, so autovacuum runs and reclaims nothing. Every sample
records table size, index size, dead tuples, `Heap Fetches` from an index-only
scan, and the latency of two read queries. Then the horizon is released and the
table is recovered twice -- plain VACUUM, then VACUUM FULL -- so you can see
which one gives the disk space back.

WHAT TO LOOK FOR:
  * `heap_fetches` going from 0 to large. The plan does NOT change: EXPLAIN still
    says "Index Only Scan". The visibility map stopped saying all-visible, so the
    scan quietly started fetching heap pages after all. This is the sneakiest
    line in the whole layer, and it is invisible unless you read that field.
  * the two latency columns diverging from the baseline, and which one moves first.
  * `table_mb` after plain VACUUM vs after VACUUM FULL. Plain VACUUM returns pages
    to the free space map, not to the operating system.

Duration: DURATION_S (default 120) sampling every SAMPLE_S (default 20). The
layer README's version runs 15+ minutes: DURATION_S=900 SAMPLE_S=30.
"""
from __future__ import annotations

import csv
import os
import random
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402
import mvcc_lab  # noqa: E402

ROWS = int(os.environ.get("ROWS", 300_000))
DURATION_S = int(os.environ.get("DURATION_S", 120))
SAMPLE_S = int(os.environ.get("SAMPLE_S", 20))
PROBES_PER_SAMPLE = int(os.environ.get("PROBES_PER_SAMPLE", 150))
CHURN_BATCH = int(os.environ.get("CHURN_BATCH", 5_000))

POINT_LOOKUP = f"SELECT total_cents FROM {mvcc_lab.TABLE} WHERE id = %s"
# An index-only-scan query: everything it needs is in idx_mvcc_customer, which
# INCLUDEs total_cents. The span is deliberately narrow -- widen it and the
# planner switches to a bitmap heap scan, which can never be index-only, and the
# Heap Fetches line disappears along with the point of the measurement.
RANGE_SPAN = int(os.environ.get("RANGE_SPAN", 5))
RANGE_SCAN = (f"SELECT sum(total_cents) FROM {mvcc_lab.TABLE} "
              f"WHERE customer_id BETWEEN %s AND %s")


class Writer(threading.Thread):
    """Continuous write load on an indexed column: every update is a new row
    version at a new location, and a new entry in every index."""

    def __init__(self):
        super().__init__(daemon=True)
        self.stop = threading.Event()
        self.updates = 0

    def run(self) -> None:
        conn = lab_db.connect()
        try:
            while not self.stop.is_set():
                mvcc_lab.churn(conn, CHURN_BATCH, ROWS)
                self.updates += CHURN_BATCH
        except Exception as exc:  # noqa: BLE001
            print(f"  [writer] stopped: {exc}")
        finally:
            conn.close()


def probe_latency(conn, n: int) -> tuple[float, float, float, float]:
    point, ranged = [], []
    for _ in range(n):
        i = random.randint(1, ROWS)
        t0 = time.perf_counter()
        conn.execute(POINT_LOOKUP, (i,)).fetchone()
        point.append((time.perf_counter() - t0) * 1000)

        lo = random.randint(1, 49_000)
        t0 = time.perf_counter()
        conn.execute(RANGE_SCAN, (lo, lo + RANGE_SPAN)).fetchone()
        ranged.append((time.perf_counter() - t0) * 1000)
    return (lab_db.percentile(point, 50), lab_db.percentile(point, 99),
            lab_db.percentile(ranged, 50), lab_db.percentile(ranged, 99))


def heap_fetches(conn) -> tuple[int, str]:
    lo = random.randint(1, 49_000)
    explained = lab_db.explain(conn, RANGE_SCAN, (lo, lo + RANGE_SPAN))
    node = lab_db.node_by_type(explained, "Index Only Scan")
    if node is None:
        # Not an index-only scan any more, so there is no Heap Fetches to read.
        # Printed as -1 rather than 0 so it can never be mistaken for "no fetches".
        return -1, lab_db.scan_summary(explained)
    return int(node.get("Heap Fetches", 0) or 0), lab_db.scan_summary(explained)


def sample(conn, label: str, elapsed: float) -> dict:
    s = mvcc_lab.stats(conn)
    fetches, plan = heap_fetches(conn)
    p50, p99, rp50, rp99 = probe_latency(conn, PROBES_PER_SAMPLE)
    row = {
        "label": label,
        "elapsed_s": round(elapsed),
        "dead_tuples": s["dead"],
        "hot_updates": s["hot"],
        "autovacuums": s["autovacuums"],
        "table_mb": round(s["table_bytes"] / 1024 / 1024, 1),
        "index_mb": round(s["index_bytes"] / 1024 / 1024, 1),
        "heap_fetches": fetches,
        "point_p50_ms": round(p50, 3),
        "point_p99_ms": round(p99, 3),
        "range_p50_ms": round(rp50, 3),
        "range_p99_ms": round(rp99, 3),
        "plan": plan,
    }
    print(f"  {label:<20}{row['elapsed_s']:>6}s{row['dead_tuples']:>12,}"
          f"{row['table_mb']:>10.1f}{row['index_mb']:>10.1f}{row['heap_fetches']:>14,}"
          f"{row['point_p99_ms']:>11.3f}{row['range_p50_ms']:>11.3f}{row['range_p99_ms']:>11.3f}")
    return row


def header() -> None:
    print(f"  {'phase':<20}{'time':>7}{'dead tuples':>12}{'table MB':>10}{'index MB':>10}"
          f"{'heap fetches':>14}{'point p99':>11}{'range p50':>11}{'range p99':>11}")
    print("  " + "-" * 106)


def main() -> None:
    lab_db.ensure_database()
    with lab_db.connect() as conn:
        lab_db.tune_session(conn)
        lab_db.banner(f"Vacuum starvation and latency -- {lab_db.describe_server(conn)}")
        print(f"{ROWS:,} rows, {DURATION_S}s of starvation sampled every {SAMPLE_S}s, "
              f"{PROBES_PER_SAMPLE} probes per sample.\n")

        mvcc_lab.ensure_table(conn, ROWS)
        conn.execute(f"VACUUM (FULL, ANALYZE) {mvcc_lab.TABLE}")
        rows = []
        header()
        rows.append(sample(conn, "baseline", 0))

        holder = lab_db.connect(autocommit=False)
        holder.execute("SELECT set_config('application_name', 'sep-horizon-holder', false)")
        holder.execute("SET default_transaction_isolation = 'repeatable read'")
        holder.commit()
        holder_pid = holder.execute("SELECT pg_backend_pid()").fetchone()[0]
        holder.execute("SELECT 1")  # opens the transaction, and the snapshot
        print(f"\n  starving the horizon: pid {holder_pid} is idle in a REPEATABLE READ transaction")

        writer = Writer()
        writer.start()
        t0 = time.perf_counter()
        next_sample = SAMPLE_S
        while time.perf_counter() - t0 < DURATION_S:
            time.sleep(0.5)
            elapsed = time.perf_counter() - t0
            if elapsed >= next_sample:
                rows.append(sample(conn, "starved", elapsed))
                next_sample += SAMPLE_S
        writer.stop.set()
        writer.join(timeout=30)
        print(f"\n  writer performed ~{writer.updates:,} non-HOT updates")

        holder.rollback()
        holder.close()
        print(f"  released the horizon (pid {holder_pid} closed)\n")

        conn.execute(f"VACUUM (ANALYZE) {mvcc_lab.TABLE}")
        rows.append(sample(conn, "after VACUUM", time.perf_counter() - t0))
        conn.execute(f"VACUUM (FULL, ANALYZE) {mvcc_lab.TABLE}")
        rows.append(sample(conn, "after VACUUM FULL", time.perf_counter() - t0))

        out = os.path.join(os.environ.get("LAB_OUT", tempfile.gettempdir()), "mvcc_bloat_samples.csv")
        with open(out, "w", newline="") as fh:
            writer_csv = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer_csv.writeheader()
            writer_csv.writerows(rows)
        print(f"\n  samples written to {out}")
        for plan in dict.fromkeys(r["plan"] for r in rows):
            print(f"  plan seen for the probe query: {plan}")
        if any(r["heap_fetches"] < 0 for r in rows):
            print("  (heap_fetches = -1 means that sample was not an index-only scan at all --")
            print("   under enough bloat the planner gives up on it, which is its own finding)")
        print()
        print("Plain VACUUM returns pages to the free space map; the FILE does not shrink, which")
        print("is why `table MB` moves at `after VACUUM FULL` and not before. VACUUM FULL takes an")
        print("ACCESS EXCLUSIVE lock -- on a production table that is an outage, and the reason")
        print("pg_repack exists.")
        print()
        print("One more thing worth noticing in the last two rows: after plain VACUUM the")
        print("index-only scan reports 0 heap fetches, and after VACUUM FULL it does not. VACUUM")
        print("FULL rewrites the table and leaves the visibility map unset, so index-only scans")
        print("start fetching heap pages again until something re-vacuums it. The same applies")
        print("after pg_repack. ANALYZE does not fix it; a plain VACUUM does.")
        print()
        print("If dead tuples stay near zero: your updates went HOT (update an INDEXED column).")
        print("If p99 rises while dead tuples stay flat: you are watching autovacuum compete for")
        print("I/O, which is a different and also real effect -- check pg_stat_progress_vacuum.")


if __name__ == "__main__":
    main()
