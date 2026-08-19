"""
What SERIALIZABLE costs, and why the cost depends on your query plans.

    python3 01-isolation-levels/python/serializable_cost.py

WHAT IT DEMONSTRATES: the same SERIALIZABLE workload, run twice against the same
40,000-row table -- once where the read can use an index, once where it cannot.
SSI takes predicate locks (SIReadLock) on what a transaction READ. An index scan
locks the tuples and pages it actually touched. A SEQUENTIAL SCAN read the whole
relation, so it takes a RELATION-level predicate lock, and every concurrent
writer to that table now conflicts with it. Nothing about your data or your
transactions changed. One dropped index moved the abort rate.

WHAT TO LOOK FOR: the 40001 and `retries/req` columns between the two rows, and
the `SIReadLock granularity` line under each -- tuple/page locks with the index,
a relation lock without it. This is why "Topics 3 and 4 are load-bearing for
Topic 1": at SERIALIZABLE, a plan regression is an availability incident.
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402

TOTAL_SHIFTS = int(os.environ.get("TOTAL_SHIFTS", 20_000))
HOT_SHIFTS = int(os.environ.get("HOT_SHIFTS", 100))
PAIRS_IN_FLIGHT = int(os.environ.get("PAIRS_IN_FLIGHT", 10))
MAX_RETRIES = 5
THINK_SECONDS = 0.003

DDL = """
CREATE TABLE IF NOT EXISTS oncall_big (
    shift_id  int     NOT NULL,
    doctor_id int     NOT NULL,
    on_call   boolean NOT NULL
);
"""


def reset(setup) -> None:
    setup.execute(DDL)
    setup.execute("TRUNCATE oncall_big")
    setup.execute(
        "INSERT INTO oncall_big (shift_id, doctor_id, on_call) "
        "SELECT s, d, true FROM generate_series(1, %s) s, generate_series(1, 2) d",
        (TOTAL_SHIFTS,),
    )
    setup.execute("ANALYZE oncall_big")


class LockSampler(threading.Thread):
    """Sample pg_locks during the run. The granularity of the predicate locks IS
    the finding here, and it is only observable while the transactions are live."""

    def __init__(self):
        super().__init__(daemon=True)
        self.stop = threading.Event()
        self.seen: dict[str, int] = {}

    def run(self) -> None:
        conn = lab_db.connect()
        try:
            while not self.stop.is_set():
                rows = conn.execute(
                    "SELECT locktype, count(*) FROM pg_locks "
                    "WHERE mode = 'SIReadLock' GROUP BY locktype"
                ).fetchall()
                for locktype, n in rows:
                    self.seen[locktype] = max(self.seen.get(locktype, 0), n)
                time.sleep(0.02)
        finally:
            conn.close()


def go_off_call(conn, shift: int, doctor: int) -> dict:
    counters = {"aborts": 0, "retries": 0, "gave_up": 0, "wrote": 0}
    for attempt in range(MAX_RETRIES + 1):
        try:
            with conn.transaction():
                on_call = conn.execute(
                    "SELECT count(*) FROM oncall_big WHERE shift_id = %s AND on_call",
                    (shift,),
                ).fetchone()[0]
                time.sleep(THINK_SECONDS)
                if on_call > 1:
                    conn.execute(
                        "UPDATE oncall_big SET on_call = false "
                        "WHERE shift_id = %s AND doctor_id = %s",
                        (shift, doctor),
                    )
                    counters["wrote"] += 1
            return counters
        except Exception as exc:  # noqa: BLE001
            if lab_db.sqlstate(exc) != "40001":
                raise
            counters["aborts"] += 1
            counters["wrote"] = 0
            if attempt == MAX_RETRIES:
                counters["gave_up"] += 1
                return counters
            counters["retries"] += 1
    return counters


def plan_for_read(setup, shift: int) -> str:
    explained = lab_db.explain(
        setup, "SELECT count(*) FROM oncall_big WHERE shift_id = %s AND on_call", (shift,)
    )
    return lab_db.scan_summary(explained)


def run_variant(label: str, setup) -> dict:
    reset(setup)
    rounds = HOT_SHIFTS // PAIRS_IN_FLIGHT
    n_workers = PAIRS_IN_FLIGHT * 2
    start_together = threading.Barrier(n_workers)
    plan = plan_for_read(setup, 1)

    def body(worker: "lab_db.Worker", index: int) -> dict:
        totals = {"aborts": 0, "retries": 0, "gave_up": 0, "wrote": 0}
        latencies = []
        for r in range(rounds):
            # Spread the hot shifts across the table so a seq scan really does
            # have to walk it, rather than living in one hot page.
            shift = 1 + (r * PAIRS_IN_FLIGHT + index // 2) * (TOTAL_SHIFTS // HOT_SHIFTS)
            doctor = (index % 2) + 1
            start_together.wait()
            t0 = time.perf_counter()
            got = go_off_call(worker.conn, shift, doctor)
            latencies.append((time.perf_counter() - t0) * 1000)
            for k, v in got.items():
                totals[k] += v
        totals["latencies"] = latencies
        return totals

    sampler = LockSampler()
    sampler.start()
    t0 = time.perf_counter()
    results = lab_db.run_workers(n_workers, body, isolation="serializable")
    elapsed = time.perf_counter() - t0
    sampler.stop.set()
    sampler.join(timeout=2)

    totals = {k: sum(r[k] for r in results) for k in ("aborts", "retries", "gave_up", "wrote")}
    latencies = [ms for r in results for ms in r["latencies"]]
    broken = setup.execute(
        "SELECT count(*) FROM (SELECT shift_id FROM oncall_big GROUP BY shift_id "
        "HAVING sum(on_call::int) = 0) t"
    ).fetchone()[0]
    return {
        "label": label,
        "plan": plan,
        "broken": broken,
        "aborts": totals["aborts"],
        "gave_up": totals["gave_up"],
        "retries_per_req": totals["retries"] / len(latencies),
        "p99": lab_db.percentile(latencies, 99),
        "rps": len(latencies) / elapsed,
        "locks": dict(sorted(sampler.seen.items())),
    }


def main() -> None:
    lab_db.ensure_database()
    with lab_db.connect() as setup:
        lab_db.banner(f"The cost of SERIALIZABLE -- {lab_db.describe_server(setup)}")
        print(f"{TOTAL_SHIFTS:,} shifts x 2 doctors; {HOT_SHIFTS} of them contended, "
              f"{PAIRS_IN_FLIGHT * 2} connections, every transaction SERIALIZABLE.\n")

        rows = []
        setup.execute(DDL)
        setup.execute("CREATE INDEX IF NOT EXISTS idx_oncall_big_shift ON oncall_big (shift_id)")
        rows.append(run_variant("index on shift_id", setup))
        setup.execute("DROP INDEX IF EXISTS idx_oncall_big_shift")
        rows.append(run_variant("no index (seq scan)", setup))
        # Leave the table indexed for whoever runs this next.
        setup.execute("CREATE INDEX IF NOT EXISTS idx_oncall_big_shift ON oncall_big (shift_id)")

        header = f"{'variant':<24}{'broken':>8}{'40001':>8}{'gave up':>9}{'retries/req':>13}{'p99 ms':>9}{'req/s':>9}"
        print(header)
        print("-" * len(header))
        for r in rows:
            print(f"{r['label']:<24}{r['broken']:>8}{r['aborts']:>8}{r['gave_up']:>9}"
                  f"{r['retries_per_req']:>13.2f}{r['p99']:>9.1f}{r['rps']:>9.1f}")
        print()
        for r in rows:
            locks = ", ".join(f"{k}={v}" for k, v in r["locks"].items()) or "none sampled"
            print(f"  {r['label']:<22} plan: {r['plan']}")
            print(f"  {'':<22} peak SIReadLock granularity: {locks}")
        print()
        print("SIReadLocks do not block and cannot deadlock -- they exist so SSI can detect a")
        print("dependency cycle and abort one transaction. `relation` in that line means one")
        print("transaction's read conflicts with every concurrent write to the table.")
        print()
        print("Zero aborts in BOTH rows means there is no real contention: check that the hot")
        print("shifts overlap and that both doctors of a shift are running at the same time.")


if __name__ == "__main__":
    main()
