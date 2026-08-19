"""
Layer 5 lab - the system under test. One image, one process, role from ROLE.

WHAT THIS DEMONSTRATES
  Everything in this layer needs a real network boundary, a real connection
  pool and a real load generator that does not wait for you. This is the
  service on the other end of that. It is deliberately ONE image with a
  runtime-mutable config, because every experiment here is of the form
  "change exactly one thing and rerun", and a rebuild between runs makes
  that claim impossible to trust.

WHAT TO LOOK FOR IN THE OUTPUT
  Not this file's output - k6's. But every response carries the headers the
  scripts turn into CSV columns, and they are the whole measurement:

    X-Inflight        requests in the handler right now (topic 1's L)
    X-Pool-Wait-Ms    time this request spent waiting for a pool slot
    X-Pool-In-Use     connections checked out of the pool
    X-Service-Ms      time the handler actually spent working
    X-Remaining-Ms    budget left when the handler started (topic 2)
    X-Zombie          1 if this completed after its caller's deadline
    X-Shed            1 if the admission controller rejected it (topic 5)
    X-Cache           hit|miss (topic 4)
    X-Hedged          1 if a second copy was issued (topic 6)

ROLES
  app         standalone service under test (topics 1, 4, 5, 7)
  gateway     hop 1 of the chain (topics 2, 3), or the fan-out root (topic 6)
  service_b   hop 2
  service_c   hop 3 - the one holding a real Postgres connection
  backend     a fan-out leaf (topic 6): latency from a distribution, no DB

ENDPOINTS
  GET  /healthz            liveness, no dependencies touched
  GET  /work               hold a pooled connection for SERVICE_MS
  GET  /checkout           /work at tier 0   (topic 5)
  GET  /search             /work at tier 3   (topic 5)
  GET  /report             the slow neighbour that shares the pool (topic 5)
  GET  /chain              this hop's part of the three-hop chain (topics 2, 3)
  GET  /cached             Redis, then Postgres on a miss (topic 4)
  GET  /fanout?k=          fan out to k backends and wait for all (topic 6)
  GET  /backend            one fan-out leaf's service time (topic 6)
  POST /charge             idempotent payment (topic 7)
  GET  /admin/config       current values of every variable
  POST /admin/config       apply new ones, live
  POST /admin/fault        inject local errors/latency/response loss
  GET  /admin/counters     monotonic counters + gauges, for the k6 poller
  GET  /admin/zombies      topic 2's numbers, per hop
  POST /admin/reset        zero the counters and truncate topic 7's tables
  GET  /metrics            Prometheus
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from . import cache, deadline as dl, fanout as fo, idempotency
from .config import config
from .db import do_work, engine, init_schema, pool_in_use, pool_total, report_engine, wait_for_db
from .faults import faults
from .metrics import (
    CONTENT_TYPE_LATEST, DOWNSTREAM_SECONDS, EVENTS, INFLIGHT, POOL_IN_USE,
    POOL_TOTAL, REQUEST_SECONDS, counters, generate_latest,
)
from .retry import budget, with_retries
from .shed import shedder

_client: httpx.AsyncClient | None = None
_started = time.time()


def http() -> httpx.AsyncClient:
    """One connection pool for outbound calls, so hops reuse connections.

    A fresh client per request would put a TCP handshake in front of every
    measurement and make the chain look slower than it is. `limits` is set
    high on purpose: the bound in this lab must be the DATABASE pool, not an
    accidental HTTP one, or topic 1 measures the wrong count.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=1000, max_keepalive_connections=1000),
            timeout=httpx.Timeout(30.0),
        )
    return _client


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    role = config.get("ROLE")
    POOL_TOTAL.labels(role).set(pool_total())
    if role != "backend":
        ok = await wait_for_db(timeout_s=90.0)
        if ok:
            with contextlib.suppress(Exception):
                await init_schema()
    yield
    if _client is not None:
        await _client.aclose()


app = FastAPI(title="layer5-failure-lab", lifespan=lifespan)


# ----------------------------------------------------------------- plumbing

class Outcome:
    """One handler's result, plus everything the measurement needs."""

    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status = status
        self.payload = payload
        self.headers: dict[str, str] = {}


async def run_handler(request: Request, endpoint: str, tier: int, work):
    """Admission control, deadline check, fault injection, timing, headers.

    Every measured endpoint goes through here so the columns mean the same
    thing everywhere. The ORDER matters and is the topic 5 lesson in three
    lines: shed BEFORE doing work, check the deadline BEFORE taking a slot,
    and count a rejection as a rejection rather than as a failure.
    """
    counters.inc("received")
    started = time.perf_counter()
    role = config.get("ROLE")
    deadline = dl.read_deadline(request.headers)
    if deadline is None and role == "gateway":
        # The gateway is where the budget starts, so it is where the absolute
        # deadline is minted - in BOTH variants. PROPAGATE_DEADLINE decides
        # whether the hops behind it act on the number, not whether they are
        # told it; otherwise the naive variant's zombie count would have to be
        # estimated rather than measured.
        deadline = dl.now_ms() + float(config.get("CLIENT_TIMEOUT_MS"))

    if dl.should_reject(deadline):
        counters.inc("deadline_rejected")
        EVENTS.labels(role, "deadline_rejected").inc()
        left = dl.remaining_ms(deadline)
        return _respond(504, {"error": "deadline_exceeded_before_start",
                              "remaining_ms": round(left or 0.0, 1)},
                        {"X-Remaining-Ms": f"{left or 0.0:.1f}"}, endpoint, started, "rejected")

    admitted, queue_wait_ms = await shedder.acquire(tier)
    if not admitted:
        EVENTS.labels(role, "shed").inc()
        return _respond(503, {"error": "shed", "tier": tier,
                              "queue_wait_ms": round(queue_wait_ms, 2)},
                        {"X-Shed": "1", "Retry-After": "1",
                         "X-Queue-Wait-Ms": f"{queue_wait_ms:.2f}"},
                        endpoint, started, "shed")

    # Sample L WHILE the request is in flight. Sampling it in _respond would
    # read the gauge after this request had already been released, which
    # under-reports by one on every single sample - and topic 1 checks
    # L against lambda x W, so a systematic bias there is a wrong finding
    # rather than noise.
    entry_inflight = shedder.inflight
    INFLIGHT.labels(role).set(entry_inflight)
    try:
        if faults.should_error():
            counters.inc("failed")
            return _respond(503, {"error": "injected_fault"}, {"X-Fault": "1"},
                            endpoint, started, "fault")
        await faults.delay()
        outcome: Outcome = await work(deadline)
    except Exception as exc:  # never leak a traceback into the measurement
        counters.inc("failed")
        return _respond(500, {"error": type(exc).__name__, "detail": str(exc)[:200]},
                        {}, endpoint, started, "error")
    finally:
        elapsed = time.perf_counter() - started
        await shedder.release(rtt_s=elapsed)
        INFLIGHT.labels(role).set(shedder.inflight)
        POOL_IN_USE.labels(role).set(max(0, pool_in_use()))

    # Only a request that actually completed its work can be a zombie; a 5xx
    # here is the deadline machinery refusing to do the work, not late work.
    zombie = dl.record_completion(deadline, did_work=outcome.status < 400)
    if outcome.status < 400:
        counters.inc("completed")
    elif outcome.status >= 500:
        counters.inc("failed")
    # 4xx is neither. A 409 or a 422 is a correct answer to a wrong request:
    # counting it as a failure would make topic 7's own assertions look like
    # an outage, and counting it as goodput would be worse.

    headers = dict(outcome.headers)
    headers.setdefault("X-Inflight", str(entry_inflight))
    headers["X-Queue-Wait-Ms"] = f"{queue_wait_ms:.2f}"
    headers["X-Zombie"] = "1" if zombie else "0"
    if deadline is not None:
        headers["X-Remaining-Ms"] = f"{dl.remaining_ms(deadline):.1f}"

    if faults.should_drop():
        # The work happened. The answer does not arrive. This is the case
        # topic 7 exists for, and the client cannot tell it from "nothing
        # happened" without an idempotency key.
        EVENTS.labels(role, "response_dropped").inc()
        return _respond(599, {"error": "response_dropped_after_work"},
                        headers, endpoint, started, "dropped")

    return _respond(outcome.status, outcome.payload, headers, endpoint, started,
                    "ok" if 200 <= outcome.status < 300 else "fail")


def _respond(status: int, payload: dict, headers: dict, endpoint: str,
             started: float, outcome: str) -> JSONResponse:
    elapsed = time.perf_counter() - started
    role = config.get("ROLE")
    REQUEST_SECONDS.labels(role, endpoint, outcome).observe(elapsed)
    counters.gauge("inflight", shedder.inflight)
    counters.gauge("pool_in_use", max(0, pool_in_use()))
    counters.gauge("pool_total", pool_total())
    counters.gauge("shed_limit", shedder.limit() or 0)
    counters.gauge("retry_tokens", budget.level())
    base = {
        "X-Inflight": str(shedder.inflight),
        "X-Pool-In-Use": str(pool_in_use()),
        "X-Pool-Total": str(pool_total()),
        "X-Service-Ms": f"{elapsed * 1000.0:.2f}",
        "X-Role": role,
    }
    base.update(headers)
    return JSONResponse(payload, status_code=status, headers=base)


async def call_next_hop(url: str, timeout_ms: float, headers: dict[str, str]) -> dict:
    """One outbound attempt. Returns a dict; never raises for a normal failure."""
    role = config.get("ROLE")
    t0 = time.perf_counter()
    try:
        resp = await http().get(url, timeout=timeout_ms / 1000.0, headers=headers)
        DOWNSTREAM_SECONDS.labels(role, "ok").observe(time.perf_counter() - t0)
        return {"status": resp.status_code, "body": _safe_json(resp),
                "elapsed_ms": (time.perf_counter() - t0) * 1000.0}
    except httpx.TimeoutException:
        counters.inc("timeouts")
        DOWNSTREAM_SECONDS.labels(role, "timeout").observe(time.perf_counter() - t0)
        return {"status": 504, "body": {"error": "downstream_timeout"},
                "elapsed_ms": (time.perf_counter() - t0) * 1000.0}
    except Exception as exc:
        DOWNSTREAM_SECONDS.labels(role, "error").observe(time.perf_counter() - t0)
        return {"status": 502, "body": {"error": type(exc).__name__},
                "elapsed_ms": (time.perf_counter() - t0) * 1000.0}


def _safe_json(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
        return body if isinstance(body, dict) else {"body": body}
    except Exception:
        return {"body": resp.text[:200]}


def _retryable(result: dict) -> bool:
    """5xx and timeouts are retryable; 4xx is not.

    Retrying a 4xx is the mistake that turns a client bug into an outage:
    the answer will not change, and you have multiplied the load anyway.
    Topic 3's variant D depends on non-retryable errors propagating UPWARD
    rather than each hop rediscovering them.
    """
    return int(result.get("status", 500)) >= 500


# ------------------------------------------------------------------ handlers

@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "role": config.get("ROLE"),
            "uptime_s": round(time.time() - _started, 1)}


@app.get("/work")
async def work(request: Request, tier: int = 0, ms: int | None = None):
    async def do(deadline):
        service_ms = ms if ms is not None else int(config.get("SERVICE_MS"))
        res = await do_work(service_ms, dl.statement_timeout_ms(deadline),
                            deadline_ms=deadline)
        status = 200 if res["error"] is None else 503
        out = Outcome(status, {"ok": res["error"] is None, "error": res["error"],
                               "query_ms": round(res["query_ms"], 2)})
        out.headers["X-Pool-Wait-Ms"] = f"{res['wait_ms']:.2f}"
        out.headers["X-Pool-In-Use"] = str(res.get("in_use", -1))
        return out
    return await run_handler(request, "work", tier, do)


@app.get("/checkout")
async def checkout(request: Request):
    """Tier 0. Under priority shedding this is the traffic that must survive."""
    return await work(request, tier=0)


@app.get("/search")
async def search(request: Request):
    """Tier 3. Under priority shedding this is what absorbs the rejections."""
    return await work(request, tier=3)


@app.get("/report")
async def report(request: Request):
    """The slow neighbour. With BULKHEAD=0 it drowns /checkout; with 1 it cannot."""
    async def do(deadline):
        eng = await report_engine()
        res = await do_work(int(config.get("REPORT_SERVICE_MS")),
                            dl.statement_timeout_ms(deadline), eng=eng,
                            deadline_ms=deadline)
        out = Outcome(200 if res["error"] is None else 503,
                      {"ok": res["error"] is None, "error": res["error"]})
        out.headers["X-Pool-Wait-Ms"] = f"{res['wait_ms']:.2f}"
        out.headers["X-Bulkhead"] = "1" if config.get("BULKHEAD") else "0"
        return out
    return await run_handler(request, "report", 2, do)


@app.get("/chain")
async def chain(request: Request):
    """This hop's part of the three-hop chain.

    A hop with a DOWNSTREAM_URL calls it, with retries, under whatever
    deadline policy is configured. A hop without one is the leaf and does
    the database work. Same handler either way, so the only difference
    between gateway, service-b and service-c is configuration.
    """
    async def do(deadline):
        downstream = config.get("DOWNSTREAM_URL")
        if not downstream:
            res = await do_work(int(config.get("SERVICE_MS")),
                                dl.statement_timeout_ms(deadline),
                                deadline_ms=deadline)
            status = 200 if res["error"] is None else 504
            out = Outcome(status, {"leaf": True, "error": res["error"],
                                   "query_ms": round(res["query_ms"], 2)})
            out.headers["X-Pool-Wait-Ms"] = f"{res['wait_ms']:.2f}"
            return out

        headers = dl.headers_for_next_hop(deadline, request.headers)

        async def attempt(n: int, timeout_ms: float) -> dict:
            return await call_next_hop(downstream, timeout_ms, headers)

        result = await with_retries(attempt, is_retryable=_retryable,
                                    deadline_ms=deadline)
        status = int(result["status"])
        out = Outcome(status, {"hop": config.get("ROLE"), "downstream_status": status,
                               "downstream_ms": round(result["elapsed_ms"], 2)})
        out.headers["X-Downstream-Ms"] = f"{result['elapsed_ms']:.2f}"
        return out

    return await run_handler(request, "chain", 0, do)


@app.get("/cached")
async def cached(request: Request, key: int | None = None):
    """Redis first, Postgres on a miss. Topic 4's amplification mechanism.

    The miss path is the expensive one, and it is guarded by exactly the
    retry policy topic 3 warned about. FLUSHALL makes every request take
    the miss path at once; the retries make each miss cost more than one
    database call; and the database being slower makes more requests time
    out and retry. That loop is the sustaining effect.
    """
    async def do(deadline):
        n = key if key is not None else int(time.time() * 1000) % int(config.get("CACHE_KEYS"))
        ck = f"lab:item:{n}"
        hit = await cache.get(ck)
        if hit is not None:
            out = Outcome(200, {"key": ck, "cached": True})
            out.headers["X-Cache"] = "hit"
            return out

        async def attempt(attempt_n: int, timeout_ms: float) -> dict:
            # The per-attempt timeout is the whole amplification mechanism and
            # it has to be APPLIED, not just passed in. Without it the only
            # bound on a miss is POOL_TIMEOUT_S (30s by default), so under a
            # miss storm requests queue quietly for half a minute, nothing
            # ever times out, nothing retries, and topic 4's sustaining loop
            # - timeout -> retry -> more misses - never starts. wait_for
            # cancels the checkout, which is what returns the waiter's place
            # in the pool queue and lets the retry be a NEW arrival.
            try:
                res = await asyncio.wait_for(
                    do_work(int(config.get("SERVICE_MS")),
                            dl.statement_timeout_ms(deadline),
                            deadline_ms=deadline),
                    timeout=max(0.001, timeout_ms / 1000.0))
            except (asyncio.TimeoutError, TimeoutError):
                counters.inc("timeouts")
                return {"status": 504,
                        "res": {"error": "client_timeout", "wait_ms": timeout_ms,
                                "query_ms": 0.0, "in_use": pool_in_use()}}
            return {"status": 200 if res["error"] is None else 503, "res": res}

        result = await with_retries(attempt, is_retryable=_retryable, deadline_ms=deadline)
        if result["status"] == 200:
            await cache.setex(ck, cache.now_value(ck))
        out = Outcome(result["status"], {"key": ck, "cached": False,
                                         "error": result["res"]["error"]})
        out.headers["X-Cache"] = "miss"
        out.headers["X-Pool-Wait-Ms"] = f"{result['res']['wait_ms']:.2f}"
        return out

    return await run_handler(request, "cached", 0, do)


@app.get("/backend")
async def backend(request: Request):
    """One fan-out leaf. Service time from LATENCY_DIST; no database at all."""
    async def do(deadline):
        seconds = fo.service_seconds()
        await asyncio.sleep(seconds)
        out = Outcome(200, {"backend_ms": round(seconds * 1000.0, 2)})
        out.headers["X-Backend-Ms"] = f"{seconds * 1000.0:.2f}"
        return out
    return await run_handler(request, "backend", 0, do)


@app.get("/fanout")
async def fanout(request: Request, k: int = 1):
    """Call k backends, wait for ALL of them, and report the slowest.

    Waiting for all is the point: the request is as slow as its slowest
    dependency, so a 1% backend tail becomes a 1 - 0.99^k request tail.
    """
    async def do(deadline):
        addresses = fo.backend_addresses(max(1, k))
        timeout_ms = dl.outbound_timeout_ms(deadline)
        hedged_any = False

        async def call(address: str, timeout: float) -> dict:
            t0 = time.perf_counter()
            res = await call_next_hop(f"http://{address}/backend", timeout,
                                      dl.headers_for_next_hop(deadline, request.headers))
            fo.backend_latency.observe(time.perf_counter() - t0)
            return res

        results = await asyncio.gather(
            *[fo.hedged_call(call, address, float(timeout_ms)) for address in addresses],
            return_exceptions=True,
        )
        slowest = 0.0
        failed = 0
        for item in results:
            if isinstance(item, BaseException):
                failed += 1
                continue
            res, hedged = item
            hedged_any = hedged_any or hedged
            slowest = max(slowest, float(res.get("elapsed_ms", 0.0)))
            if int(res.get("status", 500)) >= 400:
                failed += 1
        out = Outcome(200 if failed == 0 else 503,
                      {"k": len(addresses), "failed": failed,
                       "slowest_backend_ms": round(slowest, 2)})
        out.headers["X-Fanout-K"] = str(len(addresses))
        out.headers["X-Slowest-Backend-Ms"] = f"{slowest:.2f}"
        out.headers["X-Hedged"] = "1" if hedged_any else "0"
        return out

    return await run_handler(request, "fanout", 0, do)


@app.post("/charge")
async def charge(request: Request):
    """Idempotent payment. The key is the Idempotency-Key header (topic 7)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    key = request.headers.get("Idempotency-Key") or str(body.get("key", "")) or "no-key"

    async def do(deadline):
        status, payload = await idempotency.charge(key, body if isinstance(body, dict) else {})
        out = Outcome(status, payload)
        out.headers["X-Idempotency-Mode"] = config.get("IDEMPOTENCY_MODE")
        out.headers["X-Idempotency-Key"] = key
        return out

    return await run_handler(request, "charge", 0, do)


# -------------------------------------------------------------------- admin

@app.get("/admin/config")
async def get_config() -> dict:
    snap = config.snapshot()
    snap["_pool_total"] = pool_total()
    snap["_lambda_max_rps"] = round(pool_total() / max(0.001, config.get("SERVICE_MS") / 1000.0), 1)
    return snap


@app.post("/admin/config")
async def post_config(patch: dict) -> JSONResponse:
    """Apply new values live. Pool changes rebuild the engine before returning."""
    try:
        applied, unknown = config.apply(patch)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if any(k in {"POOL_SIZE", "MAX_OVERFLOW", "POOL_TIMEOUT_S", "DATABASE_URL",
                 "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE"}
           for k in applied):
        with contextlib.suppress(Exception):
            await engine()   # rebuilds under the new key
    POOL_TOTAL.labels(config.get("ROLE")).set(pool_total())
    return JSONResponse({"applied": applied, "unknown": unknown,
                         "config": config.snapshot()})


@app.post("/admin/fault")
async def post_fault(patch: dict) -> dict:
    return {"fault": faults.apply(patch)}


@app.get("/admin/counters")
async def get_counters() -> dict:
    snap = counters.snapshot()
    snap["inflight"] = shedder.inflight
    snap["pool_in_use"] = pool_in_use()
    snap["pool_total"] = pool_total()
    snap["shed"] = snap.get("shed", 0)
    snap["role"] = config.get("ROLE")
    snap["cache_hit_rate_pct"] = round(cache.hit_rate(), 2)
    snap["retry_tokens"] = budget.level()
    snap["hedge_tokens"] = fo.hedge_budget.level()
    snap.update({f"shedder_{k}": v for k, v in shedder.snapshot().items()})
    return snap


@app.get("/admin/zombies")
async def get_zombies() -> dict:
    """Topic 2's numbers for this hop, in the shape tools/zombie_report.py prints."""
    snap = counters.snapshot()
    return {
        "role": config.get("ROLE"),
        "propagate_deadline": bool(config.get("PROPAGATE_DEADLINE")),
        "deadline_slack_ms": config.get("DEADLINE_SLACK_MS"),
        "client_timeout_ms": config.get("CLIENT_TIMEOUT_MS"),
        "service_ms": config.get("SERVICE_MS"),
        "received": snap["received"],
        "completed": snap["completed"],
        "failed": snap["failed"],
        "zombies": snap["zombies"],
        "deadline_rejected": snap["deadline_rejected"],
        "deadline_abandoned": snap["deadline_abandoned"],
        "timeouts": snap["timeouts"],
        "retries": snap["retries"],
        "pool_in_use": pool_in_use(),
        "pool_total": pool_total(),
        "uptime_s": snap["uptime_s"],
    }


@app.post("/admin/reset")
async def post_reset(payload: dict | None = None) -> dict:
    """Zero the counters; optionally truncate topic 7's tables."""
    global _started
    counters.reset()
    _started = time.time()
    out: dict[str, Any] = {"counters": "reset"}
    if payload and payload.get("tables"):
        with contextlib.suppress(Exception):
            out["tables"] = await idempotency.reset()
    return out


@app.get("/admin/report")
async def admin_report() -> dict:
    """Topic 7's assertions, computed in Postgres rather than in the load test."""
    return await idempotency.report()


@app.get("/metrics")
async def metrics() -> Response:
    role = config.get("ROLE")
    INFLIGHT.labels(role).set(shedder.inflight)
    POOL_IN_USE.labels(role).set(max(0, pool_in_use()))
    POOL_TOTAL.labels(role).set(pool_total())
    return PlainTextResponse(generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)
