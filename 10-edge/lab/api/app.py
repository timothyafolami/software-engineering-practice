"""
Layer 10 lab - the `api` service. Topics 3 and 5.

What it is
    FastAPI + SQLAlchemy async over Postgres, with the connection pool as
    the subject rather than a detail. POOL_PROFILE selects one of three
    configurations, and the difference between them is the whole of topic
    3's experiment (a):

      default    pool_size=5, max_overflow=10, no deadline, no shedding.
                 The shape you get from copying a config off a blog post.
                 Note the effective c in Little's Law is 5 + 10 = 15, not
                 5 -- the single most common reason a measured knee lands
                 somewhere other than where the arithmetic said.
      sized      pool sized from measured W and target λ, overflow 0 so
                 that c is exactly what you think it is.
      budgeted   sized, plus a per-request deadline propagated into the DB
                 (asyncio.timeout AND a Postgres statement_timeout, so the
                 database stops working on abandoned queries too), plus
                 503 + Retry-After instead of unbounded queueing.

What to look for in /metrics
    api_acquire_seconds     time spent WAITING FOR A POOL SLOT
    api_query_seconds       time the query itself took
    api_request_seconds     total handler time
    api_pool_checkedout     slots currently in use
    api_shed_total          requests rejected with 503 (budgeted only)

    The graph that is the topic: total goes vertical while query stays
    flat, and the entire increase is acquire wait. Three timers, measured
    separately, or you cannot see it.

    One honest caveat about `budgeted`: DEADLINE is a target, not a bound.
    `asyncio.timeout` fires only when the event loop gets round to it, and
    unwinding a cancelled request still costs a round trip to Postgres to
    kill the in-flight query. Below the wall that is invisible; far above
    it the measured handler time runs well past the deadline even though
    every one of those requests is being shed. Read api_request_seconds
    and api_shed_total together, not either alone. Note that SQLAlchemy acquires
    lazily at first QUERY, not at session creation -- a timer around
    session creation measures nothing, which is why the acquire timer here
    wraps `session.connection()` explicitly.

Endpoints
    GET /work?ms=50&dist=fixed        one row, indexed lookup, then a
                                      server-side sleep of a controlled
                                      duration: fixed (c_s = 0) or
                                      exponential (c_s = 1)
    GET /healthz                      profile, pool geometry, derived λ_max
    GET /metrics                      Prometheus
"""

from __future__ import annotations

import asyncio
import os
import time

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import (CONTENT_TYPE_LATEST, Counter, Gauge, Histogram,
                               generate_latest)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://app:app@db:5432/app")
POOL_PROFILE = os.environ.get("POOL_PROFILE", "default")

# Measured mean service time you are sizing against. Change it when you
# measure a different one -- it is an input to the arithmetic, not a
# constant of nature.
TARGET_W_SECONDS = float(os.environ.get("TARGET_W", "0.05"))
TARGET_LAMBDA = float(os.environ.get("TARGET_LAMBDA", "400"))
DEADLINE_SECONDS = float(os.environ.get("DEADLINE", "0.5"))

# c = pool_size + max_overflow. Both halves count, and forgetting the
# second half is how a "20 connection" service turns out to be a 30
# connection service.
PROFILES = {
    "default": {"pool_size": 5, "max_overflow": 10, "deadline": None, "shed": False},
    "sized": {"pool_size": max(1, round(TARGET_LAMBDA * TARGET_W_SECONDS)),
              "max_overflow": 0, "deadline": None, "shed": False},
    "budgeted": {"pool_size": max(1, round(TARGET_LAMBDA * TARGET_W_SECONDS)),
                 "max_overflow": 0, "deadline": DEADLINE_SECONDS, "shed": True},
}
PROFILE = PROFILES[POOL_PROFILE]
POOL_C = PROFILE["pool_size"] + PROFILE["max_overflow"]

BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10)
ACQUIRE = Histogram("api_acquire_seconds", "Time waiting for a pool slot", buckets=BUCKETS)
QUERY = Histogram("api_query_seconds", "Time executing the query", buckets=BUCKETS)
REQUEST = Histogram("api_request_seconds", "Total handler time", buckets=BUCKETS)
CHECKED_OUT = Gauge("api_pool_checkedout", "Pool slots currently in use")
SHED = Counter("api_shed_total", "Requests rejected rather than queued")
FAILED = Counter("api_failed_total", "Requests that errored", ["reason"])

engine = create_async_engine(
    DATABASE_URL,
    pool_size=PROFILE["pool_size"],
    max_overflow=PROFILE["max_overflow"],
    pool_timeout=DEADLINE_SECONDS if PROFILE["shed"] else 30.0,
    pool_pre_ping=False,
)
Session = async_sessionmaker(engine, expire_on_commit=False)

app = FastAPI(title="layer10-api")


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({
        "profile": POOL_PROFILE,
        "pool_size": PROFILE["pool_size"],
        "max_overflow": PROFILE["max_overflow"],
        "effective_c": POOL_C,
        "assumed_W_seconds": TARGET_W_SECONDS,
        # L = λW rearranged: this is the wall, not a guideline.
        "max_lambda_req_per_s": POOL_C / TARGET_W_SECONDS,
        "deadline_seconds": PROFILE["deadline"],
        "sheds_when_full": PROFILE["shed"],
    })


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)


@app.get("/work")
async def work(ms: int = 50, dist: str = "fixed") -> Response:
    """One indexed lookup plus a controlled service time.

    `dist=fixed` gives c_s = 0; `dist=exp` gives c_s = 1 at the same mean.
    Kingman says the queue in front of the second is twice as deep at
    identical utilisation, and that is topic 3's part (c)."""
    started = time.perf_counter()
    fn = "work_exponential" if dist.startswith("exp") else "work_fixed"

    async def handle() -> Response:
        async with Session() as session:
            # Acquire is timed on its own. SQLAlchemy hands out a session
            # object without touching the pool; the checkout happens here.
            acquire_start = time.perf_counter()
            conn = await session.connection()
            ACQUIRE.observe(time.perf_counter() - acquire_start)
            CHECKED_OUT.set(engine.pool.checkedout())

            query_start = time.perf_counter()
            if PROFILE["deadline"] is not None:
                # The database must also stop working on abandoned queries,
                # or the client giving up just moves the waste server-side.
                await conn.execute(
                    text(f"SET LOCAL statement_timeout = "
                         f"{int(PROFILE['deadline'] * 1000)}"))
            row = await conn.execute(
                text("SELECT count(*) FROM items WHERE tenant_id = :t"),
                {"t": 7})
            await conn.execute(text(f"SELECT {fn}(:ms)"), {"ms": ms})
            QUERY.observe(time.perf_counter() - query_start)
            return JSONResponse({"rows": row.scalar(), "dist": dist, "ms": ms})

    try:
        if PROFILE["deadline"] is not None:
            async with asyncio.timeout(PROFILE["deadline"]):
                return await handle()
        return await handle()
    except (asyncio.TimeoutError, TimeoutError):
        SHED.inc()
        # 503 with Retry-After, not a silent 30-second queue. The failure
        # becoming explicit is the improvement; the extra throughput is not.
        return JSONResponse({"error": "overloaded"}, status_code=503,
                            headers={"Retry-After": "1"})
    except Exception as exc:  # pool timeout, driver error, cancelled query
        reason = type(exc).__name__
        FAILED.labels(reason=reason).inc()
        if PROFILE["shed"]:
            SHED.inc()
            return JSONResponse({"error": reason}, status_code=503,
                                headers={"Retry-After": "1"})
        raise
    finally:
        REQUEST.observe(time.perf_counter() - started)
        CHECKED_OUT.set(engine.pool.checkedout())
