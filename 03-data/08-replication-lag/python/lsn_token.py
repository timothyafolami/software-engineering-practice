"""
Three fixes for read-your-own-writes, and exactly what each one costs.

    python3 08-replication-lag/python/lsn_token.py

WHAT IT DEMONSTRATES: the same write-then-read pair as stale_reads.py, routed
four ways, with the two numbers that decide between them measured side by side:

    stale reads          -- correctness
    % of reads that
    still reached the    -- how much of the replica's value the fix cost you
    replica

Those two are in tension by construction. A fix that eliminates stale reads by
sending everything to the primary has not solved the problem; it has undone the
reason you added a replica.

  1. none            read the replica always. Fast, cheap, wrong.
  2. sticky N ms     after a write, route this SESSION's reads to the primary
                     for N milliseconds. Cheap and coarse, and wrong at BOTH
                     edges: too short and it is still stale, too long and you
                     have thrown away the replica for every user who writes.
  3. LSN token       after the write, SELECT pg_current_wal_lsn(); carry that
                     token; on read, compare it against the replica's
                     pg_last_wal_replay_lsn() and fall back to the primary only
                     if the replica has not caught up. Correct AND precise.
                     The cost is plumbing a token through every layer of the
                     application, including the layers that have no idea they
                     are in a distributed system.
  4. WAIT FOR LSN    Postgres 19: the replica BLOCKS until it has replayed to
                     your LSN. You pay the lag as LATENCY instead of as
                     STALENESS. Needs a timeout, because "block until caught up"
                     against a badly lagging replica is an outage with good
                     intentions. Gated on server version, and skipped honestly
                     rather than simulated if the server is older.

THE BUG PEOPLE SHIP, and the reason this file names it twice: comparing the
token against pg_last_wal_receive_lsn() -- received -- instead of
pg_last_wal_replay_lsn() -- applied. The check passes, the read is still stale,
and the code reviews perfectly.

TWO TABLES, AND THE SECOND ONE IS THE ARGUMENT. With the read fired
immediately after the write, every correct fix reads the primary and `sticky`
and `lsn` are indistinguishable -- both score 0% stale and 0% on the replica,
and the table cannot tell you why you would ever plumb a token through your
application. The second table pauses PAUSE_MS between the write and the read,
which is the ordinary case: a user does something else before refreshing. There
the two separate, because the sticky window is a guess about time and the token
is a measurement of position.

Knobs: REQUESTS, WAIT_REQUESTS, STICKY_MS, PAUSE_MS, WAIT_TIMEOUT_MS.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db      # noqa: E402
import repl_lab    # noqa: E402
import psycopg     # noqa: E402

REQUESTS = int(os.environ.get("REQUESTS", "200"))
# `wait` is the one strategy that BLOCKS on purpose: every request pays the
# standby's whole apply delay. At REQUESTS=200 against a 2s delay that single
# row takes 400 seconds and turns an 8-second program into a 7-minute one, which
# is how a program stops being run. Sized separately, and the table prints the
# sample size per row so a rate over 25 samples is never mistaken for a rate
# over 200.
WAIT_REQUESTS = int(os.environ.get("WAIT_REQUESTS", "25"))
STICKY_MS = float(os.environ.get("STICKY_MS", "5000"))
PAUSE_MS = float(os.environ.get("PAUSE_MS", "2500"))
WAIT_TIMEOUT_MS = int(os.environ.get("WAIT_TIMEOUT_MS", "3000"))


class Router:
    """Where a read goes, and why. This is the object that has to live BELOW
    your ORM session -- reaching for `replica_engine` at individual call sites
    is how a service ends up with three different consistency behaviours nobody
    documented."""

    def __init__(self, primary, replica, strategy: str):
        self.primary = primary
        self.replica = replica
        self.strategy = strategy
        self.last_write_at = 0.0
        self.token: str | None = None
        self.reads_on_replica = 0
        self.reads_on_primary = 0

    def write(self, payload: str) -> int:
        row_id = self.primary.execute(
            "INSERT INTO repl_probe (payload) VALUES (%s) RETURNING id", (payload,)
        ).fetchone()[0]
        self.last_write_at = time.perf_counter()
        if self.strategy in ("lsn", "wait"):
            # BOTH token strategies need this, not just "lsn": `wait` hands the
            # same token to WAIT FOR LSN. Capturing it only for "lsn" left
            # self.token = None on the PG19 path, and the first `wait` read died
            # with `invalid input syntax for type pg_lsn: "None"` -- a line that
            # could not run at all until a PG19 server existed to run it.
            #
            # Taken AFTER the commit, which autocommit has just done. Capturing
            # it before the write commits gives you a token for a position the
            # replica may already have passed, and the check passes wrongly.
            self.token = repl_lab.current_lsn(self.primary)
        return row_id

    def read(self, row_id: int) -> bool:
        if self.strategy == "none":
            return self._read_replica(row_id)
        if self.strategy == "sticky":
            if (time.perf_counter() - self.last_write_at) * 1000 < STICKY_MS:
                return self._read_primary(row_id)
            return self._read_replica(row_id)
        if self.strategy == "lsn":
            if self.token and not repl_lab.replayed_through(self.replica, self.token):
                return self._read_primary(row_id)
            return self._read_replica(row_id)
        if self.strategy == "wait":
            self.replica.execute(f"SET statement_timeout = {WAIT_TIMEOUT_MS}")
            self.replica.execute(f"WAIT FOR LSN '{self.token}'")
            return self._read_replica(row_id)
        raise ValueError(self.strategy)

    def _read_replica(self, row_id: int) -> bool:
        self.reads_on_replica += 1
        return self.replica.execute(
            "SELECT 1 FROM repl_probe WHERE id = %s", (row_id,)).fetchone() is not None

    def _read_primary(self, row_id: int) -> bool:
        self.reads_on_primary += 1
        return self.primary.execute(
            "SELECT 1 FROM repl_probe WHERE id = %s", (row_id,)).fetchone() is not None


def requests_for(strategy: str) -> int:
    """How many pairs this strategy is worth. See WAIT_REQUESTS."""
    return WAIT_REQUESTS if strategy == "wait" else REQUESTS


def run(primary, replica, strategy: str) -> dict:
    router = Router(primary, replica, strategy)
    n = requests_for(strategy)
    stale = 0
    latencies = []
    for i in range(n):
        t0 = time.perf_counter()
        row_id = router.write(f"{strategy}-{i}")
        if not router.read(row_id):
            stale += 1
        latencies.append((time.perf_counter() - t0) * 1000)
    total_reads = router.reads_on_replica + router.reads_on_primary
    return {
        "strategy": strategy,
        "stale": stale,
        "n": n,
        "stale_pct": 100.0 * stale / n,
        "replica_pct": 100.0 * router.reads_on_replica / max(total_reads, 1),
        "p50": lab_db.percentile(latencies, 50),
        "p99": lab_db.percentile(latencies, 99),
    }


def run_delayed(primary, replica, strategy: str, pause_ms: float) -> dict:
    """The same four fixes, with a pause between the write and the read.

    The writes are done first and the reads afterwards, so the pause is paid
    ONCE for the whole row instead of once per request -- N x 2.5s of sleeping
    would be a quarter of an hour for one line of a table. Each read is replayed
    against the routing state its own write produced (token, and the time of
    that write), so every routing decision is exactly the one the request would
    have made.
    """
    router = Router(primary, replica, strategy)
    n = requests_for(strategy)
    written = []
    for i in range(n):
        row_id = router.write(f"{strategy}-delayed-{i}")
        written.append((row_id, router.token, router.last_write_at))
    time.sleep(pause_ms / 1000.0)

    stale = 0
    latencies = []
    for row_id, token, wrote_at in written:
        router.token = token
        router.last_write_at = wrote_at
        t0 = time.perf_counter()
        if not router.read(row_id):
            stale += 1
        latencies.append((time.perf_counter() - t0) * 1000)
    total_reads = router.reads_on_replica + router.reads_on_primary
    return {
        "strategy": strategy,
        "stale": stale,
        "n": n,
        "stale_pct": 100.0 * stale / n,
        "replica_pct": 100.0 * router.reads_on_replica / max(total_reads, 1),
        "p50": lab_db.percentile(latencies, 50),
        "p99": lab_db.percentile(latencies, 99),
    }


def main() -> None:
    lab_db.ensure_database()
    replica = repl_lab.replica_or_exit("Read-your-own-writes: three fixes")
    primary = lab_db.connect()
    repl_lab.ensure_probe_table(primary, replica)

    lab_db.banner("Read-your-own-writes: three fixes, and what each costs")
    delay = replica.execute("SHOW recovery_min_apply_delay").fetchone()[0]
    version = lab_db.server_version(replica)
    print(f"  replica recovery_min_apply_delay = {delay}, {REQUESTS} write+read pairs each")
    print(f"  ('wait' blocks for the apply delay on every request, so it runs "
          f"{WAIT_REQUESTS}; the table says which)")
    print("  table 1: the read fires immediately after the write.")
    print(f"  table 2: the read fires {PAUSE_MS:.0f}ms later.")
    print(f"  sticky window = {STICKY_MS:.0f}ms")
    print("\n  Predict two numbers per row before running: the stale-read rate, and the")
    print("  share of reads that still reach the replica. The second is what the fix cost.")

    strategies = ["none", "sticky", "lsn"]
    have_wait = version >= 190000
    lab_db.gate("WAIT FOR LSN (PG19)", have_wait,
                "needs a Postgres 19 server; build one as a separate container and treat "
                "its numbers as beta numbers, or record this row as not measured")
    if have_wait:
        strategies.append("wait")

    print(f"\n  {'fix':<14}{'stale reads':>14}{'rate':>9}{'% on replica':>15}"
          f"{'p50 ms':>10}{'p99 ms':>10}")
    print("  " + "-" * 74)
    rows = []
    for strategy in strategies:
        try:
            r = run(primary, replica, strategy)
        except psycopg.Error as exc:
            print(f"  {strategy:<14}failed: {exc}")
            continue
        rows.append(r)
        print(f"  {strategy:<14}{r['stale']:>9} / {r['n']:<4}{r['stale_pct']:>8.1f}%"
              f"{r['replica_pct']:>14.1f}%{r['p50']:>10.1f}{r['p99']:>10.1f}")

    # ------------------------------------------------------------------
    # Table 2. The same fixes, with a pause between write and read.
    # ------------------------------------------------------------------
    print(f"\n  the same fixes with a {PAUSE_MS:.0f}ms pause between the write and the read")
    print(f"  (the sticky window is still {STICKY_MS:.0f}ms, so it has not expired yet):")
    print(f"\n  {'fix':<14}{'stale reads':>14}{'rate':>9}{'% on replica':>15}"
          f"{'p50 ms':>10}{'p99 ms':>10}")
    print("  " + "-" * 74)
    delayed = []
    for strategy in strategies:
        try:
            r = run_delayed(primary, replica, strategy, PAUSE_MS)
        except psycopg.Error as exc:
            print(f"  {strategy:<14}failed: {exc}")
            continue
        delayed.append(r)
        print(f"  {strategy:<14}{r['stale']:>9} / {r['n']:<4}{r['stale_pct']:>8.1f}%"
              f"{r['replica_pct']:>14.1f}%{r['p50']:>10.1f}{r['p99']:>10.1f}")

    d = {r["strategy"]: r for r in delayed}
    if "sticky" in d and "lsn" in d:
        print()
        print(f"  This is the table that separates them. The pause ({PAUSE_MS:.0f}ms) is longer")
        print("  than the replica's apply delay, so the row IS on the replica by read time.")
        print(f"    sticky sent {d['sticky']['replica_pct']:.0f}% of reads to the replica -- its window is a")
        print("           guess about time, and it is still counting down.")
        print(f"    lsn    sent {d['lsn']['replica_pct']:.0f}% of reads to the replica, at "
              f"{d['lsn']['stale_pct']:.0f}% stale -- it asked the")
        print("           replica where it actually was instead of guessing.")
        print("  That difference is the whole reason to carry a token. In table 1 the two")
        print("  fixes are indistinguishable; the cost of a guess only shows up once the")
        print("  guess has a chance to be wrong.")

    by = {r["strategy"]: r for r in rows}
    print()
    if "none" in by and "lsn" in by:
        print(f"  none:   {by['none']['stale_pct']:.0f}% stale, "
              f"{by['none']['replica_pct']:.0f}% of reads on the replica. Cheapest and wrong.")
        print(f"  sticky: {by['sticky']['stale_pct']:.0f}% stale, "
              f"{by['sticky']['replica_pct']:.0f}% on the replica -- it works here by pushing")
        print(f"          essentially every read back to the primary for {STICKY_MS:.0f}ms.")
        print("          Shorten the window and staleness returns; lengthen it and the")
        print("          replica stops carrying read traffic for anybody who writes.")
        print(f"  lsn:    {by['lsn']['stale_pct']:.0f}% stale, "
              f"{by['lsn']['replica_pct']:.0f}% on the replica in table 1 -- identical to sticky,")
        print("          because a read fired microseconds after a write CANNOT be served by")
        print("          a replica that is 2s behind, whatever you route it with. Table 2 is")
        if 'lsn' in d:
            print(f"          where it earns its keep: {d['lsn']['replica_pct']:.0f}% of those reads went to the replica")
            print(f"          against sticky's {d['sticky']['replica_pct']:.0f}%, at the same 0% stale. The token is a")
        print("          measurement of position; the sticky window is a guess about time.")
    if not have_wait:
        print("\n  WAIT FOR LSN row: NOT MEASURED on this server. It is a PG19 feature and")
        print("  this is not a PG19 server, so there is no honest number to put in the")
        print("  table. Leave that row of your notes blank rather than estimating it.")

    print("\n  The bug worth naming twice, because it survives review: comparing the token")
    print("  against pg_last_wal_receive_lsn() instead of pg_last_wal_replay_lsn().")
    print("  Received means the standby has the bytes. Replayed means a query can see")
    print("  them. Between the two, your check passes and your read is stale.")
    print("\n  And where this belongs in the code: below the ORM session -- a routing")
    print("  engine or a get_bind() override -- not at the call sites. Every call site")
    print("  that reaches for the replica engine by hand is a consistency decision")
    print("  nobody wrote down.")
    primary.execute("TRUNCATE repl_probe")
    replica.close()
    primary.close()


if __name__ == "__main__":
    main()
