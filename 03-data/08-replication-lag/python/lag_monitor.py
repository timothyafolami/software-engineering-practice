"""
Monitoring lag properly: bytes, and why the seconds number lies.

    python3 08-replication-lag/python/lag_monitor.py

WHAT IT DEMONSTRATES: three phases, sampled once a second into a CSV.

  1. idle primary       no writes at all. Byte lag is zero -- correctly, the
                        standby has everything. The SECONDS-behind figure,
                        derived from pg_last_xact_replay_timestamp(), grows
                        without bound anyway, because the newest transaction
                        timestamp the standby has replayed keeps getting older.
  2. steady writes      byte lag becomes a real, small number.
  3. write burst        byte lag climbs, then drains. This is the shape your
                        alert has to distinguish from phase 1.

WHY THIS IS THE WHOLE POINT: nearly every replication alert anybody writes is
"seconds behind > N", and phase 1 is why that alert pages on a quiet Sunday
while the system is perfectly healthy. Alert on BYTES from
pg_stat_replication on the PRIMARY:

    pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)

and use the seconds figure only as a secondary signal, on a primary you already
know is taking writes.

WHAT TO LOOK FOR: in the printed table, the `seconds behind` column during
phase 1 against the `replay bytes` column beside it. One is climbing and one is
zero, and they are describing the same healthy standby.

Output: a CSV at $LAB_OUT/replication_lag.csv (default: the system temp dir).
Knobs: PHASE_S, WRITE_RATE, BURST_ROWS, LAB_OUT.
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db      # noqa: E402
import repl_lab    # noqa: E402

PHASE_S = float(os.environ.get("PHASE_S", "12"))
WRITE_RATE = float(os.environ.get("WRITE_RATE", "20"))
BURST_ROWS = int(os.environ.get("BURST_ROWS", "40000"))
OUT_DIR = os.environ.get("LAB_OUT", tempfile.gettempdir())
CSV_PATH = os.path.join(OUT_DIR, "replication_lag.csv")


def writer_thread(stop: threading.Event, phase: dict) -> None:
    """Generates the write volume each phase asks for."""
    conn = lab_db.connect()
    repl_lab.ensure_probe_table(conn)
    try:
        while not stop.is_set():
            mode = phase["mode"]
            if mode == "idle":
                time.sleep(0.1)
            elif mode == "steady":
                conn.execute("INSERT INTO repl_probe (payload) VALUES ('steady')")
                time.sleep(1.0 / WRITE_RATE)
            elif mode == "burst":
                conn.execute(
                    "INSERT INTO repl_probe (payload) "
                    "SELECT 'burst ' || g FROM generate_series(1, %s) g", (BURST_ROWS,))
            else:
                time.sleep(0.1)
    finally:
        conn.close()


def main() -> None:
    lab_db.ensure_database()
    replica = repl_lab.replica_or_exit("Replication lag monitor")
    primary = lab_db.connect()
    repl_lab.ensure_probe_table(primary, replica)

    lab_db.banner("Monitoring replication lag: bytes, not seconds")
    delay = replica.execute("SHOW recovery_min_apply_delay").fetchone()[0]
    print(f"  replica recovery_min_apply_delay = {delay}")
    print(f"  three phases of {PHASE_S:.0f}s: idle, steady writes, one big burst")
    print(f"  CSV: {CSV_PATH}")
    print("\n  Watch the two lag columns disagree in phase 1. Both are correct")
    print("  measurements; only one of them is measuring the thing you care about.")

    phase = {"mode": "idle"}
    stop = threading.Event()
    t = threading.Thread(target=writer_thread, args=(stop, phase), daemon=True)
    t.start()

    rows = []
    print(f"\n  {'phase':<10}{'t':>5}{'replay bytes':>15}{'flush bytes':>14}"
          f"{'seconds behind':>17}{'replay_lag_s':>14}")
    print("  " + "-" * 76)
    try:
        for mode in ("idle", "steady", "burst"):
            phase["mode"] = mode
            phase_start = time.time()
            while time.time() - phase_start < PHASE_S:
                lag = repl_lab.lag(primary)
                pos = repl_lab.replica_positions(replica)
                row = {
                    "phase": mode,
                    "t": round(time.time() - phase_start, 1),
                    "replay_bytes": lag.get("replay_bytes"),
                    "flush_bytes": lag.get("flush_bytes"),
                    "seconds_behind": (round(pos["seconds_behind"], 1)
                                       if pos["seconds_behind"] is not None else None),
                    "replay_lag_s": (round(lag["replay_lag_s"], 2)
                                     if lag.get("replay_lag_s") is not None else None),
                    "receive_lsn": str(pos["receive_lsn"]),
                    "replay_lsn": str(pos["replay_lsn"]),
                }
                rows.append(row)
                print(f"  {mode:<10}{row['t']:>5.1f}"
                      f"{(row['replay_bytes'] if row['replay_bytes'] is not None else -1):>15,}"
                      f"{(row['flush_bytes'] if row['flush_bytes'] is not None else -1):>14,}"
                      f"{(row['seconds_behind'] if row['seconds_behind'] is not None else -1):>17.1f}"
                      f"{(row['replay_lag_s'] if row['replay_lag_s'] is not None else -1):>14.2f}")
                time.sleep(1.0)
    finally:
        stop.set()
        t.join(5)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(CSV_PATH, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    idle = [r for r in rows if r["phase"] == "idle"]
    burst = [r for r in rows if r["phase"] == "burst"]
    print(f"\n  wrote {len(rows)} samples to {CSV_PATH}")
    if idle:
        first, last = idle[0], idle[-1]
        grew = ((last["seconds_behind"] or 0) - (first["seconds_behind"] or 0))
        print(f"\n  phase 1, idle primary:")
        print(f"    replay bytes   {first['replay_bytes']} -> {last['replay_bytes']}")
        print(f"    seconds behind {first['seconds_behind']} -> {last['seconds_behind']}"
              f"   (grew by {grew:.1f}s over {len(idle)} samples)")
        if delay not in ("0", "0ms"):
            print(f"    READ THAT AS BLOCKED, not as the result: recovery_min_apply_delay is")
            print(f"    {delay}, so `seconds behind` is pinned near {delay} by the delay itself and")
            print("    cannot show the unbounded growth this phase exists to show. The delay")
            print("    is holding replay back deliberately; an idle primary is supposed to be")
            print("    the one case where nothing is holding it back at all.")
            print("    unblock -- the honest version of phase 1 needs a standby with no delay:")
            print("      bash 08-replication-lag/scripts/start_replica.sh --stop")
            print("      APPLY_DELAY=0 bash 08-replication-lag/scripts/start_replica.sh")
            print("      python3 08-replication-lag/python/lag_monitor.py")
        elif grew >= 0.5 * PHASE_S:
            print("    Nothing changed about the standby's health between those two samples.")
            print("    The seconds figure grew by roughly one second per second, because no new")
            print("    transaction arrived to carry a newer timestamp, while replay bytes stayed")
            print("    at zero. An alert on that column pages you for an idle system.")
        else:
            print("    The seconds figure did NOT grow here, so this run does not demonstrate")
            print("    the effect: something was still writing to the primary. Check that no")
            print("    other program is pointed at this database and run it again.")
    if burst:
        peak = max(r["replay_bytes"] or 0 for r in burst)
        print(f"\n  phase 3, burst: peak replay lag {peak:,} bytes")
        if peak == 0:
            print("    Byte lag never moved: the burst was too small to produce WAL faster")
            print("    than it ships on this machine. That is a healthy system, not a")
            print("    measurement -- raise BURST_ROWS until it moves.")
    print("\n  The alert to actually write, on the PRIMARY:")
    print("    SELECT application_name,")
    print("           pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_bytes")
    print("    FROM pg_stat_replication;")
    print("  Threshold it in bytes. Add a separate alert for a standby that has")
    print("  DISAPPEARED from pg_stat_replication entirely, because zero rows there is")
    print("  not zero lag -- it is no replica, and byte lag cannot warn you about a row")
    print("  that does not exist.")
    replica.close()
    primary.execute("TRUNCATE repl_probe")
    primary.close()


if __name__ == "__main__":
    main()
