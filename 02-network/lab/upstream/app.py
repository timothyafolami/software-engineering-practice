"""
Layer 2 lab - the dependency that gets slow.

Tunable three ways, in increasing order of specificity:
  container-wide  DELAY_MS, FAIL_PCT, BODY_BYTES environment variables
  per request     ?delay_ms= &fail_pct= &bytes=
  externally      a Toxiproxy toxic in front of it, which is how topics 1, 3
                  and 6 do it -- injecting the fault in the network rather
                  than in the server is the whole reason toxi is in the stack

/whoami reports NAME and the container's own address, which is how Topic 5
tells upstream_a from upstream_b after the alias moves.
"""
import asyncio
import os
import socket
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

NAME = os.environ.get("NAME", "upstream")
DELAY_MS = int(os.environ.get("DELAY_MS", "0"))
FAIL_PCT = float(os.environ.get("FAIL_PCT", "0"))
BODY_BYTES = int(os.environ.get("BODY_BYTES", "1024"))

app = FastAPI()
STARTED = time.monotonic()
SERVED = {"n": 0}


@app.get("/work")
async def work(delay_ms: int | None = None, fail_pct: float | None = None, bytes: int | None = None):
    SERVED["n"] += 1
    d = DELAY_MS if delay_ms is None else delay_ms
    f = FAIL_PCT if fail_pct is None else fail_pct
    n = BODY_BYTES if bytes is None else bytes
    if d:
        await asyncio.sleep(d / 1000.0)
    if f and (SERVED["n"] % 100) < f:
        return JSONResponse({"error": "injected"}, status_code=503)
    # Deterministic filler. Topic 6 needs bodies around 100 KB before
    # multiplexing has anything to multiplex.
    return PlainTextResponse("x" * n)


@app.get("/whoami")
async def whoami():
    return {
        "name": NAME,
        "hostname": socket.gethostname(),
        "addrs": sorted({ai[4][0] for ai in socket.getaddrinfo(socket.gethostname(), None)}),
        "served": SERVED["n"],
        "uptime_s": round(time.monotonic() - STARTED, 1),
    }
