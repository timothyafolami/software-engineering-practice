"""Topic 6 -- the outbox relay (SKIP LOCKED) plus a consumer, in one process.

The relay claims unpublished rows with FOR UPDATE SKIP LOCKED, publishes, and
marks them. When the broker is down it publishes nothing and marks nothing --
the rows stay in the outbox, which is the entire argument for the design.

The consumer records one t6_delivered row per message it receives, so the
deliverable can count what actually arrived on the far side of a real broker.
"""
import os, json, time, threading
from .db import pool
from .kafka import producer, consumer, TOPIC

EFFECTS_DDL = """
CREATE TABLE IF NOT EXISTS t6_consumer_effects (
    id bigserial PRIMARY KEY, run_id text NOT NULL, consumer text NOT NULL,
    outbox_id bigint NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id, outbox_id));
"""

RUN_ID = os.environ.get("RUN_ID", "compose")
NAME = os.environ.get("RELAY_NAME", "relay")
BATCH = int(os.environ.get("BATCH", "50"))


def relay_loop() -> None:
    with pool("LAB_DSN").connection() as c:
        c.execute(EFFECTS_DDL)
    p = producer()
    while True:
        try:
            with pool("LAB_DSN").connection() as c:
                c.autocommit = False
                rows = c.execute(
                    "SELECT id,payload FROM t6_outbox WHERE run_id=%s AND published_at IS NULL"
                    " ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s",
                    (RUN_ID, BATCH)).fetchall()
                if not rows:
                    c.rollback(); c.autocommit = True; time.sleep(0.2); continue
                # A row is marked published ONLY when its delivery report came
                # back without an error. flush() draining the queue is not that:
                # a message that timed out permanently also leaves the queue, so
                # keying off flush()'s return marks lost events as delivered --
                # an outbox relay that loses events silently, which is worse
                # than the dual write it replaces.
                acked: list[int] = []

                def _dr(err, _msg, oid=None):
                    if err is None and oid is not None:
                        acked.append(oid)

                sent = 0
                for oid, payload in rows:
                    try:
                        # The OUTBOX ROW ID has to travel in the payload.
                        # t6_delivered.outbox_id means the outbox row, and the
                        # deliverable joins on it; recording the charge id there
                        # instead makes query 2 report 687 "permanently skipped"
                        # rows that were all delivered, and query 5 emit
                        # NEGATIVE latencies from comparing two different rows.
                        msg = dict(payload); msg["outbox_id"] = oid
                        p.produce(TOPIC, json.dumps(msg),
                                  callback=lambda e, m, o=oid: _dr(e, m, o))
                        sent += 1
                    except Exception:                    # noqa: BLE001
                        break
                p.flush(5.0)
                published = acked
                if len(published) < sent:
                    # broker unreachable or partial: commit only what was acked,
                    # leave the rest claimable. Never mark an unacked row.
                    if not published:
                        c.rollback(); c.autocommit = True; time.sleep(1.0); continue
                c.execute("UPDATE t6_outbox SET published_at=clock_timestamp(),"
                          " published_by=%s WHERE id = ANY(%s)", (NAME, published))
                c.commit(); c.autocommit = True
        except Exception:                                # noqa: BLE001
            time.sleep(1.0)


def consume_loop() -> None:
    c = consumer(f"t6-{RUN_ID}")
    while True:
        try:
            m = c.poll(1.0)
            if m is None or m.error():
                continue
            d = json.loads(m.value())
            oid = d.get("outbox_id", 0)
            with pool("LAB_DSN").connection() as conn:
                conn.execute(
                    "INSERT INTO t6_delivered(run_id,relay,woken_by,outbox_id)"
                    " VALUES(%s,%s,'kafka',%s)",
                    (d.get("run_id", RUN_ID), NAME, oid))
                # INSERT-THEN-ACT, never check-then-act (Topic 2, at the other
                # end of the pipeline). The unique index is the idempotency; a
                # duplicate delivery loses the race and does no effect.
                n = conn.execute(
                    "INSERT INTO t6_consumer_effects(run_id,consumer,outbox_id)"
                    " VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                    (d.get("run_id", RUN_ID), NAME, oid)).rowcount
                if n:
                    pass   # the effect would happen here
        except Exception:                                # noqa: BLE001
            time.sleep(1.0)


if __name__ == "__main__":
    threading.Thread(target=consume_loop, daemon=True).start()
    relay_loop()
