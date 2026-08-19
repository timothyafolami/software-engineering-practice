"""
Write skew, reproduced under real concurrency, then fixed -- in one program.

    python3 01-isolation-levels/python/write_skew.py

WHAT IT DEMONSTRATES: 100 shifts, 2 doctors each, invariant "at least one doctor
per shift is on call". Both doctors of a shift try to go off call at the same
moment. Each transaction reads the same rows, decides its own write is safe, and
writes a DIFFERENT row -- so nothing conflicts at row level and the invariant
spanning both rows breaks anyway. That is write skew, and it is the anomaly the
roadmap asks you to be able to draw from memory.

WHAT TO LOOK FOR: the `broken shifts` column. It should be non-zero at READ
COMMITTED and at REPEATABLE READ (snapshot isolation does not prevent write
skew -- every read in a write-skew execution is consistent with one snapshot),
zero at SERIALIZABLE, and zero at READ COMMITTED once the read takes row locks
with FOR UPDATE. Then look at what SERIALIZABLE cost you: the `40001` and
`retries/req` columns are that cost, stated as a number.

The pair of transactions is *started* together (that is the load shape two users
clicking at the same instant produces). Nothing after that is arranged: no
barrier between the read and the write. If you want the arranged, whiteboard
version, run python/lockstep_psql.py -- it drives two real psql sessions.
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402

SHIFTS = int(os.environ.get("SHIFTS", 100))
PAIRS_IN_FLIGHT = int(os.environ.get("PAIRS_IN_FLIGHT", 10))  # -> 2x this many connections
MAX_RETRIES = 5
# Time the application spends between its read and its write. A real handler
# spends this deserialising, validating, calling something. It is the window the
# anomaly lives in, and pretending it is zero is what makes write skew look rare.
THINK_SECONDS = 0.003


class Variant:
    def __init__(self, label: str, isolation: str, lock_reads: bool):
        self.label = label
        self.isolation = isolation
        self.lock_reads = lock_reads


VARIANTS = [
    Variant("read committed", "read committed", False),
    Variant("repeatable read", "repeatable read", False),
    Variant("serializable", "serializable", False),
    Variant("read committed + FOR UPDATE", "read committed", True),
]


def go_off_call(worker: "lab_db.Worker", shift: int, doctor: int, lock_reads: bool) -> dict:
    """One request. Returns counters; retries the WHOLE transaction on 40001.

    The trap the layer README names: retrying only the failed UPDATE would
    reintroduce the anomaly, because the decision was made from the old read.
    The retry has to re-run the read too, which is why the retry lives out here
    around the entire `with conn.transaction()` block and not inside it.
    """
    counters = {"aborts": 0, "retries": 0, "wrote": 0, "declined": 0, "gave_up": 0}
    conn = worker.conn
    for attempt in range(MAX_RETRIES + 1):
        try:
            with conn.transaction():
                if lock_reads:
                    # The manual fix at READ COMMITTED: lock the rows you READ,
                    # not the row you write. Aggregates cannot take a row lock,
                    # so count in Python over the locked rows.
                    rows = conn.execute(
                        "SELECT on_call FROM oncall WHERE shift_id = %s ORDER BY doctor_id FOR UPDATE",
                        (shift,),
                    ).fetchall()
                    on_call = sum(1 for (flag,) in rows if flag)
                else:
                    on_call = conn.execute(
                        "SELECT count(*) FROM oncall WHERE shift_id = %s AND on_call",
                        (shift,),
                    ).fetchone()[0]

                time.sleep(THINK_SECONDS)

                if on_call > 1:
                    conn.execute(
                        "UPDATE oncall SET on_call = false WHERE shift_id = %s AND doctor_id = %s",
                        (shift, doctor),
                    )
                    counters["wrote"] += 1
                else:
                    counters["declined"] += 1
            return counters
        except Exception as exc:  # noqa: BLE001 - we classify it immediately
            if lab_db.sqlstate(exc) != "40001":
                raise
            counters["aborts"] += 1
            counters["wrote"] = counters["declined"] = 0  # the txn rolled back
            if attempt == MAX_RETRIES:
                counters["gave_up"] += 1
                return counters
            counters["retries"] += 1
            # No backoff here on purpose: see question 4 in the layer README.
    return counters


def run_variant(variant: Variant, setup: "lab_db.Connection") -> dict:
    lab_db.reset_oncall(setup, SHIFTS)
    rounds = SHIFTS // PAIRS_IN_FLIGHT
    n_workers = PAIRS_IN_FLIGHT * 2
    start_together = threading.Barrier(n_workers)
    observed_isolation: list[str] = []
    lock = threading.Lock()

    def body(worker: "lab_db.Worker", index: int) -> dict:
        # Prove the flag actually applied. "Any 40001 at READ COMMITTED" is on
        # the layer README's broken-experiment list precisely because a silently
        # unapplied isolation setting is the usual cause.
        with worker.conn.transaction():
            level = worker.conn.execute("SHOW transaction_isolation").fetchone()[0]
        with lock:
            observed_isolation.append(level)

        totals = {"aborts": 0, "retries": 0, "wrote": 0, "declined": 0, "gave_up": 0}
        latencies = []
        for r in range(rounds):
            shift = r * PAIRS_IN_FLIGHT + (index // 2) + 1
            doctor = (index % 2) + 1
            start_together.wait()
            t0 = time.perf_counter()
            got = go_off_call(worker, shift, doctor, variant.lock_reads)
            latencies.append((time.perf_counter() - t0) * 1000)
            for k, v in got.items():
                totals[k] += v
        totals["latencies"] = latencies
        return totals

    t0 = time.perf_counter()
    results = lab_db.run_workers(n_workers, body, isolation=variant.isolation)
    elapsed = time.perf_counter() - t0

    summed = {k: sum(r[k] for r in results) for k in ("aborts", "retries", "wrote", "declined", "gave_up")}
    latencies = [ms for r in results for ms in r["latencies"]]
    requests = len(latencies)

    broken = setup.execute(
        """
        SELECT count(*) FROM (
            SELECT shift_id FROM oncall GROUP BY shift_id HAVING sum(on_call::int) = 0
        ) t
        """
    ).fetchone()[0]

    return {
        "label": variant.label,
        "isolation_observed": sorted(set(observed_isolation)),
        "broken": broken,
        "aborts": summed["aborts"],
        "gave_up": summed["gave_up"],
        "retries_per_req": summed["retries"] / requests,
        "p50": lab_db.percentile(latencies, 50),
        "p99": lab_db.percentile(latencies, 99),
        "rps": requests / elapsed,
        "requests": requests,
    }


def main() -> None:
    lab_db.ensure_database()
    with lab_db.connect() as setup:
        lab_db.banner(f"Write skew -- {lab_db.describe_server(setup)}")
        print(f"{SHIFTS} shifts x 2 doctors, both doctors of a shift released simultaneously,")
        print(f"{PAIRS_IN_FLIGHT * 2} connections, {THINK_SECONDS * 1000:.0f}ms of application think time between read and write.")
        print("Invariant: every shift keeps at least one doctor on call.\n")

        rows = [run_variant(v, setup) for v in VARIANTS]

        header = f"{'variant':<30}{'broken':>8}{'40001':>8}{'retries/req':>13}{'p50 ms':>9}{'p99 ms':>9}{'req/s':>9}"
        print(header)
        print("-" * len(header))
        for r in rows:
            print(f"{r['label']:<30}{r['broken']:>8}{r['aborts']:>8}"
                  f"{r['retries_per_req']:>13.2f}{r['p50']:>9.1f}{r['p99']:>9.1f}{r['rps']:>9.1f}")
        print()
        for r in rows:
            print(f"  {r['label']:<30} ran at transaction_isolation = {', '.join(r['isolation_observed'])}"
                  + (f"   ({r['gave_up']} gave up after {MAX_RETRIES} retries)" if r["gave_up"] else ""))
        print()
        print("Read it like this:")
        print("  broken > 0 at repeatable read  -> snapshot isolation does not stop write skew.")
        print("  broken = 0 at serializable     -> SSI stopped it; the 40001 column is what that cost.")
        print("  broken = 0 with FOR UPDATE     -> you locked what you READ, so the pair serialised.")
        print("  broken = 0 at read committed with no locking -> the experiment is broken, not Postgres:")
        print("     the pair is not overlapping (check PAIRS_IN_FLIGHT and think time) or something")
        print("     upstream is serialising your requests.")


if __name__ == "__main__":
    main()
