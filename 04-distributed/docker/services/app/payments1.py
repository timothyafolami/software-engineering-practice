"""Topic 1 -- `payments-api`. The CLIENT, and the side that records belief.

It calls `ledger` THROUGH TOXIPROXY, so the fault is in the network between two
real processes rather than simulated inside one. For every attempt it writes a
t1_client_attempts row classified exactly the way Topic 1 argues you must:

  success    2xx came back
  safe       the request provably never landed  (connect refused / DNS)
  ambiguous  everything else -- timeout, read error, reset after send, EOF

The reconciliation query (sql/topic1_reconcile.sql) diffs those rows against
t1_charges. A charge whose client verdict is not 'success' is an ORPHAN.

The classification is the entire deliverable, so it is written as one function
with the safe branch listed first and narrowly: anything not proven safe is
ambiguous. Getting that default backwards is the bug the topic is about.
"""
import os, httpx
from fastapi import FastAPI
from pydantic import BaseModel
from .db import pool

LEDGER = os.environ.get("LEDGER_URL", "http://toxiproxy:8666")
TIMEOUT = float(os.environ.get("CLIENT_TIMEOUT", "2.0"))
ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
RETRY = os.environ.get("RETRY_POLICY", "any")   # any | safe-only

app = FastAPI()


class Pay(BaseModel):
    request_id: str
    toxic: str


def classify(exc: Exception) -> tuple[str, str]:
    """-> (verdict, error_kind). Safe means PROVABLY never landed."""
    if isinstance(exc, httpx.ConnectError):
        # Refused / DNS failure: no bytes left this host for that socket.
        return "safe", "ConnectError"
    if isinstance(exc, httpx.ConnectTimeout):
        return "safe", "ConnectTimeout"
    if isinstance(exc, httpx.ReadTimeout):
        return "ambiguous", "ReadTimeout"
    if isinstance(exc, httpx.WriteTimeout):
        return "ambiguous", "WriteTimeout"
    if isinstance(exc, httpx.PoolTimeout):
        return "safe", "PoolTimeout"          # never left the client
    if isinstance(exc, httpx.RemoteProtocolError):
        return "ambiguous", "RemoteProtocolError"
    if isinstance(exc, httpx.ReadError):
        return "ambiguous", "ReadError"       # includes RST after the request
    return "ambiguous", type(exc).__name__


def record(rid: str, toxic: str, verdict: str, kind: str | None) -> None:
    with pool("LAB_DSN").connection() as c:
        c.execute("INSERT INTO t1_client_attempts(request_id,toxic,verdict,error_kind)"
                  " VALUES(%s,%s,%s,%s)", (rid, toxic, verdict, kind))


@app.get("/health")
def health():
    return {"ledger": LEDGER, "timeout": TIMEOUT, "attempts": ATTEMPTS,
            "retry_policy": RETRY}


@app.post("/pay")
def pay(p: Pay):
    last = None
    with httpx.Client(timeout=TIMEOUT) as client:
        for _ in range(ATTEMPTS):
            try:
                r = client.post(f"{LEDGER}/charge",
                                json={"request_id": p.request_id,
                                      "toxic": p.toxic, "amount_cents": 100})
                r.raise_for_status()
                record(p.request_id, p.toxic, "success", None)
                return {"status": "success"}
            except Exception as e:                      # noqa: BLE001
                verdict, kind = classify(e)
                record(p.request_id, p.toxic, verdict, kind)
                last = (verdict, kind)
                if RETRY == "safe-only" and verdict != "safe":
                    break                                # do not retry ambiguity
    return {"status": last[0] if last else "ambiguous",
            "error_kind": last[1] if last else None}
