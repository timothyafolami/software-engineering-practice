"""
Layer 4 Topic 7 (parts 2-3, local) -- a paused leader, with and without fencing.

WHAT THIS DEMONSTRATES: two relay workers contend for a lease on a `t7_leases`
row. The elected one processes payouts. Then it is PAUSED for longer than the
lease TTL, the other worker takes over, and the paused one wakes up still
believing it holds the lease -- and writes.

  FENCING=0   the stale writer's UPDATE succeeds. Count duplicate payouts.
  FENCING=1   every write carries the epoch it was issued under and is guarded
              by `AND fence < $epoch`. Zero rows updated means you are stale; the
              stale writer logs loudly and exits.

WHAT THIS IS NOT: the compose experiment. That one SIGSTOPs a container
(`docker kill -s SIGSTOP relay-a`) and is blocked while the Docker daemon is
down. Here the pause is a thread that stops renewing and stops working for
`--pause-seconds` -- which is what a SIGSTOP, a CFS throttle, a stop-the-world
collection and a blocked event loop all look like FROM THE DATABASE'S SIDE, and
the database's side is where the safety argument has to hold. What it does not
reproduce is a pause landing in the middle of an in-flight statement.

WHAT TO LOOK FOR IN THE OUTPUT: DUPLICATE PAYOUTS with fencing off, zero with it
on, and the stale writer's REJECTED count -- writes it attempted and the resource
refused. A run where the stale writer attempts nothing has tested nothing.

  python3 python/fencing_demo.py --fencing 0
  python3 python/fencing_demo.py --fencing 1
  psql -d sep_lab_04_dist -f sql/topic7_duplicate_payouts.sql
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
CREATE TABLE IF NOT EXISTS t7_leases (
    name       text        PRIMARY KEY,
    holder     text,
    epoch      bigint      NOT NULL DEFAULT 0,  -- THE FENCING TOKEN. Monotonic,
                                                -- issued by the database, never
                                                -- a timestamp: a clock can go
                                                -- backwards and this cannot.
    expires_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS t7_payouts (
    id         bigserial PRIMARY KEY,
    run_id     text        NOT NULL,
    payout_key text        NOT NULL,
    status     text        NOT NULL DEFAULT 'pending',
    fence      bigint      NOT NULL DEFAULT 0,
    sent_by    text,
    sent_at    timestamptz,
    UNIQUE (run_id, payout_key)
);

-- Every attempt, accepted or refused. Without this there is no way to tell a
-- stale writer that was REJECTED from one that never tried -- and "fencing works
-- but you never saw a rejected write" is the README's broken-experiment case.
CREATE TABLE IF NOT EXISTS t7_payout_attempts (
    id          bigserial PRIMARY KEY,
    run_id      text        NOT NULL,
    fencing     boolean     NOT NULL,
    worker      text        NOT NULL,
    payout_key  text        NOT NULL,
    epoch       bigint      NOT NULL,
    rows_updated integer    NOT NULL,
    attempted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
"""


def acquire(conn, lease: str, holder: str, ttl: float) -> int | None:
    """Take the lease if it is free or expired. Returns the new epoch, or None.

    The epoch increments on ACQUISITION, not on renewal, so it names a
    leadership term rather than a heartbeat -- the same idea as Raft's term
    (Topic 5) and, in etcd, the key's CreateRevision.
    """
    row = conn.execute(
        "INSERT INTO t7_leases (name, holder, epoch, expires_at)"
        " VALUES (%s, %s, 1, now() + %s * interval '1 second')"
        " ON CONFLICT (name) DO UPDATE"
        "    SET holder = EXCLUDED.holder,"
        "        epoch = t7_leases.epoch + 1,"
        "        expires_at = EXCLUDED.expires_at"
        "  WHERE t7_leases.expires_at < now()"
        " RETURNING epoch",
        (lease, holder, ttl),
    ).fetchone()
    return row[0] if row else None


def renew(conn, lease: str, holder: str, epoch: int, ttl: float) -> bool:
    """Extend the lease, but only if it is still ours AND still this epoch."""
    cur = conn.execute(
        "UPDATE t7_leases SET expires_at = now() + %s * interval '1 second'"
        " WHERE name = %s AND holder = %s AND epoch = %s AND expires_at > now()",
        (ttl, lease, holder, epoch),
    )
    return cur.rowcount == 1


def send_payout(conn, run_id: str, key: str, worker: str, epoch: int,
                fencing: bool) -> int:
    """The effect. Returns rows updated -- zero means the resource refused.

    Note where the guard is. `AND fence < %s` is in the WHERE clause of the
    UPDATE, not in an `if` above it. An application-level check moves the race,
    it does not remove it: the same pause can land between your check and your
    UPDATE, and then you have written exactly the row you decided not to.
    """
    if fencing:
        cur = conn.execute(
            "UPDATE t7_payouts SET status = 'sent', fence = %s, sent_by = %s,"
            " sent_at = clock_timestamp()"
            " WHERE run_id = %s AND payout_key = %s AND fence < %s",
            (epoch, worker, run_id, key, epoch),
        )
    else:
        cur = conn.execute(
            "UPDATE t7_payouts SET status = 'sent', fence = %s, sent_by = %s,"
            " sent_at = clock_timestamp()"
            " WHERE run_id = %s AND payout_key = %s",
            (epoch, worker, run_id, key),
        )
    rows = cur.rowcount
    conn.execute(
        "INSERT INTO t7_payout_attempts (run_id, fencing, worker, payout_key,"
        " epoch, rows_updated) VALUES (%s, %s, %s, %s, %s, %s)",
        (run_id, fencing, worker, key, epoch, rows),
    )
    return rows


def worker_loop(name: str, run_id: str, keys: list[str], ttl: float, fencing: bool,
                pause_at: float | None, pause_for: float, stop: threading.Event,
                log: list[str]) -> None:
    epoch: int | None = None
    started = time.monotonic()
    paused = False
    with lab_db.connect(autocommit=True) as conn:
        while not stop.is_set():
            now = time.monotonic() - started
            if pause_at is not None and not paused and now >= pause_at:
                paused = True
                log.append(f"[{name}] PAUSED at t={now:.1f}s for {pause_for:.0f}s "
                           f"(holding epoch {epoch})")
                # The pause. No renewal, no work, no code running at all -- which
                # is what SIGSTOP, a CFS throttle, a stop-the-world collection
                # and a blocked event loop all look like from here.
                stop.wait(pause_for)
                log.append(f"[{name}] RESUMED at t={time.monotonic() - started:.1f}s, "
                           f"still believing it holds epoch {epoch}")
                # And this is the bug, written the way it is always written: the
                # holder does not re-check on resume, because from inside the
                # process nothing happened.
                for key in keys:
                    rows = send_payout(conn, run_id, key, name, epoch or 0, fencing)
                    if rows == 0:
                        log.append(f"[{name}] REJECTED writing {key} at epoch {epoch}: "
                                   f"zero rows updated -- I am stale, exiting")
                        return
                    if len(log) < 12:
                        log.append(f"[{name}] wrote {key} at epoch {epoch} AFTER the "
                                   f"pause -- accepted, nothing objected")
                    elif len(log) == 12:
                        log.append(f"[{name}] ... and kept going. Every one of these is "
                                   f"a stale write the resource accepted.")
                return

            if epoch is None:
                epoch = acquire(conn, "relay", name, ttl)
                if epoch is not None:
                    log.append(f"[{name}] acquired the lease at epoch {epoch} "
                               f"(t={now:.1f}s)")
                else:
                    stop.wait(0.25)
                    continue

            if not renew(conn, "relay", name, epoch, ttl):
                log.append(f"[{name}] lost the lease it held at epoch {epoch}")
                epoch = None
                continue

            # The steady-state loop claims work that is still pending, which is
            # what a relay does. The post-pause batch above deliberately does
            # NOT: a resumed holder replays the batch it believed it owned,
            # including rows somebody else has since finished.
            pending = conn.execute(
                "SELECT payout_key FROM t7_payouts WHERE run_id = %s"
                " AND status = 'pending' ORDER BY payout_key LIMIT 2", (run_id,)
            ).fetchall()
            for (key,) in pending:
                send_payout(conn, run_id, key, name, epoch, fencing)
            stop.wait(0.5)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--fencing", type=int, choices=(0, 1), default=0)
    ap.add_argument("--ttl", type=float, default=3.0, help="lease TTL in seconds")
    ap.add_argument("--pause-seconds", type=float, default=6.0,
                    help="how long worker A stops. Must exceed --ttl or nothing "
                         "is being tested.")
    ap.add_argument("--payouts", type=int, default=40,
                    help="more than one worker can drain before the pause, so "
                         "there is still work for the new leader to claim")
    ap.add_argument("--seconds", type=float, default=14.0)
    args = ap.parse_args(argv)

    if args.pause_seconds <= args.ttl:
        print(f"--pause-seconds ({args.pause_seconds}) must exceed --ttl ({args.ttl}), "
              "or the lease never expires and there is no split brain to observe.")
        return 2

    run_id = f"t7-{'fence' if args.fencing else 'nofence'}-{uuid.uuid4().hex[:6]}"
    keys = [f"payout-{i:03d}" for i in range(args.payouts)]
    conn = lab_db.open_lab(ddl=DDL)
    conn.execute("DELETE FROM t7_leases WHERE name = 'relay'")
    for key in keys:
        conn.execute(
            "INSERT INTO t7_payouts (run_id, payout_key) VALUES (%s, %s)"
            " ON CONFLICT DO NOTHING", (run_id, key))

    lab_db.banner(f"Topic 7 -- paused leader, FENCING={args.fencing}")
    print(f"  server        : {lab_db.describe_server(conn)}")
    print(f"  run id        : {run_id}")
    print(f"  lease TTL     : {args.ttl:.0f}s")
    print(f"  pause         : worker-a stops for {args.pause_seconds:.0f}s, "
          f"which is longer than the TTL")
    print(f"  guard         : "
          + ("`AND fence < $epoch` inside the UPDATE"
             if args.fencing else "NONE -- the stale writer's UPDATE is unguarded"))

    stop = threading.Event()
    log: list[str] = []
    threads = [
        threading.Thread(target=worker_loop, daemon=True,
                         args=("worker-a", run_id, keys, args.ttl, bool(args.fencing),
                               2.0, args.pause_seconds, stop, log)),
        threading.Thread(target=worker_loop, daemon=True,
                         args=("worker-b", run_id, keys, args.ttl, bool(args.fencing),
                               None, 0.0, stop, log)),
    ]
    threads[0].start()
    time.sleep(0.4)      # let A win the first election, deterministically
    threads[1].start()
    time.sleep(args.seconds)
    stop.set()
    for t in threads:
        t.join(timeout=args.pause_seconds + 3)

    lab_db.section("what happened, in order")
    for line in log:
        print("  " + line)

    attempts, accepted, rejected = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE rows_updated > 0),"
        " count(*) FILTER (WHERE rows_updated = 0)"
        " FROM t7_payout_attempts WHERE run_id = %s", (run_id,)).fetchone()
    # "Stale" means stale AT THE TIME OF THE ATTEMPT: a higher epoch had already
    # written something, earlier in real time. Comparing against the FINAL epoch
    # instead marks the old leader's perfectly legitimate pre-pause writes as
    # stale, and then fencing looks broken in every run. That was the first
    # version of this query and it reported eight stale accepted writes in a run
    # that had none.
    current_epoch, = conn.execute(
        "SELECT epoch FROM t7_leases WHERE name = 'relay'").fetchone()
    stale_attempts, stale_rejected = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE rows_updated = 0)"
        " FROM t7_payout_attempts a WHERE a.run_id = %s"
        "   AND EXISTS (SELECT 1 FROM t7_payout_attempts b"
        "                WHERE b.run_id = a.run_id AND b.epoch > a.epoch"
        "                  AND b.attempted_at < a.attempted_at)",
        (run_id,)).fetchone()
    # A duplicate payout is an accepted write from a STALE epoch: the resource
    # took it even though a HIGHER epoch had already written that key. Counting
    # "written under more than one epoch" instead would flag a legitimate
    # handover -- the new leader re-driving a pending payout is the system
    # working, and a metric that cannot tell those apart is worse than none.
    sent_twice, = conn.execute(
        "SELECT count(*) FROM t7_payout_attempts a"
        " WHERE a.run_id = %s AND a.rows_updated > 0"
        "   AND EXISTS (SELECT 1 FROM t7_payout_attempts b"
        "                WHERE b.run_id = a.run_id AND b.payout_key = a.payout_key"
        "                  AND b.rows_updated > 0 AND b.epoch > a.epoch"
        "                  AND b.attempted_at < a.attempted_at)", (run_id,)).fetchone()

    lab_db.section("the numbers")
    print(f"  write attempts             {attempts}")
    print(f"  accepted by the resource   {accepted}")
    print(f"  REJECTED by the resource   {rejected}")
    print(f"  lease epoch at the end     {current_epoch}")
    print(f"  stale-epoch attempts       {stale_attempts}")
    print(f"  ... of which rejected      {stale_rejected}")
    print(f"  DUPLICATE PAYOUTS          {sent_twice}")
    print( "  ^ accepted writes from a STALE epoch: the resource took a write after")
    print( "    a HIGHER epoch had already written that key. Two leaders each")
    print( "    believing they were in charge, and the money moved twice.")
    if stale_attempts == 0:
        print()
        print("  *** BROKEN RUN, not a wrong prediction. ***")
        print("  The stale writer never attempted a write, so nothing was tested.")
        print("  Raise --pause-seconds above --ttl, or lengthen --seconds so that")
        print("  worker-b has time to take over before worker-a resumes.")
        conn.close()
        return 1
    print()
    if args.fencing:
        print("  Fencing on. The stale writer still WOKE UP and still TRIED -- there is")
        print("  no way to stop that, and any design that assumes otherwise is assuming")
        print("  it can bound a pause it does not control. What changed is that the")
        print("  RESOURCE refused, in the WHERE clause, and the stale holder found out")
        print("  from a row count rather than from a belief about itself.")
    else:
        print("  No fencing. The stale writer's UPDATE was accepted because nothing in")
        print("  it mentioned an epoch. Note that the lease worked exactly as designed:")
        print("  it expired on time, worker-b took over correctly, and the payout still")
        print("  went out twice. A lease with a timeout does not prevent two workers")
        print("  acting at once. It only makes it rarer.")
    print()
    print(f"  full breakdown:  psql -d {lab_db.DB_NAME} -f sql/topic7_duplicate_payouts.sql")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
