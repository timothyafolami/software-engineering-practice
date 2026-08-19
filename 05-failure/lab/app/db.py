"""
Layer 5 lab - the real pool, and the work that holds a slot in it.

WHAT THIS DEMONSTRATES
  The bound this whole layer is about is a COUNT, and here it is:
  pool_size + max_overflow slots, held for SERVICE_MS each. Little's Law
  says that fixes throughput at (pool_size + max_overflow) / SERVICE_MS
  before you benchmark anything, and pool_timeout says how long the
  request after that one waits before it runs a query at all.

WHAT TO LOOK FOR
  `wait_ms` returned by do_work() is checkout wait measured on both ends -
  the clock starts before engine.connect() and stops when a connection is
  in hand. At rho=0.2 it is ~0. If it is not ~0 at rho=0.2 you were never
  at 20%, and topic 1's "what would mean the experiment is broken" section
  says so.

  The work itself is pg_sleep() plus a real query inside the SAME
  connection, not asyncio.sleep(). asyncio.sleep() measures the event loop
  and leaves the pool untouched, which is the single most common way to
  build this experiment and measure nothing.

STATEMENT TIMEOUT
  When STATEMENT_TIMEOUT_MS is set (topic 2 derives it per-request from the
  remaining budget) it is emitted as SET LOCAL inside the transaction, so
  it applies to this statement and unwinds with it. Postgres then cancels
  the query itself: the point of topic 2 is that a caller giving up does
  not stop the database, and this is the only thing that does.
"""
from __future__ import annotations

import asyncio
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from . import deadline as dl
from .config import config
from .metrics import POOL_IN_USE, POOL_TOTAL, POOL_WAIT_SECONDS, counters

_engine: AsyncEngine | None = None
_report_engine: AsyncEngine | None = None
_engine_key: tuple | None = None
_lock = asyncio.Lock()


def dsn() -> str:
    """asyncpg DSN, from DATABASE_URL if set or assembled from PG* parts."""
    if config.get("DATABASE_URL"):
        url = config.get("DATABASE_URL")
        # Accept the psql-style URL people paste, and drive it with asyncpg.
        return url.replace("postgresql://", "postgresql+asyncpg://", 1) \
                  .replace("postgres://", "postgresql+asyncpg://", 1)
    return (
        f"postgresql+asyncpg://{config.get('PGUSER')}:{config.get('PGPASSWORD')}"
        f"@{config.get('PGHOST')}:{config.get('PGPORT')}/{config.get('PGDATABASE')}"
    )


def _key() -> tuple:
    return (dsn(), config.get("POOL_SIZE"), config.get("MAX_OVERFLOW"),
            config.get("POOL_TIMEOUT_S"))


def pool_total() -> int:
    return int(config.get("POOL_SIZE")) + int(config.get("MAX_OVERFLOW"))


async def engine() -> AsyncEngine:
    """The engine for the current pool configuration, rebuilt when it changes.

    POST /admin/config {"POOL_SIZE": 10} has to produce a genuinely
    different pool, otherwise "change only pool_size and rerun" is a lie.
    The old engine is disposed after the swap, so connections in flight
    finish rather than being yanked.
    """
    global _engine, _engine_key
    key = _key()
    if _engine is not None and key == _engine_key:
        return _engine
    async with _lock:
        if _engine is not None and _key() == _engine_key:
            return _engine
        old = _engine
        new = create_async_engine(
            dsn(),
            pool_size=int(config.get("POOL_SIZE")),
            max_overflow=int(config.get("MAX_OVERFLOW")),
            pool_timeout=float(config.get("POOL_TIMEOUT_S")),
            pool_pre_ping=False,     # a ping per checkout would add latency we did not ask for
            future=True,
        )
        _engine, _engine_key = new, _key()
        POOL_TOTAL.labels(config.get("ROLE")).set(pool_total())
        if old is not None:
            asyncio.create_task(_dispose(old))
        return new


async def _dispose(old: AsyncEngine) -> None:
    try:
        await old.dispose()
    except Exception:  # pragma: no cover - best effort
        pass


async def report_engine() -> AsyncEngine:
    """Topic 5's bulkhead: /report gets its own small pool, or shares the main one.

    BULKHEAD=0 -> /report competes for the same slots as /checkout, which is
    the failure. BULKHEAD=1 -> REPORT_POOL_SIZE connections of its own, and
    /checkout survives a slow report. Same code path either way, so the
    comparison is a config change and nothing else.
    """
    global _report_engine
    if not config.get("BULKHEAD"):
        return await engine()
    if _report_engine is None:
        async with _lock:
            if _report_engine is None:
                _report_engine = create_async_engine(
                    dsn(),
                    pool_size=int(config.get("REPORT_POOL_SIZE")),
                    max_overflow=0,
                    pool_timeout=float(config.get("POOL_TIMEOUT_S")),
                    future=True,
                )
    return _report_engine


def pool_in_use() -> int:
    """Connections currently checked out. -1 before the first connection."""
    if _engine is None:
        return -1
    try:
        return int(_engine.pool.checkedout())  # type: ignore[union-attr]
    except Exception:
        return -1


async def do_work(service_ms: int, statement_timeout_ms: int | None = None,
                  eng: AsyncEngine | None = None,
                  deadline_ms: float | None = None) -> dict:
    """Hold one pooled connection for service_ms, doing real database work.

    Returns {"wait_ms": checkout wait, "query_ms": time inside the
    connection, "in_use": connections checked out WHILE this one was held,
    "error": str|None}. Never raises for a query-level failure -
    the caller decides what status that becomes, because topic 2 needs a
    statement_timeout cancellation to be a 504 rather than a 500.

    WHY THE DEADLINE IS RE-READ HERE AND NOT BY THE CALLER
      The budget is spent by the WAIT, and the wait happens inside this
      function. A statement_timeout computed before engine.connect() is a
      duration measured from when the statement finally runs, so a request
      that queued 400ms for a slot gets its full budget all over again and
      overshoots the real deadline by exactly the queue wait. Under load
      the queue wait is the largest term, which is the regime topic 2 is
      about - so a deadline checked only on arrival is a deadline that is
      correct precisely when it does not matter.

      Pass `deadline_ms` and this function re-reads it AFTER checkout: no
      budget left means the query is never issued (counted as
      `deadline_abandoned`), and what is left becomes the statement_timeout.
    """
    eng = eng or await engine()
    role = config.get("ROLE")
    t0 = time.perf_counter()
    seconds = max(0.0, service_ms / 1000.0)
    try:
        async with eng.connect() as conn:
            waited = time.perf_counter() - t0
            POOL_WAIT_SECONDS.labels(role).observe(waited)
            # Sampled here, holding the connection, rather than after the
            # `with` block returns it - a gauge read after release always
            # reads one lower than the truth.
            in_use = pool_in_use()
            POOL_IN_USE.labels(role).set(in_use)
            if deadline_ms is not None and config.get("PROPAGATE_DEADLINE"):
                left = dl.remaining_ms(deadline_ms)
                slack = float(config.get("DEADLINE_SLACK_MS"))
                if left is not None and left < slack:
                    # The wait ate the budget. Starting the query now would
                    # produce a correct answer nobody is waiting for, while
                    # holding this slot for the whole of service_ms.
                    counters.inc("deadline_abandoned")
                    return {"wait_ms": waited * 1000.0, "query_ms": 0.0,
                            "in_use": in_use,
                            "error": "deadline_exceeded_after_pool_wait"}
                statement_timeout_ms = dl.statement_timeout_ms(deadline_ms)
            q0 = time.perf_counter()
            try:
                async with conn.begin():
                    if statement_timeout_ms is not None:
                        await conn.execute(
                            text(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}")
                        )
                    # pg_sleep holds the connection; the count() gives the
                    # server something real to do and the round trip a real
                    # result to carry back.
                    await conn.execute(text("SELECT pg_sleep(:s)"), {"s": seconds})
                    await conn.execute(text("SELECT count(*) FROM lab_rows"))
                counters.inc("db_queries")
                return {"wait_ms": waited * 1000.0,
                        "query_ms": (time.perf_counter() - q0) * 1000.0,
                        "in_use": in_use, "error": None}
            except Exception as exc:
                counters.inc("db_errors")
                return {"wait_ms": waited * 1000.0,
                        "query_ms": (time.perf_counter() - q0) * 1000.0,
                        "in_use": in_use,
                        "error": type(exc).__name__ + ": " + str(exc)[:200]}
    except Exception as exc:
        # Checkout itself failed - pool_timeout, or Postgres refusing the
        # connection ("sorry, too many clients already"). Topic 1's Go
        # section is about exactly this: an unbounded pool moves the queue
        # into the database and turns latency into availability.
        counters.inc("db_errors")
        return {"wait_ms": (time.perf_counter() - t0) * 1000.0,
                "query_ms": 0.0, "in_use": pool_in_use(),
                "error": type(exc).__name__ + ": " + str(exc)[:200]}


SCHEMA = """
-- Same shape as the table topic 1's standalone latency_knee.py creates, so
-- the two can share a `failure_lab` database without colliding.
CREATE TABLE IF NOT EXISTS lab_rows (
    id      integer PRIMARY KEY,
    payload text
);

CREATE TABLE IF NOT EXISTS charges (
    id          bigserial PRIMARY KEY,
    idem_key    text        NOT NULL,
    amount_cents integer    NOT NULL,
    currency    text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
-- Deliberately NO unique index on charges.idem_key. Topic 7's naive mode
-- has to be able to double-charge, or there is nothing to demonstrate.

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         text PRIMARY KEY,
    fingerprint text        NOT NULL,
    state       text        NOT NULL,     -- 'in_progress' | 'done'
    response    jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL
);
"""


async def init_schema() -> None:
    """Create the lab's tables and seed lab_rows. Idempotent; safe on restart."""
    eng = await engine()
    async with eng.begin() as conn:
        for stmt in [s for s in SCHEMA.split(";") if s.strip()]:
            await conn.execute(text(stmt))
        await conn.execute(text(
            "INSERT INTO lab_rows (id, payload) SELECT g, repeat('x', 64) "
            "FROM generate_series(1, 1000) AS g ON CONFLICT DO NOTHING"
        ))


async def wait_for_db(timeout_s: float = 60.0) -> bool:
    """Block until Postgres answers, so the first request does not pay for it."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            eng = await engine()
            async with eng.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            await asyncio.sleep(0.5)
    return False
