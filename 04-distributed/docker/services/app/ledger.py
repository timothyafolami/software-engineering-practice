"""Topic 1 -- the `ledger` service. Server-side truth.

Every accepted charge is committed to t1_charges BEFORE anything can go wrong
with the reply. That ordering is the experiment: four of Topic 1's six faults
commit and then fail to tell the caller.

CRASH_AFTER_COMMIT=1 makes that explicit -- commit, then os._exit(1) without
writing a byte of response. Ambiguity becomes 100% and no timeout tuning fixes
it. The container is restarted by compose (restart: unless-stopped), which is
what a crash-looping pod does too.
"""
import os
from fastapi import FastAPI
from pydantic import BaseModel
from .db import pool

CRASH_AFTER_COMMIT = os.environ.get("CRASH_AFTER_COMMIT", "0") == "1"

app = FastAPI()

DDL = """
CREATE TABLE IF NOT EXISTS t1_charges (
    id bigserial PRIMARY KEY,
    request_id text NOT NULL,
    toxic text NOT NULL,
    amount_cents bigint NOT NULL,
    committed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS t1_client_attempts (
    id bigserial PRIMARY KEY,
    request_id text NOT NULL,
    toxic text NOT NULL,
    verdict text NOT NULL,
    error_kind text,
    observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS t1_charges_request_idx ON t1_charges (toxic, request_id);
CREATE INDEX IF NOT EXISTS t1_attempts_request_idx ON t1_client_attempts (toxic, request_id);
"""


class Charge(BaseModel):
    request_id: str
    toxic: str
    amount_cents: int = 100


@app.on_event("startup")
def _startup() -> None:
    with pool("LAB_DSN").connection() as c:
        c.execute(DDL)


@app.get("/health")
def health():
    return {"crash_after_commit": CRASH_AFTER_COMMIT}


@app.post("/charge")
def charge(c: Charge):
    with pool("LAB_DSN").connection() as conn:
        conn.execute(
            "INSERT INTO t1_charges(request_id,toxic,amount_cents) VALUES(%s,%s,%s)",
            (c.request_id, c.toxic, c.amount_cents))
    if CRASH_AFTER_COMMIT:
        os._exit(1)          # committed; the caller will never hear about it
    return {"ok": True}
