"""
Layer 4 Topic 6 -- the outbox relay: LISTEN/NOTIFY *and* polling, and why both.

WHAT THIS DEMONSTRATES: the relay from `hwm_skip.py`'s fixed half, built properly.
`SELECT ... WHERE published_at IS NULL ORDER BY id FOR UPDATE SKIP LOCKED LIMIT n`,
publish, mark published -- woken instantly by LISTEN/NOTIFY, with the polling
loop still running underneath as a safety net.

WHY BOTH, which is the entire point of this file:

  * A relay that ONLY polls has your poll interval as its floor on latency. That
    is a fixed tax on every event, paid whether or not anything happened.
  * A relay that ONLY listens is correct until its first reconnect. NOTIFY is
    NOT durable: if no session is listening at the moment of the NOTIFY, the
    message is simply gone. Nothing is queued, nothing is retried, and the row
    sits unpublished until something else notices -- and in a listen-only relay
    nothing else ever does.

  Belt and braces is the correct answer here, and knowing WHY both are needed is
  more useful than either mechanism on its own.

WHAT TO LOOK FOR IN THE OUTPUT: the `woken by` breakdown. Rows published on a
NOTIFY wake have low charge-to-publish latency; rows published on a poll tick are
the ones NOTIFY lost, and their latency is bounded below by the poll interval.
Run with `--no-listen` to see the floor, and with `--drop-notifications` to
simulate a listener that was not connected and watch the poll loop catch them.

  python3 python/outbox_relay.py --seconds 30
  python3 python/outbox_relay.py --seconds 30 --no-listen
  python3 python/outbox_relay.py --seconds 30 --drop-notifications
  psql -d sep_lab_04_dist -f sql/topic6_reconcile.sql

It relays whatever `hwm_skip.py` (or the compose `payments-api`) has written, so
run one of those first or in parallel -- this program never writes a charge. Use
`--relays 0` on the writer, otherwise the writer's own SKIP LOCKED relay
publishes every row before this one sees it and you get a run with nothing to do:

    python3 python/hwm_skip.py --writers 3 --hold-seconds 1 --duration 30 --relays 0 &
    python3 python/outbox_relay.py --seconds 30
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import threading
import time

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"),
)
import lab_db  # noqa: E402  - after the sys.path fix, on purpose

CHANNEL = "t6_outbox_new"

# The trigger is what makes NOTIFY interesting: it fires on COMMIT, so a listener
# is woken exactly when the row becomes visible -- not when it was inserted.
# pg_notify's payload is capped (8000 bytes) and it is a WAKE-UP, not a message
# bus: the relay still reads the row from the table. Putting the event body in
# the payload would be a second, undurable copy of data you already have.
TRIGGER_DDL = """
CREATE OR REPLACE FUNCTION t6_notify_outbox() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('t6_outbox_new', NEW.run_id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS t6_outbox_notify ON t6_outbox;
CREATE TRIGGER t6_outbox_notify
    AFTER INSERT ON t6_outbox
    FOR EACH ROW EXECUTE FUNCTION t6_notify_outbox();
"""


def publish_batch(conn, name: str, batch: int, woken_by: str,
                  stats: dict[str, int]) -> int:
    """One drain pass. Returns how many rows it published."""
    published = 0
    with conn.transaction():
        rows = conn.execute(
            "SELECT id, run_id FROM t6_outbox WHERE published_at IS NULL"
            " ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s", (batch,)
        ).fetchall()
        # SKIP LOCKED is what lets you run more than one of these. Each relay
        # grabs a disjoint batch instead of blocking on the other's locks --
        # without it, a second relay is not extra throughput, it is a queue.
        for oid, run_id in rows:
            # relay and woken_by are separate columns on purpose. Folding the
            # wake reason into the relay name would split one relay into two in
            # every per-relay query, and "outbox-relay skipped 58 rows" would be
            # an artefact of the label rather than a fact about the design.
            conn.execute(
                "INSERT INTO t6_delivered (run_id, relay, woken_by, outbox_id)"
                " VALUES (%s, 'outbox-relay', %s, %s)", (run_id, woken_by, oid)
            )
            conn.execute(
                "UPDATE t6_outbox SET published_at = clock_timestamp(),"
                " published_by = %s WHERE id = %s", (name, oid)
            )
            published += 1
    stats[woken_by] = stats.get(woken_by, 0) + published
    return published


def listener(name: str, batch: int, stop: threading.Event, stats: dict[str, int],
             drop_rate: float) -> None:
    """LISTEN on one connection, publish on another. The split is not tidiness.

    psycopg3's `notifies()` is a generator that owns the connection while it is
    being iterated; running a query on that same connection from inside the loop
    leaves the generator wedged and notifications stop arriving. The symptom is a
    listener that receives exactly one notification and then goes quiet forever,
    which reads as "NOTIFY does not work here" and sends you to debug the
    trigger. Drain the generator, leave it, then publish.

    And note the second reason the loop is shaped this way: `notifies(timeout=t)`
    ENDS at the timeout rather than yielding None and continuing, so it has to be
    re-entered. A `for note in conn.notifies(timeout=0.2)` that is not itself
    inside a loop is a listener with a 200ms lifetime.
    """
    with lab_db.connect(autocommit=True) as lconn, \
         lab_db.connect(autocommit=True) as wconn:
        lconn.execute(f"LISTEN {CHANNEL}")
        while not stop.is_set():
            notes = list(lconn.notifies(timeout=0.2))
            if not notes:
                continue
            if random.random() < drop_rate:
                # Stands in for "no session was listening at that moment". NOTIFY
                # is not durable and there is no redelivery: the wake-up is gone,
                # and only the poll loop will ever find these rows.
                stats["notifications_dropped"] = (
                    stats.get("notifications_dropped", 0) + len(notes))
                continue
            stats["notify_wakeups"] = stats.get("notify_wakeups", 0) + 1
            publish_batch(wconn, name, batch, "notify", stats)
        lconn.execute(f"UNLISTEN {CHANNEL}")


def poller(name: str, batch: int, interval: float, stop: threading.Event,
           stats: dict[str, int]) -> None:
    with lab_db.connect(autocommit=True) as conn:
        while not stop.is_set():
            stats["poll_wakeups"] = stats.get("poll_wakeups", 0) + 1
            publish_batch(conn, name, batch, "poll", stats)
            stop.wait(interval)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--name", default="relay-a")
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--poll-interval", type=float, default=5.0,
                    help="the floor on charge-to-event latency for anything "
                         "NOTIFY did not deliver")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--no-listen", action="store_true",
                    help="polling only -- watch the latency floor appear")
    ap.add_argument("--drop-notifications", action="store_true",
                    help="drop 100%% of notifications, as a disconnected listener "
                         "would; the poll loop must still deliver everything")
    args = ap.parse_args(argv)

    conn = lab_db.open_lab()
    exists = conn.execute(
        "SELECT to_regclass('t6_outbox') IS NOT NULL").fetchone()[0]
    if not exists:
        print("t6_outbox does not exist yet. Run the writer first:\n"
              "  python3 python/hwm_skip.py --writers 3 --hold-seconds 2 --duration 30")
        return 1
    conn.execute(TRIGGER_DDL)

    lab_db.banner(f"Topic 6 -- outbox relay ({args.name})")
    print(f"  server        : {lab_db.describe_server(conn)}")
    print(f"  wake-ups      : {'polling only' if args.no_listen else 'LISTEN/NOTIFY + polling'}")
    print(f"  poll interval : {args.poll_interval:.1f}s   batch {args.batch}")
    if args.drop_notifications:
        print( "  notifications : DROPPED (simulating a listener that was not connected)")
    print(f"  backlog now   : "
          f"{conn.execute('SELECT count(*) FROM t6_outbox WHERE published_at IS NULL').fetchone()[0]}"
          " unpublished rows")

    stop = threading.Event()
    stats: dict[str, int] = {}
    threads = [threading.Thread(target=poller, daemon=True,
                                args=(args.name, args.batch, args.poll_interval,
                                      stop, stats))]
    if not args.no_listen:
        threads.append(threading.Thread(
            target=listener, daemon=True,
            args=(args.name, args.batch, stop, stats,
                  1.0 if args.drop_notifications else 0.0)))
    for t in threads:
        t.start()
    time.sleep(args.seconds)
    stop.set()
    for t in threads:
        t.join(timeout=3)

    lab_db.section("what woke the relay, and what each wake-up claimed")
    total = stats.get("notify", 0) + stats.get("poll", 0)
    print(f"  {'':<26}{'wake-ups':>10}{'rows published':>17}")
    print(f"  {'NOTIFY':<26}{stats.get('notify_wakeups', 0):>10}{stats.get('notify', 0):>17}")
    print(f"  {'poll tick':<26}{stats.get('poll_wakeups', 0):>10}{stats.get('poll', 0):>17}")
    print(f"  {'notifications dropped':<26}{stats.get('notifications_dropped', 0):>10}")
    print(f"  {'total published':<26}{'':>10}{total:>17}")
    remaining = conn.execute(
        "SELECT count(*) FROM t6_outbox WHERE published_at IS NULL").fetchone()[0]
    print(f"  {'still unpublished':<26}{'':>10}{remaining:>17}")
    print()
    print("  Read the two columns together. A row is credited to whichever path")
    print("  actually claimed it, so a short --poll-interval wins races it did not")
    print("  need to run: the poller wakes anyway and the batch is already there.")
    print("  That is not NOTIFY failing, it is NOTIFY being redundant at that")
    print("  interval -- and the interval is what is costing you the queries.")
    print()
    if args.no_listen:
        print("  Polling only. Every row waited for a tick, so charge-to-event p50")
        print(f"  cannot be below roughly half the {args.poll_interval:.1f}s interval. That is")
        print("  physics, not tuning -- and it is why NOTIFY is worth wiring up.")
    elif args.drop_notifications:
        print("  Every notification was thrown away, exactly as a NOTIFY issued while")
        print("  no session was listening would be. Nothing was queued and nothing was")
        print("  retried. The poll loop delivered everything anyway, which is the")
        print("  entire argument for keeping it: a listen-only relay would still be")
        print("  sitting on this backlog, reporting itself healthy.")
    else:
        print("  Rows claimed on a NOTIFY wake-up were published within a round trip")
        print("  of the commit. Rows claimed on a poll tick had their latency bounded")
        print("  below by the poll interval, whatever the broker was doing.")
    print()
    print(f"  latency breakdown:  psql -d {lab_db.DB_NAME} -f sql/topic6_reconcile.sql")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
