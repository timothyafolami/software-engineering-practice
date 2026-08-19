"""
Layer 4 Topic 3 (Part B, local) -- lost updates from a 250ms clock offset.

WHAT THIS DEMONSTRATES: two writers contending on a small key space, with
last-write-wins resolved three different ways, while writer B's clock is offset.

  v0  LWW on a CLIENT-generated updated_at.  Writer B is ahead, so B's writes
      beat A's even when A wrote LATER in real time. A's write is discarded with
      no error, no log line and no exception -- the user's change reverts.
  v1  LWW on the DATABASE's now(), one clock for both writers. Same load, same
      offset, and the offset stops mattering because it is no longer consulted.
  v2  compare-and-set on a version column. Rejections here are CORRECT behaviour
      rather than errors, and the number to record is the retry cost they add.

WHAT TO LOOK FOR IN THE OUTPUT: the LOST UPDATES row for v0 at a non-zero offset,
and that the same number for v1 is zero. Then the rejected-CAS count for v2,
which is not a failure -- it is the design telling the truth about a conflict
instead of silently picking a winner.

WHAT THIS IS NOT: the compose experiment. That runs two container writers under
k6 (see ../lab/README.md) and is blocked while the Docker daemon is down. This is
two writer THREADS against whatever Postgres is listening. It reproduces the loss
faithfully because the skew is application-level in both versions -- lab/README.md
explains why it has to be -- and because the mechanism needs only two writers
contending on one row. What it does not reproduce is network delay between the
writers and the database, which changes the *rate* of collisions and not the
existence of them.

  python3 python/lww_writers.py --variant v0 --offset-ms 250
  python3 python/lww_writers.py --variant v0 --offset-ms 0
  python3 python/lww_writers.py --variant v1 --offset-ms 250
  python3 python/lww_writers.py --variant v2 --offset-ms 250
  psql -d sep_lab_04_dist -f sql/topic3_lost_updates.sql
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import uuid

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"),
)
import lab_db  # noqa: E402  - after the sys.path fix, on purpose

DDL = """
CREATE TABLE IF NOT EXISTS lww_items (
    run_id        text        NOT NULL,
    key           text        NOT NULL,
    value         text        NOT NULL,
    version       bigint      NOT NULL DEFAULT 0,
    updated_at    timestamptz NOT NULL,   -- the field LWW compares; v0 fills it
                                          -- from the CLIENT, v1 from the server
    writer        text        NOT NULL,
    applied_db_ts timestamptz NOT NULL,   -- when the database actually wrote it.
                                          -- Never used to resolve anything; it
                                          -- exists so the experiment has a
                                          -- referee that is not under test.
    PRIMARY KEY (run_id, key)
);

-- Server-side truth. Every write ATTEMPT lands here, accepted or not, stamped
-- with clock_timestamp() -- the database's own clock, read at statement time.
-- Without this table there is no way to tell a lost update from a write that
-- never happened, which is the whole reason Topic 1 insisted on server-side
-- truth in the first place.
CREATE TABLE IF NOT EXISTS lww_write_log (
    id             bigserial PRIMARY KEY,
    run_id         text        NOT NULL,
    variant        text        NOT NULL,
    offset_ms      integer     NOT NULL,
    key            text        NOT NULL,
    writer         text        NOT NULL,
    seq            bigint      NOT NULL,
    client_ts      timestamptz NOT NULL,  -- the writer's own (possibly wrong) clock
    submitted_db_ts timestamptz NOT NULL, -- database clock, read just BEFORE the write
    executed_db_ts  timestamptz NOT NULL, -- database clock, inside the write itself
    winner_db_ts    timestamptz,          -- when the row this lost to was written
    outcome        text        NOT NULL
                   CHECK (outcome IN ('applied', 'rejected_lww', 'rejected_cas'))
);
CREATE INDEX IF NOT EXISTS lww_write_log_run_idx
    ON lww_write_log (run_id, key, executed_db_ts);
"""


class OffsetClock:
    """The writer's own now(). Writer B's is wrong by --offset-ms.

    Application-level, not system-level, and lab/README.md explains why that is
    the only option: CLOCK_REALTIME is deliberately not namespaced by Linux time
    namespaces, and Docker Desktop is one VM, so there is no flag that skews one
    container's wall clock. Doing it here makes the independent variable explicit
    instead of magic, and it behaves identically on this Mac and in CI.
    """

    def __init__(self, offset_ms: float) -> None:
        self.offset = offset_ms / 1000.0

    def now(self) -> float:
        return time.time() + self.offset


def writer_loop(name: str, clock: OffsetClock, run_id: str, variant: str, offset_ms: int,
                keys: list[str], stop: threading.Event, stats: dict[str, int],
                rate_hz: float) -> None:
    """One writer. Its own connection, its own clock, its own sequence numbers."""
    seq = 0
    interval = 1.0 / rate_hz
    next_at = time.perf_counter()
    write = WRITERS[variant]
    with lab_db.connect(autocommit=True) as conn:
        while not stop.is_set():
            key = keys[seq % len(keys)]
            seq += 1
            # Read the database's clock immediately before the write. This is the
            # referee: the client clock is the thing under test and cannot also
            # be the judge, and a timestamp taken after the write cannot tell a
            # write that waited on a lock from one that did not.
            submitted_at, = conn.execute("SELECT clock_timestamp()").fetchone()
            client_ts = clock.now()
            outcome, executed_at, winner_at = write(
                conn, run_id, key, f"{name}-{seq}", name, client_ts)

            conn.execute(
                "INSERT INTO lww_write_log (run_id, variant, offset_ms, key, writer,"
                " seq, client_ts, submitted_db_ts, executed_db_ts, winner_db_ts,"
                " outcome)"
                " VALUES (%s, %s, %s, %s, %s, %s, to_timestamp(%s), %s, %s, %s, %s)",
                (run_id, variant, offset_ms, key, name, seq, client_ts,
                 submitted_at, executed_at, winner_at, outcome),
            )
            stats[outcome] = stats.get(outcome, 0) + 1

            next_at += interval
            delay = next_at - time.perf_counter()
            if delay > 0:
                stop.wait(delay)
            else:
                next_at = time.perf_counter()


def write_v0(conn, run_id: str, key: str, value: str, writer: str,
             client_ts: float):
    """LWW on a CLIENT timestamp. The bug, written the way it is usually written.

    Nobody sits down and decides to resolve conflicts with an unsynchronised
    clock. It arrives as `updated_at = datetime.now()` in a model, and this WHERE
    clause arrives later as "don't clobber newer data".

    The statement also returns the database's own clock at the moment the write
    executed, so the experiment's referee never depends on a client clock -- and
    the client clock is the thing under test.
    """
    return _apply(conn, run_id, key,
                  """
                  WITH ins AS (
                      INSERT INTO lww_items
                             (run_id, key, value, updated_at, writer, applied_db_ts)
                      VALUES (%(run)s, %(key)s, %(val)s, to_timestamp(%(cts)s),
                              %(w)s, clock_timestamp())
                      ON CONFLICT (run_id, key) DO UPDATE
                         SET value = EXCLUDED.value,
                             updated_at = EXCLUDED.updated_at,
                             writer = EXCLUDED.writer,
                             applied_db_ts = clock_timestamp()
                       WHERE lww_items.updated_at < EXCLUDED.updated_at
                      RETURNING 1
                  )
                  SELECT (SELECT count(*) FROM ins), clock_timestamp()
                  """,
                  {"run": run_id, "key": key, "val": value, "cts": client_ts,
                   "w": writer},
                  "rejected_lww")


def write_v1(conn, run_id: str, key: str, value: str, writer: str, client_ts: float):
    """LWW on the DATABASE's clock. One clock, so there is no skew to have.

    now() is transaction start time from one machine, consistently. Not
    clock_timestamp(), which moves within a transaction -- and not a timestamp
    the application computed and passed in as a parameter, which is v0 wearing a
    disguise and is precisely what the README's broken-experiment note warns
    about. The client_ts argument is accepted and deliberately ignored.
    """
    return _apply(conn, run_id, key,
                  """
                  WITH ins AS (
                      INSERT INTO lww_items
                             (run_id, key, value, updated_at, writer, applied_db_ts)
                      VALUES (%(run)s, %(key)s, %(val)s, now(), %(w)s, clock_timestamp())
                      ON CONFLICT (run_id, key) DO UPDATE
                         SET value = EXCLUDED.value,
                             updated_at = now(),
                             writer = EXCLUDED.writer,
                             applied_db_ts = clock_timestamp()
                       WHERE lww_items.updated_at < now()
                      RETURNING 1
                  )
                  SELECT (SELECT count(*) FROM ins), clock_timestamp()
                  """,
                  {"run": run_id, "key": key, "val": value, "w": writer},
                  "rejected_lww")


def write_v2(conn, run_id: str, key: str, value: str, writer: str, client_ts: float):
    """Compare-and-set on a version column. No clock is consulted at all.

    Update only if the version has not moved. A rejection is not an error -- it
    is the database telling you somebody else wrote in between, which is exactly
    the information v0 and v1 throw away. The caller can see it, and can retry.
    """
    return _apply(conn, run_id, key,
                  """
                  WITH cur AS (
                      SELECT version FROM lww_items
                       WHERE run_id = %(run)s AND key = %(key)s
                  ), ins AS (
                      INSERT INTO lww_items
                             (run_id, key, value, version, updated_at, writer,
                              applied_db_ts)
                      VALUES (%(run)s, %(key)s, %(val)s,
                              coalesce((SELECT version FROM cur), 0) + 1,
                              now(), %(w)s, clock_timestamp())
                      ON CONFLICT (run_id, key) DO UPDATE
                         SET value = EXCLUDED.value,
                             version = lww_items.version + 1,
                             updated_at = now(),
                             writer = EXCLUDED.writer,
                             applied_db_ts = clock_timestamp()
                       WHERE lww_items.version = coalesce((SELECT version FROM cur), -1)
                      RETURNING 1
                  )
                  SELECT (SELECT count(*) FROM ins), clock_timestamp()
                  """,
                  {"run": run_id, "key": key, "val": value, "w": writer},
                  "rejected_cas")


def _apply(conn, run_id: str, key: str, sql: str, params: dict, reject_outcome: str):
    """Run the write, then -- only if it lost -- find out what beat it.

    The winner is read in a SEPARATE statement on purpose. Reading it inside the
    same statement looks tidier and is wrong: a data-modifying CTE's effects are
    not visible to the rest of its own statement, and under READ COMMITTED the
    write that beat us may have committed after our snapshot was taken. The
    subquery would then name the wrong row and quietly inflate every number
    below. A second statement sees the winner, and if a third write lands in
    between it names one that is even newer -- which biases the lost-update count
    DOWN. Wrong in the safe direction is the only kind of wrong worth having in a
    measurement.
    """
    applied, executed_at = conn.execute(sql, params).fetchone()
    if applied == 1:
        return "applied", executed_at, None
    winner_at, = conn.execute(
        "SELECT applied_db_ts FROM lww_items WHERE run_id = %s AND key = %s",
        (run_id, key),
    ).fetchone()
    return reject_outcome, executed_at, winner_at


WRITERS = {"v0": write_v0, "v1": write_v1, "v2": write_v2}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--variant", choices=("v0", "v1", "v2"), default="v0")
    ap.add_argument("--offset-ms", type=int, default=250,
                    help="writer B's clock offset (CLOCK_OFFSET_MS in the compose stack)")
    ap.add_argument("--keys", type=int, default=10,
                    help="key space. Small on purpose: two writers spread over a large "
                         "key space at a low rate never collide, and then zero lost "
                         "updates means nothing at all.")
    ap.add_argument("--rate", type=float, default=50.0, help="writes per second per writer")
    ap.add_argument("--seconds", type=float, default=10.0)
    args = ap.parse_args(argv)

    run_id = f"{args.variant}-off{args.offset_ms}-{uuid.uuid4().hex[:6]}"
    keys = [f"k{i:02d}" for i in range(args.keys)]
    conn = lab_db.open_lab(ddl=DDL)

    lab_db.banner(f"Topic 3 Part B (local) -- {args.variant}, writer B offset "
                  f"{args.offset_ms:+d} ms")
    print(f"  server        : {lab_db.describe_server(conn)}")
    print(f"  run id        : {run_id}")
    print(f"  writers       : writer-a (offset 0), writer-b (offset {args.offset_ms:+d} ms)")
    print(f"  key space     : {args.keys} keys, {args.rate:.0f} writes/s each, "
          f"{args.seconds:.0f}s")
    print( "  clock         : application-level offset; the system clock is untouched")

    stop = threading.Event()
    stats_a: dict[str, int] = {}
    stats_b: dict[str, int] = {}
    threads = [
        threading.Thread(target=writer_loop, daemon=True,
                         args=("writer-a", OffsetClock(0), run_id, args.variant,
                               args.offset_ms, keys, stop, stats_a, args.rate)),
        threading.Thread(target=writer_loop, daemon=True,
                         args=("writer-b", OffsetClock(args.offset_ms), run_id,
                               args.variant, args.offset_ms, keys, stop, stats_b,
                               args.rate)),
    ]
    for t in threads:
        t.start()
    time.sleep(args.seconds)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    # ------------------------------------------------------------ the numbers
    # A LOST UPDATE is defined server-side, from the database's own clock, because
    # the client timestamps are the thing under test and cannot also be the
    # referee. Derive the definition rather than take it on faith.
    #
    # A rejected write is only a *loss* when it was not concurrent with the write
    # that beat it. If the two overlapped in real time, either could legitimately
    # win and no clock is at fault. So the test is the unambiguous one:
    #
    #     the rejected write was SUBMITTED after the winning write had already
    #     FINISHED executing        (submitted_db_ts > winner_db_ts)
    #
    # There is no reading of "later" under which that write should have lost. It
    # is a user's change discarded in favour of an older one, with no error
    # returned to anybody. Rejections that fail this test are counted separately
    # as genuine concurrent conflicts -- they are not the bug this topic is about.
    #
    # Only rejected_lww rows can qualify. v2's rejected_cas is a different animal:
    # the caller is TOLD, and can retry. A CAS rejection is the system working.
    lost, concurrent, stale_p50, stale_max = conn.execute(
        """
        SELECT count(*) FILTER (WHERE submitted_db_ts > winner_db_ts),
               count(*) FILTER (WHERE submitted_db_ts <= winner_db_ts),
               coalesce(round(1000 * percentile_disc(0.5) WITHIN GROUP (
                   ORDER BY extract(epoch FROM submitted_db_ts - winner_db_ts))
                   FILTER (WHERE submitted_db_ts > winner_db_ts)), 0),
               coalesce(round(1000 * max(extract(epoch FROM submitted_db_ts - winner_db_ts))
                   FILTER (WHERE submitted_db_ts > winner_db_ts)), 0)
        FROM   lww_write_log
        WHERE  run_id = %s AND outcome = 'rejected_lww' AND winner_db_ts IS NOT NULL
        """,
        (run_id,),
    ).fetchone()

    counts = conn.execute(
        "SELECT writer, outcome, count(*) FROM lww_write_log WHERE run_id = %s"
        " GROUP BY 1, 2 ORDER BY 1, 2", (run_id,)
    ).fetchall()
    total = sum(n for _, _, n in counts)
    rejected = sum(n for _, o, n in counts if o != "applied")

    lab_db.section("outcomes")
    print(f"  {'writer':<12}{'outcome':<16}{'count'}")
    for writer, outcome, n in counts:
        print(f"  {writer:<12}{outcome:<16}{n}")
    print(f"\n  writes issued            {total}")
    print(f"  writes rejected          {rejected}"
          f"  ({100.0 * rejected / max(total, 1):.1f}%)")
    print(f"  LOST UPDATES             {lost}")
    print( "  ^ rejected writes that were SUBMITTED after the winning write had")
    print( "    already finished. Not concurrent, not a judgement call: a change")
    print( "    discarded in favour of an older one, with nothing returned to the")
    print( "    caller. No exception, no status code, no row count anybody checks.")
    cas_rejected = conn.execute(
        "SELECT count(*) FROM lww_write_log WHERE run_id = %s"
        " AND outcome = 'rejected_cas'", (run_id,)).fetchone()[0]
    print(f"  concurrent conflicts     {concurrent}")
    print( "  ^ rejections where the two writes overlapped in real time. Either")
    print( "    could legitimately have won; these are not this topic's bug.")
    print(f"  rejected CAS attempts    {cas_rejected}")
    print( "  ^ v2 only. Not losses: the caller is TOLD and can retry. The number")
    print( "    to record next to this one is the retry cost it adds.")
    if lost:
        print(f"  how far back it reverted  p50 {int(stale_p50)} ms   max {int(stale_max)} ms")
        print( "  ^ the gap between the losing write's submission and the moment the")
        print( "    value that beat it was written. That is how much of the user's")
        print( "    world went backwards, per lost write.")

    skew = conn.execute(
        "SELECT writer,"
        " round(avg(extract(epoch FROM client_ts - executed_db_ts)) * 1000)::int,"
        "       count(*) FROM lww_write_log WHERE run_id = %s GROUP BY 1 ORDER BY 1",
        (run_id,),
    ).fetchall()
    lab_db.section("skew as the database saw it (client_ts - executed_db_ts, ms)")
    for writer, delta, n in skew:
        print(f"  {writer:<12}{delta:+6d} ms   over {n} writes")
    print("\n  If these two rows are not ~{0} ms apart, the offset did not take effect"
          .format(args.offset_ms))
    print("  and nothing below this line means anything.")

    print()
    print(f"  full breakdown:  psql -d {lab_db.DB_NAME} -f sql/topic3_lost_updates.sql")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
