"""
Which "idle in transaction" sessions actually stall vacuum -- and which do not.

    python3 02-mvcc-and-vacuum/python/xmin_horizon.py

WHAT IT DEMONSTRATES: the folklore is "an idle-in-transaction session blocks
vacuum." Run this and watch the folklore be wrong for the first case and right
for the next two. A READ COMMITTED transaction releases its snapshot at the end
of every statement, so a session sitting idle in a read-only READ COMMITTED
transaction holds NO xmin at all. It starts holding the horizon the moment it
either (a) writes something, and so has a transaction id of its own, or (b) runs
at REPEATABLE READ or SERIALIZABLE, where one snapshot covers the whole
transaction.

That distinction is why the incident is always "a handler that made an HTTP call
after its first UPDATE" and never "a session that opened a transaction and did
nothing." Both look identical in `pg_stat_activity`. Only one of them is
strangling your cluster.

WHAT TO LOOK FOR: for each holder, `dead after VACUUM` and the vacuum notice
`N dead row versions cannot be removed yet, oldest xmin: X`. Then PHASE 3: the
fix is a server-side guardrail, not discipline.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402
import mvcc_lab  # noqa: E402

ROWS = int(os.environ.get("ROWS", 200_000))
UPDATES = int(os.environ.get("UPDATES", 200_000))
IDLE_TIMEOUT_S = int(os.environ.get("IDLE_TIMEOUT_S", 5))

MARKER_DDL = "CREATE TABLE IF NOT EXISTS mvcc_marker (id int PRIMARY KEY, n bigint NOT NULL)"


class Holder:
    """A session that opens a transaction and then stops doing anything.

    isolation:  'read committed' | 'repeatable read' | 'serializable'
    write:      whether it writes first, and so acquires a transaction id
    timeout_s:  server-side guardrail applied to this session
    """

    def __init__(self, label: str, isolation: str, write: bool, timeout_s: int | None = None):
        self.label = label
        self.conn = lab_db.connect(autocommit=False)
        self.conn.execute("SELECT set_config('application_name', 'sep-idle-in-transaction', false)")
        self.conn.execute(f"SET default_transaction_isolation = '{isolation}'")
        if timeout_s:
            self.conn.execute(f"SET idle_in_transaction_session_timeout = '{timeout_s}s'")
        self.conn.commit()
        self.pid = self.conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        if write:
            self.conn.execute("INSERT INTO mvcc_marker (id, n) VALUES (1, 1) "
                              "ON CONFLICT (id) DO UPDATE SET n = mvcc_marker.n + 1")
        else:
            self.conn.execute("SELECT 1")  # a read is enough to open the transaction

    def observed(self, probe) -> tuple[str, str, str]:
        """state, backend_xid, backend_xmin -- and you need all three.

        backend_xid  = this transaction has written, so it owns a transaction id.
        backend_xmin = this transaction is holding a snapshot.
        EITHER ONE stalls cleanup. A session can hold the horizon with a NULL
        backend_xmin, which is exactly why "check backend_xmin" is not enough.
        """
        row = probe.execute(
            """
            SELECT state,
                   COALESCE(backend_xid::text, 'NULL'),
                   COALESCE(backend_xmin::text, 'NULL')
            FROM pg_stat_activity WHERE pid = %s
            """,
            (self.pid,),
        ).fetchone()
        return row if row else ("gone", "NULL", "NULL")

    def close(self) -> None:
        try:
            self.conn.rollback()
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


def not_yet_removable(notices: list[str]) -> str:
    for line in mvcc_lab.interesting_notice_lines(notices):
        if "not yet removable" in line or "cannot be removed yet" in line:
            return line
    return "(no line about unremovable tuples)"


def churn_and_vacuum(conn, label: str) -> tuple[int, str]:
    mvcc_lab.churn(conn, UPDATES, ROWS)
    # pg_stat_user_tables is updated asynchronously; without this pause the
    # "before" sample can still be reporting the previous phase.
    time.sleep(1.0)
    before = mvcc_lab.stats(conn)["dead"]
    notices = mvcc_lab.vacuum_verbose(conn)
    time.sleep(1.0)
    after = mvcc_lab.stats(conn)["dead"]
    print(f"    dead before VACUUM {before:>9,}   dead after VACUUM {after:>9,}")
    print(f"    vacuum says: {not_yet_removable(notices)}")
    return after, label


def main() -> None:
    lab_db.ensure_database()
    with lab_db.connect() as conn:
        lab_db.banner(f"The xmin horizon -- {lab_db.describe_server(conn)}")
        print(f"table {mvcc_lab.TABLE}: {ROWS:,} rows, {UPDATES:,} updates per phase.")
        print("status is indexed, so none of these updates can be HOT updates -- if it were not,")
        print("Postgres would clean up on the same page and none of this would be visible.\n")

        conn.execute(MARKER_DDL)
        mvcc_lab.ensure_table(conn, ROWS)
        conn.execute(f"VACUUM (ANALYZE) {mvcc_lab.TABLE}")

        print("PHASE 1 -- nothing holding the horizon")
        baseline, _ = churn_and_vacuum(conn, "clean")

        holders = [
            ("idle in txn, READ COMMITTED, read-only", "read committed", False),
            ("idle in txn, READ COMMITTED, wrote first", "read committed", True),
            ("idle in txn, REPEATABLE READ, read-only", "repeatable read", False),
        ]
        print("\nPHASE 2 -- the same churn and vacuum, with one session sitting idle in a transaction")
        results = []
        for label, isolation, write in holders:
            holder = Holder(label, isolation, write)
            state, xid, xmin = holder.observed(conn)
            print(f"\n  {label}")
            print(f"    pid {holder.pid}: state={state!r}  backend_xid={xid}  backend_xmin={xmin}")
            dead, _ = churn_and_vacuum(conn, label)
            results.append((label, xid, xmin, dead))
            holder.close()

        print("\n  summary")
        print(f"    {'holder':<44}{'backend_xid':>13}{'backend_xmin':>14}{'dead after VACUUM':>20}")
        print(f"    {'(none)':<44}{'-':>13}{'-':>14}{baseline:>20,}")
        for label, xid, xmin, dead in results:
            print(f"    {label:<44}{xid:>13}{xmin:>14}{dead:>20,}")
        print()
        print("  Read the two id columns together. The read-only READ COMMITTED session holds")
        print("  NEITHER -- it released its snapshot at the end of its last statement, and it")
        print("  never wrote, so it has no transaction id. It is harmless, and it is the case")
        print("  the folklore is about. The other two each hold ONE of them: an xid because")
        print("  they wrote, or an xmin because REPEATABLE READ keeps one snapshot for the")
        print("  whole transaction. Either is enough to stop vacuum removing anything newer.")

        print(f"\nPHASE 3 -- the fix is a guardrail, not discipline")
        print("  In production:")
        print("      ALTER ROLE app_web SET idle_in_transaction_session_timeout = '30s';")
        print("      ALTER ROLE app_web SET statement_timeout = '30s';")
        print(f"  Here, applied to the offending session ({IDLE_TIMEOUT_S}s so you are not waiting on it).")
        guarded = Holder("guarded", "repeatable read", True, timeout_s=IDLE_TIMEOUT_S)
        state, xid, xmin = guarded.observed(conn)
        print(f"  pid {guarded.pid}: state={state!r}  backend_xid={xid}  backend_xmin={xmin}")
        deadline = time.time() + IDLE_TIMEOUT_S + 15
        while guarded.observed(conn)[0] != "gone" and time.time() < deadline:
            time.sleep(0.5)
        gone = guarded.observed(conn)[0] == "gone"
        print(f"  after {IDLE_TIMEOUT_S}s: " + ("Postgres terminated the session" if gone
                                                else "STILL THERE -- check the setting actually applied"))
        guarded.close()
        dead, _ = churn_and_vacuum(conn, "after guardrail")

        print()
        print("Three distinct things hold the horizon back, and each one stalls cleanup for")
        print("EVERY table in the cluster, not only the table being written to:")
        print("  1. a long-running query, or an idle transaction owning an xid OR a snapshot")
        print("  2. a replication slot whose consumer is behind or gone -- see")
        print("     replication_slot_starvation.py; this is the one that catches people, because")
        print("     nothing in pg_stat_activity looks wrong")
        print("  3. hot_standby_feedback = on with a standby running long queries")
        print("  (+ prepared two-phase transactions, where max_prepared_transactions > 0)")


if __name__ == "__main__":
    main()
