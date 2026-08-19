"""
The xmin holder that nothing in pg_stat_activity will ever show you.

    python3 02-mvcc-and-vacuum/python/replication_slot_starvation.py

WHAT IT DEMONSTRATES: a replication slot whose consumer died holds the xmin
horizon exactly like an idle transaction does -- and there is no session, no
query, no `idle in transaction` state, nothing in `pg_stat_activity` that looks
wrong. This is the cause people spend a day not finding. A logical slot pins
`catalog_xmin` and `xmin`; a physical slot pins WAL, and pins xmin too once a
standby with `hot_standby_feedback = on` connects to it.

The program creates a slot with no consumer, churns the table, vacuums, and
shows what could not be removed -- then drops the slot and shows the same vacuum
reclaim everything.

WHAT TO LOOK FOR: the `pg_replication_slots` row -- `active = false`, and an
`xmin` that stops moving -- alongside a completely healthy-looking
`pg_stat_activity`. And, at the end, the WAL retention: an abandoned slot is also
how you run out of disk on the primary.

REQUIRES wal_level = logical for the headline case. This machine's default is
`replica`, so the program checks first and tells you the exact unblock command
rather than pretending.
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
SLOT = "sep_dead_slot"


def slots(conn) -> list[tuple]:
    return conn.execute(
        """
        SELECT slot_name, slot_type, active,
               COALESCE(xmin::text, '-') AS xmin,
               COALESCE(catalog_xmin::text, '-') AS catalog_xmin,
               COALESCE(wal_status, '-') AS wal_status,
               pg_size_pretty(COALESCE(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn), 0)) AS wal_retained
        FROM pg_replication_slots ORDER BY slot_name
        """
    ).fetchall()


def print_slots(conn, label: str) -> None:
    rows = slots(conn)
    print(f"\n  pg_replication_slots {label}:")
    if not rows:
        print("    (none)")
        return
    print(f"    {'slot':<16}{'type':<10}{'active':<8}{'xmin':>10}{'catalog_xmin':>14}"
          f"{'wal_status':>12}{'wal retained':>14}")
    for r in rows:
        print(f"    {r[0]:<16}{r[1]:<10}{str(r[2]):<8}{r[3]:>10}{r[4]:>14}{r[5]:>12}{r[6]:>14}")


def suspicious_sessions(conn) -> int:
    return conn.execute(
        """
        SELECT count(*) FROM pg_stat_activity
        WHERE (backend_xmin IS NOT NULL OR backend_xid IS NOT NULL)
          AND pid <> pg_backend_pid()
        """
    ).fetchone()[0]


def churn_vacuum_report(conn, label: str) -> int:
    mvcc_lab.churn(conn, UPDATES, ROWS)
    time.sleep(1.0)
    notices = mvcc_lab.vacuum_verbose(conn)
    time.sleep(1.0)
    dead = mvcc_lab.stats(conn)["dead"]
    line = next((l for l in mvcc_lab.interesting_notice_lines(notices)
                 if "not yet removable" in l or "cannot be removed yet" in l), "")
    print(f"  {label:<34} dead after VACUUM = {dead:>9,}")
    if line:
        print(f"  {'':<34} vacuum says: {line}")
    return dead


def main() -> None:
    lab_db.ensure_database()
    with lab_db.connect() as conn:
        lab_db.banner(f"The dead replication slot -- {lab_db.describe_server(conn)}")
        wal_level = conn.execute("SHOW wal_level").fetchone()[0]
        logical_ok = lab_db.gate(
            f"logical replication slots (wal_level is '{wal_level}')",
            wal_level == "logical",
            "ALTER SYSTEM SET wal_level = 'logical';  then restart Postgres "
            "(pg_ctl -D $(psql -Atc 'show data_directory') restart)",
        )
        print()
        mvcc_lab.ensure_table(conn, ROWS)
        conn.execute(f"VACUUM (ANALYZE) {mvcc_lab.TABLE}")

        conn.execute(
            "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots "
            "WHERE slot_name = %s", (SLOT,)
        )
        print("PHASE 1 -- no slot")
        churn_vacuum_report(conn, "no slot, nothing else running")

        if logical_ok:
            print("\nPHASE 2 -- a LOGICAL slot with no consumer")
            conn.execute("SELECT pg_create_logical_replication_slot(%s, 'pgoutput')", (SLOT,))
            print_slots(conn, "with the abandoned slot")
            print(f"  sessions holding an xid or a snapshot: {suspicious_sessions(conn)}"
                  "   <- nothing to find in pg_stat_activity")
            print()
            churn_vacuum_report(conn, "logical slot, no consumer")
            conn.execute("SELECT pg_drop_replication_slot(%s)", (SLOT,))
            print()
            churn_vacuum_report(conn, "after dropping the slot")
        else:
            print("\nPHASE 2 -- SKIPPED: needs wal_level = logical (see the unblock line above).")
            print("  What you would see: pg_replication_slots gets a row with active = false and")
            print("  a frozen catalog_xmin, pg_stat_activity stays completely clean, and vacuum")
            print("  reports the same `N dead row versions cannot be removed yet` as the idle")
            print("  transaction in xmin_horizon.py does.")

        print("\nPHASE 3 -- a PHYSICAL slot with no consumer (this one works at wal_level=replica)")
        conn.execute("SELECT pg_create_physical_replication_slot(%s, true)", (SLOT,))
        print_slots(conn, "with a physical slot")
        dead = churn_vacuum_report(conn, "physical slot, no consumer")
        print()
        print("  Note the difference, because it is the useful part: an abandoned PHYSICAL slot")
        print("  has no xmin, so it does not stall vacuum -- it pins WAL, and fills your disk")
        print("  instead. It starts holding xmin as well only when a standby connects to it with")
        print("  hot_standby_feedback = on. Two different failure modes, one `pg_replication_slots`")
        print("  row, and the column that tells them apart is `xmin`.")
        conn.execute("SELECT pg_drop_replication_slot(%s)", (SLOT,))
        print_slots(conn, "after cleanup")
        print()
        print("Production check, in this order, whenever cleanup has stalled:")
        print("  SELECT slot_name, active, xmin, catalog_xmin, wal_status,")
        print("         pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn))")
        print("  FROM pg_replication_slots;")
        print("  SELECT pid, state, backend_xid, backend_xmin, xact_start FROM pg_stat_activity")
        print("  WHERE backend_xmin IS NOT NULL OR backend_xid IS NOT NULL ORDER BY xact_start;")


if __name__ == "__main__":
    main()
