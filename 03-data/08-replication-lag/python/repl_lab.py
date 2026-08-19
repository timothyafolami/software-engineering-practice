"""
Shared helpers for Topic 8: finding the replica, and measuring lag honestly.

Imported by stale_reads.py, lsn_token.py and lag_monitor.py; not run directly.

THE ONE THING WORTH READING HERE is `lag()` and why it reports BYTES first.

Seconds-based lag is derived from timestamps carried in the WAL, so on an IDLE
primary -- no new writes -- the replica is perfectly caught up and the "seconds
behind" number grows anyway, because the newest timestamp it has replayed keeps
getting older. Byte lag on an idle primary is zero, correctly. Every
seconds-behind alert anybody has ever written pages at 3am on a quiet Sunday for
exactly this reason.

    pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)    <- bytes, from the primary
    pg_last_xact_replay_timestamp()                      <- seconds, and misleading

RECEIVED vs REPLAYED is the other distinction that decides correctness:

    pg_last_wal_receive_lsn()   the standby has the bytes
    pg_last_wal_replay_lsn()    the standby has APPLIED them and a query can see them

Comparing an LSN token against the RECEIVE lsn is the bug people ship: the
standby has your data on disk and has not made it visible yet, so the check
passes and the read is still stale.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402

try:
    import psycopg
except ImportError:  # pragma: no cover
    sys.exit("This layer needs psycopg 3: python3 -m pip install 'psycopg[binary]'")

REPLICA_DSN = os.environ.get("LAB_REPLICA_DSN", "")

UNBLOCK = """\
  This experiment needs a SECOND Postgres replaying the first one's WAL.

  unblock (about a minute, and it starts a daemon -- that is why it is a script
  you run rather than something a program does to your machine):

    bash 08-replication-lag/scripts/start_replica.sh
    export LAB_REPLICA_DSN="postgresql://?host=/tmp&port=5433&dbname=sep_lab_03_data"
    python3 08-replication-lag/scripts/wait_for_replica.py

  or bring up the Docker stack, which pins Postgres 18 and gives you the same
  two nodes with `postgres-primary` and `postgres-replica`:

    docker compose -f lab/docker/compose.yml up -d postgres-primary postgres-replica

  tear the local one down again with:

    bash 08-replication-lag/scripts/start_replica.sh --stop
"""


def replica_or_exit(what: str):
    """Open the replica, or explain precisely why we cannot and stop.

    Refusing to run is the point. A version of this experiment that silently
    reads from the primary when no replica is configured would report zero stale
    reads and teach you the opposite of the truth.
    """
    if not REPLICA_DSN:
        lab_db.banner(f"{what} -- BLOCKED")
        print("  LAB_REPLICA_DSN is not set, so there is no replica to read from.")
        print()
        print(UNBLOCK)
        sys.exit(0)
    try:
        conn = psycopg.connect(REPLICA_DSN, autocommit=True)
    except psycopg.OperationalError as exc:
        lab_db.banner(f"{what} -- BLOCKED")
        print(f"  LAB_REPLICA_DSN={REPLICA_DSN} did not connect: {exc}")
        print()
        print(UNBLOCK)
        sys.exit(0)

    in_recovery = conn.execute("SELECT pg_is_in_recovery()").fetchone()[0]
    if not in_recovery:
        lab_db.banner(f"{what} -- BLOCKED")
        print("  That server is NOT in recovery: pg_is_in_recovery() returned false, so it")
        print("  is a primary, not a standby. Reading from it would show zero stale reads")
        print("  and prove nothing at all.")
        print()
        print(UNBLOCK)
        conn.close()
        sys.exit(0)
    conn.execute("SELECT set_config('application_name', 'sep-replica-reader', false)")
    return conn


def lag(primary) -> dict:
    """Lag as the primary sees it. Bytes first; seconds included to be argued with."""
    row = primary.execute(
        """
        SELECT application_name,
               pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn)   AS sent_bytes,
               pg_wal_lsn_diff(pg_current_wal_lsn(), write_lsn)  AS write_bytes,
               pg_wal_lsn_diff(pg_current_wal_lsn(), flush_lsn)  AS flush_bytes,
               pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_bytes,
               extract(epoch FROM replay_lag) AS replay_lag_s,
               state, sync_state
        FROM pg_stat_replication
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return {"connected": False}
    return {
        "connected": True, "application_name": row[0], "sent_bytes": row[1],
        "write_bytes": row[2], "flush_bytes": row[3], "replay_bytes": row[4],
        "replay_lag_s": row[5], "state": row[6], "sync_state": row[7],
    }


def replica_positions(replica) -> dict:
    """What the standby itself reports. The receive/replay gap is the point."""
    row = replica.execute(
        """
        SELECT pg_last_wal_receive_lsn(),
               pg_last_wal_replay_lsn(),
               pg_last_xact_replay_timestamp(),
               extract(epoch FROM (now() - pg_last_xact_replay_timestamp()))
        """
    ).fetchone()
    return {"receive_lsn": row[0], "replay_lsn": row[1],
            "last_xact_ts": row[2], "seconds_behind": row[3]}


def current_lsn(primary) -> str:
    return str(primary.execute("SELECT pg_current_wal_lsn()").fetchone()[0])


def replayed_through(replica, lsn: str) -> bool:
    """Has the standby APPLIED everything up to this LSN?

    pg_last_wal_replay_lsn(), never pg_last_wal_receive_lsn(). Received means the
    bytes arrived; replayed means a query can see them. Getting this wrong is the
    single most common way an LSN-token implementation still serves stale reads
    while looking correct in review.
    """
    row = replica.execute(
        "SELECT pg_last_wal_replay_lsn() >= %s::pg_lsn", (lsn,)
    ).fetchone()
    return bool(row[0])


def ensure_probe_table(primary, replica=None, timeout_s: float = 60.0) -> None:
    """A tiny table this topic owns, so nothing here disturbs the seeded tables.

    The `replica` argument is not optional in practice, and the reason is the
    first thing this topic teaches: DDL replicates through the WAL like anything
    else, so on a standby running `recovery_min_apply_delay = 2s` the table does
    not exist yet at the instant CREATE TABLE returns on the primary. A program
    that creates it and reads the standby immediately dies with

        psycopg.errors.UndefinedTable: relation "repl_probe" does not exist

    which is a stale read wearing a crash costume. Wait for the CREATE to be
    REPLAYED -- not received -- before measuring anything.
    """
    primary.execute(
        """
        CREATE TABLE IF NOT EXISTS repl_probe (
            id         bigserial PRIMARY KEY,
            written_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            payload    text        NOT NULL
        )
        """
    )
    if replica is None:
        return
    lsn = current_lsn(primary)
    deadline = time.monotonic() + timeout_s
    waited = False
    while not replayed_through(replica, lsn):
        if time.monotonic() > deadline:
            sys.exit(
                f"  the standby did not replay the probe-table DDL within {timeout_s:.0f}s.\n"
                "  unblock: check the standby is still streaming --\n"
                "    psql -h /tmp -p 5433 -d postgres -c 'SELECT pg_is_in_recovery()'\n"
                "    psql -d postgres -c 'SELECT * FROM pg_stat_replication'"
            )
        waited = True
        time.sleep(0.1)
    if waited:
        print("  (waited for the probe table's CREATE to REPLAY on the standby --\n"
              "   DDL travels through the WAL too, and the apply delay holds it back\n"
              "   exactly as it holds back your rows)")
