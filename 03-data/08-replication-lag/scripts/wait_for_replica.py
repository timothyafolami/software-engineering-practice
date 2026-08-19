"""
Is there a replica, is it replaying, and how far behind is it right now?

    python3 08-replication-lag/scripts/wait_for_replica.py

Run this after starting the standby and before any experiment in this topic.
It is the difference between "the experiment showed no stale reads" and "the
experiment was not reading from a replica", which look identical in a table.

WHAT IT CHECKS, in the order the failures actually happen:
  1. LAB_REPLICA_DSN is set and connects
  2. pg_is_in_recovery() is TRUE on it -- it is a standby, not a second primary
  3. pg_stat_wal_receiver has a streaming connection -- it is actually receiving,
     rather than sitting on a copy of the data it was cloned with
  4. the primary can see it in pg_stat_replication
  5. it catches up to the primary's current LSN within the timeout

WHAT TO LOOK FOR: the byte-lag figure, and the gap between `receive` and
`replay`. With recovery_min_apply_delay set, the standby will have RECEIVED
everything and deliberately not replayed it -- receive lag near zero and replay
lag not. That gap IS the experiment's subject.
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "python"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "lab", "local"))
import lab_db      # noqa: E402
import repl_lab    # noqa: E402

TIMEOUT_S = float(os.environ.get("TIMEOUT_S", "30"))


def main() -> None:
    lab_db.ensure_database()
    replica = repl_lab.replica_or_exit("Replica check")
    primary = lab_db.connect()

    lab_db.banner("Replica check")
    print(f"  primary  {lab_db.describe_server(primary)}")
    print(f"  replica  {lab_db.describe_server(replica)}   "
          f"(pg_is_in_recovery = true)")

    delay = replica.execute("SHOW recovery_min_apply_delay").fetchone()[0]
    print(f"  recovery_min_apply_delay on the standby: {delay}")

    receiver = replica.execute(
        "SELECT status, conninfo IS NOT NULL FROM pg_stat_wal_receiver"
    ).fetchone()
    if not receiver:
        print("\n  BLOCKED: pg_stat_wal_receiver is empty -- this standby is NOT streaming.")
        print("  It is serving a frozen copy of whatever it was cloned with, which will")
        print("  look like 100% stale reads and is not a lag measurement.")
        print("  Check the standby log, then re-run start_replica.sh.")
        sys.exit(1)
    print(f"  wal receiver status: {receiver[0]}")

    print(f"\n  waiting up to {TIMEOUT_S:.0f}s for the standby to replay the primary's "
          f"current LSN...")
    target = repl_lab.current_lsn(primary)
    deadline = time.time() + TIMEOUT_S
    caught_up = False
    while time.time() < deadline:
        if repl_lab.replayed_through(replica, target):
            caught_up = True
            break
        time.sleep(0.2)

    lag = repl_lab.lag(primary)
    pos = repl_lab.replica_positions(replica)
    print(f"  target LSN {target}: "
          f"{'replayed' if caught_up else 'NOT replayed within the timeout'}")
    if lag.get("connected"):
        print(f"\n  as the PRIMARY sees it (pg_stat_replication):")
        print(f"    state {lag['state']} / {lag['sync_state']}")
        print(f"    behind by  sent {lag['sent_bytes']:,} B   write {lag['write_bytes']:,} B"
              f"   flush {lag['flush_bytes']:,} B   replay {lag['replay_bytes']:,} B")
    else:
        print("\n  the primary does not see this standby in pg_stat_replication -- it is")
        print("  receiving WAL from somewhere else, or the connection just dropped.")
    print(f"\n  as the REPLICA sees it:")
    print(f"    receive_lsn {pos['receive_lsn']}   replay_lsn {pos['replay_lsn']}")
    if pos["seconds_behind"] is not None:
        print(f"    pg_last_xact_replay_timestamp is {pos['seconds_behind']:.1f}s old")
        print("    -- and that number keeps growing on an idle primary even when the")
        print("       standby is perfectly caught up. Alert on bytes, not on seconds.")

    if caught_up and delay not in ("0", "0ms"):
        print(f"\n  NOTE: recovery_min_apply_delay is {delay} and the standby still caught up")
        print("  to a target taken before the wait -- that is expected, the delay applies to")
        print("  each record's own commit time. Writes made from now on will be held back")
        print("  by that much, which is what the experiments rely on.")

    print("\n  ready. Now:")
    print("    python3 08-replication-lag/python/stale_reads.py")
    print("    python3 08-replication-lag/python/lsn_token.py")
    print("    python3 08-replication-lag/python/lag_monitor.py")
    replica.close()
    primary.close()


if __name__ == "__main__":
    main()
