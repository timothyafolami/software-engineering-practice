"""
Layer 4 Topic 6 -- the high-water-mark relay skips rows, silently and forever.

WHAT THIS DEMONSTRATES: the bug you are most likely to write yourself in this
topic, reproduced with evidence, then fixed with evidence, in one program.

  Sequences allocate their values OUTSIDE the transaction. Transaction X can
  take id = 99, transaction Y can take id = 100, and Y can COMMIT FIRST. A relay
  that remembers last_seen_id and queries `WHERE id > last_seen` reads row 100,
  advances its mark to 100, and only then does row 99 become visible -- behind
  the mark, forever. There is no error and no gap in the topic. The message
  simply does not exist.

  The fix is not a cleverer mark. It is to stop having one: a published_at
  column with FOR UPDATE SKIP LOCKED has no ordering assumption to violate.

WHAT TO LOOK FOR IN THE OUTPUT: the two relays run over the SAME t6_outbox rows,
written by the same writers in the same run. The high-water-mark relay reports a
non-zero PERMANENTLY SKIPPED count; the SKIP LOCKED relay reports zero. Then the
proof at the end: the skipped rows are still sitting there unpublished, and the
mark is already past them, so no future poll will ever return them.

WHY THE --hold-seconds FLAG EXISTS: without it, ids and commit order coincide
and there is nothing to skip -- which the README lists as a broken experiment
rather than a wrong prediction. One writer must START first (taking the lower
sequence value) and COMMIT second. The flag forces that.

  python3 python/hwm_skip.py --writers 2 --hold-seconds 2 --duration 60
  python3 python/hwm_skip.py --writers 2 --hold-seconds 2 --duration 20
  psql -d sep_lab_04_dist -f sql/topic6_reconcile.sql
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import threading
import time
import uuid

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"),
)
import lab_db  # noqa: E402  - after the sys.path fix, on purpose

# Table names carry a t6_ prefix because the whole layer shares ONE scratch
# database (lab/README.md), and Topic 2 already owns a table called `charges`.
# Colliding on a name here would either fail loudly or, worse, half-work.
DDL = """
CREATE TABLE IF NOT EXISTS t6_charges (
    id           bigserial PRIMARY KEY,
    run_id       text        NOT NULL,
    amount_cents integer     NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS t6_outbox (
    id            bigserial PRIMARY KEY,   -- allocated OUTSIDE the transaction.
                                           -- That is the whole bug; see header.
    run_id        text        NOT NULL,
    aggregate_id  bigint      NOT NULL,
    topic         text        NOT NULL,
    payload       jsonb       NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    committed_at  timestamptz,             -- filled by the writer at COMMIT time,
                                           -- so the id/commit inversion is visible
    published_at  timestamptz,             -- NULL = not yet published
    published_by  text
);
CREATE INDEX IF NOT EXISTS outbox_unpublished_idx
    ON t6_outbox (id) WHERE published_at IS NULL;

-- Stands in for the broker. A table, so the experiment can count what was
-- delivered without needing a broker that can be stopped -- which is the part of
-- this topic that genuinely needs Docker and is blocked here.
CREATE TABLE IF NOT EXISTS t6_delivered (
    id           bigserial PRIMARY KEY,
    run_id       text        NOT NULL,
    relay        text        NOT NULL,   -- WHICH relay design published it
    woken_by     text,                   -- and what woke it: notify | poll
    outbox_id    bigint      NOT NULL,
    delivered_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS t6_delivered_run_idx ON t6_delivered (run_id, relay);
"""


def writer(name: str, run_id: str, hold_seconds: float, stop: threading.Event,
           counts: dict[str, int], hold_probability: float) -> None:
    """One writer. Charge and t6_outbox row in ONE transaction, which is the point.

    Some transactions deliberately hold the row open before committing. That is
    not artificial: it is a slow external call, a lock wait, a big batch -- any
    of which stretches a transaction past a faster one that started later. The
    flag makes it deterministic instead of waiting for it to happen on its own.
    """
    # autocommit=True plus an EXPLICIT transaction() block, which is psycopg3's
    # recommended shape and not a detail. On a connection with autocommit=False,
    # the first statement -- here lab_db.connect's `SET application_name` -- has
    # already opened a transaction, so `with conn.transaction():` finds one in
    # progress and takes a SAVEPOINT instead of issuing BEGIN/COMMIT. Every write
    # in this loop then sits inside one enormous transaction that commits when
    # the connection closes, and the relays see nothing at all until the program
    # exits. This cost an hour: the symptom was a relay that looked broken and a
    # skip count of zero, and the bug was three levels away in a helper.
    with lab_db.connect(autocommit=True) as conn:
        while not stop.is_set():
            hold = hold_seconds if random.random() < hold_probability else 0.0
            with conn.transaction():
                charge_id, = conn.execute(
                    "INSERT INTO t6_charges (run_id, amount_cents) VALUES (%s, %s)"
                    " RETURNING id", (run_id, random.randint(100, 9999))
                ).fetchone()
                conn.execute(
                    "INSERT INTO t6_outbox (run_id, aggregate_id, topic, payload)"
                    " VALUES (%s, %s, 'payment.succeeded', %s)",
                    (run_id, charge_id, '{"event": "payment.succeeded"}'),
                )
                if hold:
                    # The sequence value is already taken. The commit has not
                    # happened. Everything that commits during this window gets a
                    # HIGHER id and becomes visible FIRST.
                    stop.wait(hold)
                conn.execute(
                    "UPDATE t6_outbox SET committed_at = clock_timestamp()"
                    " WHERE run_id = %s AND aggregate_id = %s",
                    (run_id, charge_id),
                )
            counts[name] = counts.get(name, 0) + 1
            stop.wait(0.02)


def relay_high_water_mark(run_id: str, stop: threading.Event, batch: int,
                          stats: dict[str, int]) -> None:
    """The bug. Remembers the largest id it has seen and never looks back.

    Reads perfectly reasonably. Uses the primary key index. Has no state to keep
    in the database. And it loses messages.
    """
    last_seen = 0
    with lab_db.connect(autocommit=True) as conn:
        while not stop.is_set():
            rows = conn.execute(
                "SELECT id FROM t6_outbox WHERE run_id = %s AND id > %s"
                " ORDER BY id LIMIT %s", (run_id, last_seen, batch)
            ).fetchall()
            for (oid,) in rows:
                conn.execute(
                    "INSERT INTO t6_delivered (run_id, relay, outbox_id)"
                    " VALUES (%s, 'high-water-mark', %s)", (run_id, oid)
                )
                last_seen = max(last_seen, oid)
                stats["published"] = stats.get("published", 0) + 1
            stats["mark"] = last_seen
            stop.wait(0.05)


def relay_skip_locked(run_id: str, name: str, stop: threading.Event, batch: int,
                      stats: dict[str, int]) -> None:
    """The fix. No mark, so no ordering assumption to violate.

    FOR UPDATE SKIP LOCKED is what lets you run more than one of these: each
    grabs a disjoint batch instead of blocking on the other's locks. Run this
    program with --relays 2 and watch both make progress with no duplicates.
    """
    # autocommit=True + explicit transaction(), for the reason in writer(): the
    # SELECT ... FOR UPDATE and the UPDATE that clears it must be one real
    # transaction, and a savepoint inside a never-committing outer transaction is
    # not that.
    with lab_db.connect(autocommit=True) as conn:
        while not stop.is_set():
            with conn.transaction():
                rows = conn.execute(
                    "SELECT id FROM t6_outbox"
                    " WHERE run_id = %s AND published_at IS NULL"
                    " ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s",
                    (run_id, batch),
                ).fetchall()
                for (oid,) in rows:
                    conn.execute(
                        "INSERT INTO t6_delivered (run_id, relay, outbox_id)"
                        " VALUES (%s, 'skip-locked', %s)", (run_id, oid)
                    )
                    conn.execute(
                        "UPDATE t6_outbox SET published_at = clock_timestamp(),"
                        " published_by = %s WHERE id = %s", (name, oid)
                    )
                    stats["published"] = stats.get("published", 0) + 1
            stop.wait(0.05)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--writers", type=int, default=2)
    ap.add_argument("--hold-seconds", type=float, default=2.0,
                    help="how long a held transaction stays open before commit. "
                         "0 makes ids and commit order coincide, and then there "
                         "is nothing to skip -- try it, it is the control run.")
    ap.add_argument("--hold-probability", type=float, default=0.5,
                    help="fraction of transactions that hold")
    ap.add_argument("--relays", type=int, default=1, help="SKIP LOCKED relay count")
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--duration", type=float, default=60.0)
    args = ap.parse_args(argv)

    run_id = f"t6-{uuid.uuid4().hex[:8]}"
    conn = lab_db.open_lab(ddl=DDL)

    lab_db.banner("Topic 6 -- the high-water-mark relay, reproduced and fixed")
    print(f"  server        : {lab_db.describe_server(conn)}")
    print(f"  run id        : {run_id}")
    print(f"  writers       : {args.writers}, {args.hold_probability:.0%} of them holding "
          f"{args.hold_seconds:.1f}s before commit")
    print(f"  relays        : 1 high-water-mark (the bug) + {args.relays} SKIP LOCKED (the fix)")
    print(f"  duration      : {args.duration:.0f}s")
    print( "  both relays read the SAME rows, so the comparison is not two runs.")

    # Two stop events. The writers stop first, and the relays keep running for a
    # drain window afterwards. Otherwise the high-water-mark relay would look bad
    # partly because it ran out of time, and the finding has to be that it CANNOT
    # deliver those rows -- not that it did not get round to them.
    #
    # The drain also has to happen on the SAME relay instance, keeping the same
    # in-memory mark. Restarting it would reset last_seen to 0 and it would
    # helpfully re-read everything, which is not a fix; it is a relay that has
    # forgotten its own state, and it would republish every message ever sent.
    stop_writers = threading.Event()
    stop_relays = threading.Event()
    counts: dict[str, int] = {}
    hwm_stats: dict[str, int] = {}
    skip_stats: dict[str, int] = {}
    threads = [
        threading.Thread(target=writer, daemon=True,
                         args=(f"w{i}", run_id, args.hold_seconds, stop_writers, counts,
                               args.hold_probability))
        for i in range(args.writers)
    ]
    threads.append(threading.Thread(target=relay_high_water_mark, daemon=True,
                                    args=(run_id, stop_relays, args.batch, hwm_stats)))
    for i in range(args.relays):
        threads.append(threading.Thread(target=relay_skip_locked, daemon=True,
                                        args=(run_id, f"relay-{i}", stop_relays, args.batch,
                                              skip_stats)))
    for t in threads:
        t.start()
    time.sleep(args.duration)
    stop_writers.set()
    drain_for = max(3.0, args.hold_seconds * 2)
    print(f"\n  writers stopped; letting both relays drain for {drain_for:.0f}s")
    time.sleep(drain_for)
    stop_relays.set()
    for t in threads:
        t.join(timeout=max(5.0, args.hold_seconds + 2))

    # ------------------------------------------------------------- the numbers
    total, = conn.execute(
        "SELECT count(*) FROM t6_outbox WHERE run_id = %s", (run_id,)).fetchone()
    inversions, = conn.execute(
        """
        SELECT count(*) FROM t6_outbox a JOIN t6_outbox b
          ON a.run_id = b.run_id AND a.id < b.id AND a.committed_at > b.committed_at
        WHERE a.run_id = %s
        """, (run_id,)).fetchone()
    hwm_delivered, = conn.execute(
        "SELECT count(DISTINCT outbox_id) FROM t6_delivered"
        " WHERE run_id = %s AND relay = 'high-water-mark'", (run_id,)).fetchone()
    skip_delivered, = conn.execute(
        "SELECT count(DISTINCT outbox_id) FROM t6_delivered"
        " WHERE run_id = %s AND relay = 'skip-locked'", (run_id,)).fetchone()
    hwm_dupes, = conn.execute(
        "SELECT count(*) - count(DISTINCT outbox_id) FROM t6_delivered"
        " WHERE run_id = %s AND relay = 'high-water-mark'", (run_id,)).fetchone()
    skip_dupes, = conn.execute(
        "SELECT count(*) - count(DISTINCT outbox_id) FROM t6_delivered"
        " WHERE run_id = %s AND relay = 'skip-locked'", (run_id,)).fetchone()
    skipped = conn.execute(
        """
        SELECT o.id, o.committed_at
        FROM   t6_outbox o
        WHERE  o.run_id = %s
          AND  NOT EXISTS (SELECT 1 FROM t6_delivered d
                            WHERE d.run_id = o.run_id
                              AND d.relay = 'high-water-mark'
                              AND d.outbox_id = o.id)
        ORDER  BY o.id
        """, (run_id,)).fetchall()

    lab_db.section("did the id / commit-order inversion actually happen?")
    print(f"  outbox rows written           {total}")
    print(f"  id/commit inversions          {inversions}")
    print( "  ^ pairs where the LOWER id committed LATER. If this is 0 the")
    print( "    experiment proved nothing: raise --hold-seconds or --writers.")
    if inversions == 0:
        print()
        print("  *** BROKEN RUN, not a wrong prediction. ***")
        print("  Ids and commit order coincided, so there was nothing to skip.")
        conn.close()
        return 1

    lab_db.section("what each relay delivered, over the same rows")
    print(f"  {'relay':<22}{'delivered':>11}{'PERMANENTLY SKIPPED':>22}{'duplicates':>13}")
    print(f"  {'high-water-mark':<22}{hwm_delivered:>11}{total - hwm_delivered:>22}{hwm_dupes:>13}")
    print(f"  {'SKIP LOCKED':<22}{skip_delivered:>11}{total - skip_delivered:>22}{skip_dupes:>13}")
    print()
    print(f"  high-water mark ended at id {hwm_stats.get('mark', 0)}")

    if skipped:
        lab_db.section(f"the {len(skipped)} rows the mark can never reach again")
        for oid, committed in skipped[:8]:
            print(f"    outbox id {oid:<8} committed {committed}  "
                  f"< mark {hwm_stats.get('mark', 0)}: unreachable")
        if len(skipped) > 8:
            print(f"    ... and {len(skipped) - 8} more")
        print()
        print("  Every one of these is a message that will never be delivered. There")
        print("  is no error anywhere, no gap in the topic, no dead-letter queue")
        print("  entry, and nothing in any dashboard. The row is in the database and")
        print("  the relay's query cannot see it.")

    lab_db.section("and the fix, stated as the reason it works")
    print("  The SKIP LOCKED relay has no mark. It asks 'which rows are still")
    print("  unpublished?', which is a question about state rather than about")
    print("  order, so a commit that arrives out of id order is simply a row that")
    print("  is still NULL and gets picked up on the next poll.")
    print()
    print("  A cleverer mark cannot fix this. Any mark is a claim that ids and")
    print("  commit order agree, and sequences do not make that promise.")
    print()
    print(f"  full breakdown:  psql -d {lab_db.DB_NAME} -f sql/topic6_reconcile.sql")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
