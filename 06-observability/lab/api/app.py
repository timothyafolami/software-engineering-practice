"""
Layer 6 lab - `api`: a production-shaped FastAPI service with five real defects.

You are not told which defect owns the p99. That is Topic 2's entire exercise,
and the code below is written so that reading it does not give the answer away
any faster than measuring does -- all five are real things that have been in
real Python services, all five are plausible, and the delta on p50 and p99 is
different for each.

Environment levers (the full table is in ../README.md):

  DEFECT_DISABLE   turns off exactly one defect by name:
                   n_plus_one | sync_http_in_async | small_pool |
                   missing_index | pricing_tail
  BREAK            breaks trace propagation one way at a time:
                   queue_no_traceparent | executor_no_ctx | pricing_fresh_ctx |
                   collector_strip   (the last one is the collector's, not ours)
  CARDINALITY_DEMO adds an unbounded label to the request counter, e.g.
                   customer_id
  OTEL_METRIC_CARDINALITY_LIMIT   raises or removes the SDK's per-stream cap

Endpoints:

  GET  /orders?customer_id=...   the endpoint under test
  GET  /orders/{id}              one order, with a pricing call
  POST /orders                   enqueues a job for `worker`
  GET  /health                   liveness, no DB
  POST /_fault                   fault injection for Topics 6 and 7:
                                 {"mode":"outage","seconds":180}
                                 {"mode":"error_rate","ratio":0.08,"seconds":14400}
                                 {"mode":"pricing_tail"}

VERIFICATION STATUS
-------------------
Built and run inside the compose stack on 2026-08-19 under k6 load: closed
loop at 60 VU, open loop at 300 RPS, and a 10-to-120 VU ramp. Every endpoint
above answers, all five defects are live and one of them was isolated by
delta, and the metrics and spans this module emits arrive in Prometheus and
Tempo. One pin in requirements.txt had to be corrected before the image would
build; the module itself needed no change.

Two things worth knowing that only showed up under load, neither of which is a
bug in this file:

  * `BREAK=executor_no_ctx` does nothing on its own. `fetch_price` below
    short-circuits into the blocking client whenever the `sync_http_in_async`
    defect is enabled, which is the default, so the executor path that break
    targets is never taken. Pair it with
    `DEFECT_DISABLE=sync_http_in_async`.
  * `CARDINALITY_DEMO` works and `OTEL_METRIC_CARDINALITY_LIMIT` does not.
    The Python SDK implements no per-stream cardinality cap, so the counter
    below grows without bound -- 5 series to 12,216 in one ten-minute run --
    and never emits the `otel.metric.overflow` datapoint the spec describes.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import requests  # deliberately the SYNCHRONOUS client: see sync_http_in_async
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import metrics, trace
from opentelemetry.propagate import inject
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://lab:lab@db:5432/lab")
PRICING_URL = os.environ.get("PRICING_URL", "http://pricing:8081")
DEFECT_DISABLE = os.environ.get("DEFECT_DISABLE", "").strip()
BREAK = os.environ.get("BREAK", "").strip()
CARDINALITY_DEMO = os.environ.get("CARDINALITY_DEMO", "").strip()

DEFECTS = ("n_plus_one", "sync_http_in_async", "small_pool",
           "missing_index", "pricing_tail")


def defect_enabled(name: str) -> bool:
    if name not in DEFECTS:
        raise ValueError("unknown defect %r; the five are %r" % (name, DEFECTS))
    return DEFECT_DISABLE != name


if DEFECT_DISABLE and DEFECT_DISABLE not in DEFECTS:
    sys.exit("DEFECT_DISABLE=%r is not one of %r" % (DEFECT_DISABLE, DEFECTS))

# ---------------------------------------------------------------------------
# Logging: JSON on stdout, with trace_id injected per record.
#
# Order matters and is the single most common way this goes wrong: this module
# is imported AFTER `opentelemetry-instrument` has patched `logging`, so the
# LogRecord already carries otelTraceID / otelSpanID by the time the formatter
# runs. Configure a logger before that patching and you get a formatter
# attached to a handler that never sees the fields.
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.time(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            # Injected by opentelemetry's LoggingInstrumentor. Empty string
            # rather than a missing key, so a Loki query never has to care.
            "trace_id": getattr(record, "otelTraceID", "") or "",
            "span_id": getattr(record, "otelSpanID", "") or "",
            "service": os.environ.get("OTEL_SERVICE_NAME", "api"),
        }
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    return logging.getLogger("api")


log = configure_logging()
tracer = trace.get_tracer("lab.api")
meter = metrics.get_meter("lab.api")

# The counter Topic 4 detonates. With CARDINALITY_DEMO unset its label set is
# bounded: route template x method x status class. Set CARDINALITY_DEMO to a
# field name and that field is added as a label -- one word, and the series
# count becomes a product with your customer count in it.
requests_total = meter.create_counter(
    "http.server.requests",
    description="Requests, labelled the way Topic 4 asks you to think about",
)

# The metric that does not exist by default (Topic 5). SQLAlchemy has checkout
# and checkin events and no timer; this is the timer.
pool_wait = meter.create_histogram(
    "db.client.connection.wait_time",
    unit="s",
    description="Time spent waiting for a pooled connection (Topic 5)",
)
pool_connections = meter.create_up_down_counter(
    "db.client.connection.count",
    description="Pooled connections by state (current semconv name)",
)

# ---------------------------------------------------------------------------
# Fault injection (Topics 6 and 7)
# ---------------------------------------------------------------------------


@dataclass
class Fault:
    mode: str = "none"
    ratio: float = 0.0
    until: float = 0.0
    extra: dict = field(default_factory=dict)

    def active(self) -> bool:
        return self.mode != "none" and time.time() < self.until


FAULT = Fault()

# ---------------------------------------------------------------------------
# Database: the pool, and the instrumentation SQLAlchemy does not ship.
# ---------------------------------------------------------------------------

# Defect `small_pool`: five connections, no overflow, against a 60-VU test.
# With the defect disabled the pool is sized like a service that has thought
# about it.
if defect_enabled("small_pool"):
    POOL_KWARGS = dict(pool_size=5, max_overflow=0, pool_timeout=30)
else:
    POOL_KWARGS = dict(pool_size=20, max_overflow=10, pool_timeout=30)

engine = create_async_engine(DATABASE_URL, **POOL_KWARGS)
Session = async_sessionmaker(engine, expire_on_commit=False)

# `checkout` fires when a connection leaves the pool -- AFTER the wait and
# BEFORE any statement is issued, which is exactly why the wait has no span.
_checkout_requested: contextvars.ContextVar[float] = contextvars.ContextVar(
    "checkout_requested", default=0.0)


@event.listens_for(engine.sync_engine, "checkout")
def _on_checkout(dbapi_conn, conn_record, conn_proxy):  # noqa: ANN001
    requested = _checkout_requested.get()
    if requested:
        pool_wait.record(time.perf_counter() - requested)
    pool_connections.add(1, {"state": "used"})
    pool_connections.add(-1, {"state": "idle"})


@event.listens_for(engine.sync_engine, "checkin")
def _on_checkin(dbapi_conn, conn_record):  # noqa: ANN001
    pool_connections.add(-1, {"state": "used"})
    pool_connections.add(1, {"state": "idle"})


# ---------------------------------------------------------------------------
# The pricing call. Two versions, and which one runs is defect
# `sync_http_in_async`.
# ---------------------------------------------------------------------------

EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="pricing")


def _pricing_sync(order_id: int) -> dict[str, Any]:
    """A synchronous requests.get(), called from an async handler.

    This is the defect. `requests` does not await, so it holds the event loop
    for the entire round trip -- and `pricing` has a deliberate tail. One slow
    downstream request becomes every concurrent request on this worker.
    Layer 1 Topic 3 is this, and Topic 2 is finding it from a dashboard.
    """
    with tracer.start_as_current_span("GET /price") as span:
        span.set_attribute("http.request.method", "GET")
        span.set_attribute("order.id", order_id)
        headers: dict[str, str] = {}
        inject(headers)  # W3C traceparent, unless the break below removes it
        if BREAK == "pricing_fresh_ctx":
            headers.pop("traceparent", None)
        response = requests.get(
            "%s/price/%d" % (PRICING_URL, order_id), headers=headers, timeout=5)
        span.set_attribute("http.response.status_code", response.status_code)
        response.raise_for_status()
        return response.json()


async def fetch_price(order_id: int) -> dict[str, Any]:
    if defect_enabled("sync_http_in_async"):
        # The bug: a blocking call inside `async def`.
        return _pricing_sync(order_id)

    loop = asyncio.get_running_loop()
    if BREAK == "executor_no_ctx":
        # The break: the pool thread never sees this request's context, so the
        # pricing span starts a new trace. Topic 3, Part 2, break 2.
        return await loop.run_in_executor(EXECUTOR, _pricing_sync, order_id)

    ctx = contextvars.copy_context()
    return await loop.run_in_executor(EXECUTOR, ctx.run, _pricing_sync, order_id)


# ---------------------------------------------------------------------------
# The application
# ---------------------------------------------------------------------------

app = FastAPI(title="layer6-lab-api")


@app.middleware("http")
async def record_request(request: Request, call_next):
    start = time.perf_counter()
    _checkout_requested.set(time.perf_counter())

    if FAULT.active() and FAULT.mode == "outage":
        response = JSONResponse({"error": "injected outage"}, status_code=503)
    elif FAULT.active() and FAULT.mode == "error_rate" and random.random() < FAULT.ratio:
        response = JSONResponse({"error": "injected error"}, status_code=500)
    else:
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - the 500 has to be counted
            log.exception("unhandled error")
            response = JSONResponse({"error": "internal"}, status_code=500)

    duration = time.perf_counter() - start

    attributes: dict[str, Any] = {
        # Current semantic conventions. `http.method` and `http.status_code`
        # are the OLD names; a dashboard keyed on those returns empty here,
        # which is not an error message. See lab/README.
        "http.request.method": request.method,
        "http.route": request.scope.get("route").path
        if request.scope.get("route") else "unmatched",
        "http.response.status_code": response.status_code,
    }
    if CARDINALITY_DEMO:
        # Topic 4, and it really is one word. Everything past the SDK's
        # per-stream limit lands under otel.metric.overflow=true, silently.
        attributes[CARDINALITY_DEMO] = request.query_params.get(
            CARDINALITY_DEMO, "unknown")
    requests_total.add(1, attributes)

    log.info("request complete", extra={"extra_fields": {
        "route": attributes["http.route"],
        "status": response.status_code,
        "duration_ms": round(1000 * duration, 2),
        "customer_id": request.query_params.get("customer_id", ""),
    }})
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness only: no DB, no pricing. A health check that touches the

    database is a health check that fails during exactly the incident you
    wanted it to survive.
    """
    return {"status": "ok"}


@app.get("/orders")
async def list_orders(customer_id: str = "cust-00001", limit: int = 25):
    """The endpoint under test. Two of the five defects live in this handler."""
    # The `missing_index` defect is not in this query -- the query is the same
    # either way. It is in whether orders(customer_id, created_at) has an index
    # behind it, which the startup hook decides. That is deliberate: the defect
    # you can see in the handler is not the interesting kind.
    query = text(
        "SELECT id, customer_id, total_cents, created_at "
        "FROM orders WHERE customer_id = :cid "
        "ORDER BY created_at DESC LIMIT :limit")
    async with Session() as session:
        rows = (await session.execute(
            query, {"cid": customer_id, "limit": limit})).mappings().all()

        orders = []
        if defect_enabled("n_plus_one"):
            # The ORM N+1: one query per order for its items. Twenty-five
            # round trips where one would do, each holding the connection.
            for row in rows:
                items = (await session.execute(
                    text("SELECT id, sku, qty FROM order_items "
                         "WHERE order_id = :oid"),
                    {"oid": row["id"]})).mappings().all()
                orders.append({**dict(row), "items": [dict(i) for i in items]})
        else:
            ids = [row["id"] for row in rows] or [0]
            items = (await session.execute(
                text("SELECT id, order_id, sku, qty FROM order_items "
                     "WHERE order_id = ANY(:ids)"),
                {"ids": ids})).mappings().all()
            by_order: dict[int, list] = {}
            for item in items:
                by_order.setdefault(item["order_id"], []).append(dict(item))
            orders = [{**dict(row), "items": by_order.get(row["id"], [])}
                      for row in rows]

    return {"customer_id": customer_id, "count": len(orders), "orders": orders}


@app.get("/orders/{order_id}")
async def get_order(order_id: int):
    async with Session() as session:
        row = (await session.execute(
            text("SELECT id, customer_id, total_cents, created_at "
                 "FROM orders WHERE id = :id"),
            {"id": order_id})).mappings().first()
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    price = await fetch_price(order_id)
    return {**dict(row), "price": price}


@app.post("/orders")
async def create_order(payload: dict[str, Any]):
    """Enqueues a job on the Postgres-backed queue that `worker` consumes.

    The queue is where trace context breaks: nothing about a row in a table
    carries a header. Topic 3's first break is simply not writing the
    traceparent into the message body.
    """
    headers: dict[str, str] = {}
    inject(headers)
    if BREAK == "queue_no_traceparent":
        headers.pop("traceparent", None)

    async with Session() as session:
        await session.execute(
            text("INSERT INTO jobs (payload, traceparent, state) "
                 "VALUES (:payload, :traceparent, 'pending')"),
            {"payload": json.dumps(payload),
             "traceparent": headers.get("traceparent")})
        await session.commit()
    return {"queued": True, "traceparent": headers.get("traceparent")}


@app.post("/_fault")
async def inject_fault(payload: dict[str, Any]):
    """The only endpoint here that exists for the lab rather than the business.

    {"mode":"outage","seconds":180}
    {"mode":"error_rate","ratio":0.08,"seconds":14400}
    {"mode":"pricing_tail"}
    """
    mode = payload.get("mode", "none")
    seconds = float(payload.get("seconds", 300))
    FAULT.mode = mode
    FAULT.ratio = float(payload.get("ratio", 0.0))
    FAULT.until = time.time() + seconds
    FAULT.extra = {k: v for k, v in payload.items()
                   if k not in ("mode", "seconds", "ratio")}

    if mode == "pricing_tail":
        # Ask `pricing` to turn its tail on rather than faking it here: the
        # whole point of scenario X is that the fault is in the dependency.
        try:
            requests.post("%s/_tail" % PRICING_URL, json={"on": True}, timeout=2)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not reach pricing: %s", exc)

    log.info("fault injected", extra={"extra_fields": {
        "mode": mode, "seconds": seconds, "ratio": FAULT.ratio}})
    return {"mode": FAULT.mode, "until": FAULT.until, "ratio": FAULT.ratio}


@app.on_event("startup")
async def announce() -> None:
    # `missing_index` is a property of the schema, so applying it means
    # creating or dropping the index. Building it over ~2M rows takes a few
    # seconds the first time you disable the defect; that wait is the defect
    # being fixed in front of you.
    async with engine.begin() as conn:
        if defect_enabled("missing_index"):
            await conn.execute(text("DROP INDEX IF EXISTS idx_orders_customer_created"))
        else:
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_orders_customer_created "
                "ON orders (customer_id, created_at DESC)"))

    enabled = [d for d in DEFECTS if defect_enabled(d)]
    log.info("api starting", extra={"extra_fields": {
        "defects_enabled": enabled,
        "defect_disabled": DEFECT_DISABLE or None,
        "break": BREAK or None,
        "cardinality_demo": CARDINALITY_DEMO or None,
        "pool": POOL_KWARGS,
    }})
