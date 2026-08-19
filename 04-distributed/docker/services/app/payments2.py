"""Topic 2 -- `payments-api`, the HTTP version of the three implementations.

Same schema and the same three handlers as python/idempotency_race.py; the
difference is that the concurrency comes from k6 firing the SAME key from
several VUs at once through a real socket, rather than from threads in one
process. IMPL selects the handler.

  A  check-then-insert. SELECT, and if absent INSERT and charge. Three
     statements on an AUTOCOMMIT connection, so three separate transactions,
     and the charge is written BEFORE the key row -- otherwise the unique index
     would rescue it and this would be B wearing A's name.
  B  atomic insert. INSERT ... ON CONFLICT DO NOTHING, fingerprint check,
     stored-response replay, effect in the SAME transaction.
  C  pg_advisory_xact_lock(hashtext(key)) then check-and-act.

`charges` deliberately has NO unique index on the key. The unique index IS the
idempotency, and hiding it inside the table would make A look correct.
"""
import os, json, hashlib, time
from fastapi import FastAPI, Response
from pydantic import BaseModel
from .db import pool

IMPL = os.environ.get("IMPL", "B")
RUN_ID = os.environ.get("RUN_ID", "compose")
HOLD_MS = int(os.environ.get("HOLD_MS", "0"))

app = FastAPI()

DDL = """
CREATE TABLE IF NOT EXISTS idempotency_keys (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id text NOT NULL,
    key text NOT NULL,
    fingerprint text NOT NULL,
    state text NOT NULL CHECK (state IN ('in_flight','succeeded','failed_permanently')),
    response jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '24 hours',
    UNIQUE (tenant_id, key)
);
CREATE TABLE IF NOT EXISTS charges (
    id bigserial PRIMARY KEY,
    run_id text NOT NULL,
    impl text NOT NULL,
    tenant_id text NOT NULL,
    idempotency_key text NOT NULL,
    amount_cents integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
    -- NO UNIQUE (tenant_id, idempotency_key). On purpose.
);
CREATE INDEX IF NOT EXISTS charges_run_idx ON charges (run_id);
"""


class Pay(BaseModel):
    tenant_id: str = "t1"
    key: str
    amount_cents: int = 100


def fingerprint(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"POST /payments {canonical}".encode()).hexdigest()


def _charge(cur, p: Pay) -> None:
    if HOLD_MS:
        time.sleep(HOLD_MS / 1000)
    cur.execute("INSERT INTO charges(run_id,impl,tenant_id,idempotency_key,amount_cents)"
                " VALUES(%s,%s,%s,%s,%s)",
                (RUN_ID, IMPL, p.tenant_id, p.key, p.amount_cents))


@app.on_event("startup")
def _startup() -> None:
    with pool("LAB_DSN").connection() as c:
        c.execute(DDL)


@app.get("/health")
def health():
    return {"impl": IMPL, "run_id": RUN_ID, "hold_ms": HOLD_MS}


@app.post("/payments")
def payments(p: Pay, response: Response):
    fp = fingerprint(p.model_dump())

    if IMPL == "A":
        # autocommit == three separate transactions. This is the bug, on purpose.
        with pool("LAB_DSN").connection() as c:      # pool opens autocommit
            seen = c.execute("SELECT 1 FROM idempotency_keys WHERE tenant_id=%s AND key=%s",
                             (p.tenant_id, p.key)).fetchone()
            if seen:
                return {"status": "replayed"}
            _charge(c, p)                            # effect BEFORE the key row
            try:
                c.execute("INSERT INTO idempotency_keys(tenant_id,key,fingerprint,state)"
                          " VALUES(%s,%s,%s,'succeeded')", (p.tenant_id, p.key, fp))
            except Exception:
                response.status_code = 500
                return {"status": "charged then failed to record the key"}
            return {"status": "charged"}

    with pool("LAB_DSN").connection() as c:
        c.autocommit = False
        try:
            if IMPL == "C":
                c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (p.key,))
                row = c.execute("SELECT state,fingerprint,response FROM idempotency_keys"
                                " WHERE tenant_id=%s AND key=%s",
                                (p.tenant_id, p.key)).fetchone()
                if row:
                    if row[1] != fp:
                        response.status_code = 422
                        c.rollback(); return {"status": "fingerprint mismatch"}
                    c.rollback(); return {"status": "replayed"}
                c.execute("INSERT INTO idempotency_keys(tenant_id,key,fingerprint,state)"
                          " VALUES(%s,%s,%s,'succeeded')", (p.tenant_id, p.key, fp))
                _charge(c, p)
                c.commit(); return {"status": "charged"}

            # IMPL B
            row = c.execute(
                "INSERT INTO idempotency_keys(tenant_id,key,fingerprint,state)"
                " VALUES(%s,%s,%s,'in_flight') ON CONFLICT (tenant_id,key) DO NOTHING"
                " RETURNING id", (p.tenant_id, p.key, fp)).fetchone()
            if row is None:
                existing = c.execute("SELECT state,fingerprint FROM idempotency_keys"
                                     " WHERE tenant_id=%s AND key=%s",
                                     (p.tenant_id, p.key)).fetchone()
                c.rollback()
                if existing and existing[1] != fp:
                    response.status_code = 422
                    return {"status": "fingerprint mismatch"}
                return {"status": "replayed" if existing and existing[0] == "succeeded"
                        else "in flight"}
            _charge(c, p)                            # SAME transaction as the key row
            c.execute("UPDATE idempotency_keys SET state='succeeded'"
                      " WHERE tenant_id=%s AND key=%s", (p.tenant_id, p.key))
            c.commit(); return {"status": "charged"}
        except Exception as e:                       # noqa: BLE001
            c.rollback()
            response.status_code = 500
            return {"status": "error", "detail": type(e).__name__}
        finally:
            c.autocommit = True
