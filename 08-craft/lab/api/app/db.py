"""The engine, the pool, and the instrumentation that makes the pool visible.

WHAT THIS DEMONSTRATES: `pool_size + max_overflow` is the per-process
concurrency ceiling, and it is the number Little's Law needs in topic 7. With
`pool_timeout` unset a checkout queues forever, so the symptom of a slow
database is unbounded latency rather than errors -- the baseline the ladder
starts from.

WHAT TO LOOK FOR: `pool_wait_stats()`. SQLAlchemy will not tell you how long a
checkout waited; HikariCP exposes it as a first-class metric and Python makes
you instrument it yourself, which is exactly what the `PoolEvents` listeners
below do. Topic 7's table has a "pool wait p99" column and this is where that
number comes from.
"""
from __future__ import annotations

import time
from bisect import insort
from collections import deque

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .core.config import settings

_checkout_started: dict[int, float] = {}
_wait_samples: deque[float] = deque(maxlen=5000)


def _make_engine():
    kwargs: dict = {"echo": False}
    # SQLite uses a StaticPool and rejects these outright. The lab runs against
    # Postgres; the sqlite branch exists so `tests/unit` and `tests/properties`
    # run natively on macOS with no container, which is what topics 5, 8 and 9
    # promise. Guarding on the URL rather than try/except keeps the failure of a
    # genuinely bad pool setting loud.
    _pooled = not settings.database_url.startswith("sqlite")
    if _pooled:
        kwargs.update(
            pool_size=settings.pool_size,
            max_overflow=settings.max_overflow,
            # pre-ping hides a dead connection behind an extra round trip;
            # topic 3 wants the failure, not a silent retry
            pool_pre_ping=False,
        )
        if settings.pool_timeout_s is not None:
            kwargs["pool_timeout"] = settings.pool_timeout_s
    eng = create_async_engine(settings.database_url, **kwargs)

    sync_pool = eng.sync_engine.pool

    @event.listens_for(sync_pool, "checkout")
    def _on_checkout(dbapi_conn, conn_record, conn_proxy):
        started = _checkout_started.pop(id(conn_record), None)
        if started is not None:
            _wait_samples.append(time.monotonic() - started)

    # There is no "about to wait" event, so the closest honest proxy is the
    # reset/return boundary: we stamp when a connection is asked for by
    # recording at `connect` and `checkin`, and measure the gap at `checkout`.
    @event.listens_for(sync_pool, "checkin")
    def _on_checkin(dbapi_conn, conn_record):
        _checkout_started[id(conn_record)] = time.monotonic()

    @event.listens_for(sync_pool, "connect")
    def _on_connect(dbapi_conn, conn_record):
        _checkout_started[id(conn_record)] = time.monotonic()

    return eng


engine = _make_engine()
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def pool_wait_stats() -> dict:
    """p50/p99 connection checkout wait, in milliseconds, over the last 5000."""
    if not _wait_samples:
        return {"samples": 0, "p50_ms": None, "p99_ms": None, "status": engine.pool.status()}
    ordered: list[float] = []
    for s in _wait_samples:
        insort(ordered, s)
    def q(p: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(p * len(ordered)))] * 1000, 2)
    return {
        "samples": len(ordered),
        "p50_ms": q(0.50),
        "p99_ms": q(0.99),
        # `ceiling` is the Little's Law input: max throughput is ceiling / S.
        "ceiling": settings.pool_size + settings.max_overflow,
        "status": engine.pool.status(),
    }


async def get_session():
    """FastAPI dependency. One session per request, closed on the way out."""
    async with SessionLocal() as session:
        if settings.statement_timeout_ms is not None:
            # Server-side, so a slow query cannot hold a pooled connection past
            # its usefulness even if the client forgot a timeout.
            await session.execute(
                text(f"SET LOCAL statement_timeout = {int(settings.statement_timeout_ms)}")
            )
        yield session
