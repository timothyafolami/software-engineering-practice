"""Topic 6 -- `payments-api`. POST /payments must charge AND emit an event.

MODE=v0  DUAL WRITE. Commit the charge, then publish. Stop the broker mid-load
         and you get charges with no event -- and there is no ordering of these
         two writes that is safe, which is the point.
MODE=v1  OUTBOX. Charge and outbox row in ONE transaction. Publishing is the
         relay's problem, and the broker being down is a backlog rather than a
         lost event.
"""
import os, json
from fastapi import FastAPI, Response
from .db import pool
from .kafka import producer, TOPIC

MODE = os.environ.get("MODE", "v1")
RUN_ID = os.environ.get("RUN_ID", "compose")

app = FastAPI()
_p = None

DDL = """
CREATE TABLE IF NOT EXISTS t6_charges (
    id bigserial PRIMARY KEY, run_id text NOT NULL, amount_cents integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS t6_outbox (
    id bigserial PRIMARY KEY, run_id text NOT NULL, aggregate_id bigint NOT NULL,
    topic text NOT NULL, payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    committed_at timestamptz, published_at timestamptz, published_by text);
CREATE INDEX IF NOT EXISTS outbox_unpublished_idx ON t6_outbox (id) WHERE published_at IS NULL;
CREATE TABLE IF NOT EXISTS t6_delivered (
    id bigserial PRIMARY KEY, run_id text NOT NULL, relay text NOT NULL,
    woken_by text, outbox_id bigint NOT NULL,
    delivered_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE INDEX IF NOT EXISTS t6_delivered_run_idx ON t6_delivered (run_id, relay);
CREATE TABLE IF NOT EXISTS t6_publish_failures (
    id bigserial PRIMARY KEY, run_id text NOT NULL, mode text NOT NULL,
    charge_id bigint NOT NULL, reason text NOT NULL,
    at timestamptz NOT NULL DEFAULT clock_timestamp());
"""


@app.on_event("startup")
def _startup():
    global _p
    with pool("LAB_DSN").connection() as c:
        c.execute(DDL)
    _p = producer()


@app.get("/health")
def health():
    return {"mode": MODE, "run_id": RUN_ID}


@app.post("/payments")
def payments(response: Response):
    with pool("LAB_DSN").connection() as c:
        if MODE == "v0":
            # --- DUAL WRITE ------------------------------------------------
            cid = c.execute("INSERT INTO t6_charges(run_id,amount_cents)"
                            " VALUES(%s,100) RETURNING id", (RUN_ID,)).fetchone()[0]
            # committed (autocommit). Now publish, outside any transaction.
            # flush() returns the number of messages STILL QUEUED, and a
            # message that failed permanently (message.timeout.ms with
            # retries=0) has already LEFT the queue. So `flush() == 0` means
            # "queue drained", not "broker acked" -- treating them as the same
            # silently under-reports every lost event, which is the exact
            # failure this topic is about. Only the delivery callback knows.
            acked = {"ok": False, "err": None}

            def _dr(err, _msg):
                acked["ok"] = err is None
                acked["err"] = str(err) if err else None

            try:
                _p.produce(TOPIC, json.dumps({"charge_id": cid, "run_id": RUN_ID}),
                           callback=_dr)
                _p.flush(5.0)
                if not acked["ok"]:
                    raise RuntimeError(acked["err"] or "no delivery report")
            except Exception as e:                       # noqa: BLE001
                c.execute("INSERT INTO t6_publish_failures(run_id,mode,charge_id,reason)"
                          " VALUES(%s,'v0',%s,%s)", (RUN_ID, cid, type(e).__name__))
                response.status_code = 500
                return {"status": "charged, event lost", "charge_id": cid}
            return {"status": "charged and published", "charge_id": cid}

        # --- OUTBOX -------------------------------------------------------
        c.autocommit = False
        try:
            cid = c.execute("INSERT INTO t6_charges(run_id,amount_cents)"
                            " VALUES(%s,100) RETURNING id", (RUN_ID,)).fetchone()[0]
            c.execute("INSERT INTO t6_outbox(run_id,aggregate_id,topic,payload,committed_at)"
                      " VALUES(%s,%s,%s,%s::jsonb,clock_timestamp())",
                      (RUN_ID, cid, TOPIC, json.dumps({"charge_id": cid, "run_id": RUN_ID})))
            c.commit()
        finally:
            c.autocommit = True
        return {"status": "charged and enqueued", "charge_id": cid}
