"""The lab API. One app, every topic's surface mounted on it.

WHAT THIS DEMONSTRATES: topic 1's two shapes live in the same process on
different prefixes so they can be compared without a second deployment; topic 2's
two arms likewise; topic 3's taxonomy is translated to HTTP in exactly one place
(`install_error_handlers`); topic 7's fix kit wraps every request in one
middleware whose behaviour is entirely env-driven.

WHAT TO LOOK FOR: `uvicorn app.main:app` prints the active configuration at
startup. Copy that line into the record beside whatever you measured.

    cd 08-craft/lab/api && uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from .core.config import settings
from .core.errors import Shed, install_error_handlers
from .core.resilience import LatencyBreaker, RetryBudget, deadline
from .deep import router as deep_router
from .routers.customers import router as customers_router
from .routers.orders_crud import router as orders_router
from .routers.orders_duplicated import router as dup_router
from .routers.orders_shared import router as shared_router
from .shallow import router as shallow_router

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("craft.api")

@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("craft-lab config: %s", settings.describe())
    log.info(
        "pool ceiling (pool_size + max_overflow) = %d -- this is Little's Law's "
        "P; topic 7's predicted knee is P / S",
        settings.pool_size + settings.max_overflow,
    )
    yield


app = FastAPI(
    lifespan=lifespan,
    title="craft-lab",
    version="1.0.0",
    description="Layer 8 shared lab. Orders and customers, five endpoints.",
)

retry_budget = RetryBudget(pct=settings.retry_budget_pct)
breaker = LatencyBreaker(settings.breaker_latency_ms)

install_error_handlers(app)
app.include_router(customers_router)
app.include_router(orders_router)
app.include_router(shallow_router)
app.include_router(deep_router)
app.include_router(dup_router)
app.include_router(shared_router)


@app.middleware("http")
async def apply_request_budget(request: Request, call_next):
    """Topic 7's deadline and breaker, applied in one place.

    Both are no-ops unless REQUEST_DEADLINE_MS / BREAKER_LATENCY_MS are set, so
    ladder A measures the unprotected baseline and ladders B-E each add exactly
    one thing. Keeping the fixes here rather than in the handlers is what makes
    "one fix per run" mean one environment variable.
    """
    if request.url.path in ("/healthz", "/_pool", "/_config"):
        return await call_next(request)

    budget_s = None if settings.request_deadline_ms is None else settings.request_deadline_ms / 1000
    try:
        async with deadline(budget_s):
            async with breaker.guard():
                return await call_next(request)
    except Shed:
        raise


@app.get("/_stats", include_in_schema=False)
async def stats() -> dict:
    """Retry-budget and breaker counters. Topic 7 asks you to verify both."""
    return {"retry": retry_budget.stats(), "breaker": breaker.stats()}
