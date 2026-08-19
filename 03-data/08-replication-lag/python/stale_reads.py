"""
Read-your-own-writes, broken on purpose and then measured.

    python3 08-replication-lag/python/stale_reads.py

WHAT IT DEMONSTRATES: the simplest possible request pair -- write a row to the
primary, then immediately read it back from the replica -- run a few hundred
times, with the stale-read rate counted rather than described.

Then the same thing with the deterministic delay removed and write volume raised
until lag appears on its own. Compare the two rates. The first is a bug you can
reproduce whenever you like; the second is the same bug in staging, where it
happens rarely enough to be closed as "could not reproduce" and often enough to
be a support ticket every week.

WHAT TO LOOK FOR:
  * the stale-read rate at recovery_min_apply_delay = 2s. It should be close to
    100%, because the follow-up read happens milliseconds after the write.
  * how the rate falls as you insert a delay between write and read, and where
    it reaches zero. That crossover is your lag, measured from the outside by
    the only observer who matters: a user.
  * the byte-lag figure printed alongside. Bytes, not seconds -- see the
    docstring in repl_lab.py for why the seconds number lies on an idle primary.

SAMPLE SIZE IS NOT CONSTANT ACROSS ROWS, on purpose. Every request in a row
sleeps for that row's think time, so a fixed 200 pairs at think = 2500ms is
eight minutes of sleeping for one line of output. Each row instead gets as many
pairs as fit in ROW_BUDGET_MS, floored at MIN_REQUESTS, and prints the count it
actually used -- a rate over 6 samples and a rate over 200 are different
evidence and the table says which one you are reading.

Knobs: REQUESTS (the cap), MIN_REQUESTS, ROW_BUDGET_MS, THINK_MS (delay between
the write and the read), WRITE_BURST (rows per background write, for the
natural-lag phase).
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db      # noqa: E402
import repl_lab    # noqa: E402

REQUESTS = int(os.environ.get("REQUESTS", "200"))
MIN_REQUESTS = int(os.environ.get("MIN_REQUESTS", "6"))
ROW_BUDGET_MS = float(os.environ.get("ROW_BUDGET_MS", "8000"))
THINK_MS = [int(x) for x in os.environ.get("THINK_MS", "0,50,250,1000,2500").split(",")]
WRITE_BURST = int(os.environ.get("WRITE_BURST", "2000"))


def pairs_for(think_ms: int) -> int:
    """How many write+read pairs this row can afford.

    The sleep dominates everything else, so the budget is simply how many think
    times fit in ROW_BUDGET_MS -- capped at REQUESTS and floored at MIN_REQUESTS
    so the slowest row still produces a rate rather than a single coin flip.
    """
    if think_ms <= 0:
        return REQUESTS
    return max(MIN_REQUESTS, min(REQUESTS, int(ROW_BUDGET_MS / think_ms)))


def one_round(primary, replica, think_ms: int, n: int | None = None) -> dict:
    """POST then GET. The most ordinary pair of operations there is."""
    if n is None:
        n = pairs_for(think_ms)
    stale = 0
    latencies = []
    for i in range(n):
        t0 = time.perf_counter()
        row_id = primary.execute(
            "INSERT INTO repl_probe (payload) VALUES (%s) RETURNING id",
            (f"round-{think_ms}-{i}",),
        ).fetchone()[0]
        if think_ms:
            time.sleep(think_ms / 1000.0)
        found = replica.execute(
            "SELECT 1 FROM repl_probe WHERE id = %s", (row_id,)
        ).fetchone()
        latencies.append((time.perf_counter() - t0) * 1000)
        if found is None:
            stale += 1
    return {
        "think_ms": think_ms,
        "n": n,
        "stale": stale,
        "rate": 100.0 * stale / n,
        "p50": lab_db.percentile(latencies, 50),
        "p99": lab_db.percentile(latencies, 99),
    }


def show(rows: list[dict], header: str) -> None:
    print(f"\n  {header}")
    print(f"    {'think time':>12}{'stale reads':>14}{'rate':>9}{'p50 ms':>10}{'p99 ms':>10}")
    print("    " + "-" * 55)
    for r in rows:
        print(f"    {r['think_ms']:>10} ms{r['stale']:>9} / {r['n']:<4}"
              f"{r['rate']:>8.1f}%{r['p50']:>10.1f}{r['p99']:>10.1f}")


def main() -> None:
    lab_db.ensure_database()
    replica = repl_lab.replica_or_exit("Stale reads")
    primary = lab_db.connect()
    repl_lab.ensure_probe_table(primary, replica)

    lab_db.banner("Read-your-own-writes, broken and measured")
    delay = replica.execute("SHOW recovery_min_apply_delay").fetchone()[0]
    lag = repl_lab.lag(primary)
    print(f"  replica recovery_min_apply_delay = {delay}")
    if lag.get("connected"):
        print(f"  current replay lag: {lag['replay_bytes']:,} bytes")
    budget = ", ".join(f"{t}ms x{pairs_for(t)}" for t in THINK_MS)
    print(f"  pairs per row (sized so no row costs more than {ROW_BUDGET_MS/1000:.0f}s of sleeping): {budget}")
    print("\n  Predict the first row before you read it: you write to the primary and")
    print(f"  read from a replica whose recovery_min_apply_delay is {delay}, with no delay")
    print("  in between. What fraction of reads can possibly find the row?")

    rows = [one_round(primary, replica, t) for t in THINK_MS]
    deterministic = delay not in ("0", "0ms")
    show(rows, f"phase 1 -- {'deterministic' if deterministic else 'NATURAL (no apply delay set)'}"
               f" lag (recovery_min_apply_delay = {delay})")

    zero = next((r for r in rows if r["stale"] == 0), None)
    if zero:
        print(f"    The rate reaches zero once the client waits {zero['think_ms']}ms, which is")
        print("    this replica's lag measured from outside, by the only observer that")
        print("    matters. Nobody's application waits that long on purpose.")

    # -----------------------------------------------------------------------
    # Phase 2: no artificial delay. Lag has to be earned, with write volume.
    # -----------------------------------------------------------------------
    print(f"\n  phase 2 -- natural lag. Generating write volume ({WRITE_BURST:,} rows per")
    print("  round) and measuring the same pairs. This is what staging looks like.")
    if delay not in ("0", "0ms"):
        print(f"\n    NOTE: this standby still has recovery_min_apply_delay = {delay}, so")
        print("    phase 2 is not measuring natural lag yet. Restart the replica with")
        print("    APPLY_DELAY=0 to get the honest version:")
        print("      bash 08-replication-lag/scripts/start_replica.sh --stop")
        print("      APPLY_DELAY=0 bash 08-replication-lag/scripts/start_replica.sh")
        print("    Run it both ways. The comparison is the lesson, not either number.")

    natural = []
    for think in (0, 50):
        primary.execute(
            "INSERT INTO repl_probe (payload) "
            "SELECT 'bulk ' || g FROM generate_series(1, %s) g", (WRITE_BURST,))
        natural.append(one_round(primary, replica, think))
    show(natural, "phase 2 -- under write load")

    lag = repl_lab.lag(primary)
    pos = repl_lab.replica_positions(replica)
    if lag.get("connected"):
        print(f"\n    byte lag now: sent {lag['sent_bytes']:,}  flush {lag['flush_bytes']:,}"
              f"  replay {lag['replay_bytes']:,}")
        print(f"    the standby has RECEIVED up to {pos['receive_lsn']} and REPLAYED up to")
        print(f"    {pos['replay_lsn']} -- the gap between those two is the entire bug.")

    print("\n  What to take from the two phases: the failure rate is a property of the")
    print("  lag and the client's timing, not of the code. The code is identical in both.")
    print("  You cannot test your way to confidence here -- at 0.3% you will not")
    print("  reproduce it, and your users will. The fixes are in lsn_token.py.")
    primary.execute("TRUNCATE repl_probe")
    replica.close()
    primary.close()


if __name__ == "__main__":
    main()
