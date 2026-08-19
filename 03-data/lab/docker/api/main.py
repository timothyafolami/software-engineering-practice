"""
The one HTTP service in Layer 3: FastAPI + SQLAlchemy 2.0 async + psycopg.

Three endpoints, chosen so that Topics 6, 7 and 8 can all drive the same
service:

    GET /orders?limit=N          the N+1-shaped endpoint (Topic 6)
    GET /slow?seconds=S          a deliberately slow endpoint (Topic 7 bulkhead)
    POST /orders                 a write, returning its LSN token (Topic 8)
    GET /orders/{id}?read=...    read from primary / replica / lsn-token routing
    GET /healthz                 liveness that does NOT touch the pool, and
                                 /readyz which does -- the difference is Topic
                                 7's whole point about health checks

WHY THE ENGINE IS BUILT IN THE LIFESPAN and not at import time: an engine
created before the server forks is inherited by every worker with the same
sockets, and the resulting corruption produces errors that look like nothing
else. This is the single most expensive five-line mistake in the FastAPI +
SQLAlchemy stack, and the fix is exactly the placement below.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

DSN = os.environ.get(
    "LAB_DSN", "postgresql+psycopg://lab:lab@postgres-primary:5432/sep_lab_03_data")
REPLICA_DSN = os.environ.get("LAB_REPLICA_DSN", "")
POOL_SIZE = int(os.environ.get("POOL_SIZE", "5"))
MAX_OVERFLOW = int(os.environ.get("MAX_OVERFLOW", "10"))
POOL_TIMEOUT = float(os.environ.get("POOL_TIMEOUT", "30"))
ISOLATION = os.environ.get("ISOLATION", "read committed").upper().replace(" ", " ")

state: dict = {}


def build_engine(url: str, app_name: str):
    return create_async_engine(
        url,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT,
        isolation_level=ISOLATION,
        connect_args={"application_name": app_name},
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["engine"] = build_engine(DSN, "sep-api")
    state["session"] = async_sessionmaker(state["engine"], expire_on_commit=False)
    state["replica"] = build_engine(REPLICA_DSN, "sep-api-replica") if REPLICA_DSN else None
    yield
    await state["engine"].dispose()
    if state["replica"] is not None:
        await state["replica"].dispose()


app = FastAPI(title="Layer 3 lab API", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    """Liveness. Deliberately does NOT acquire a pooled connection.

    This is the health check almost every service ships, and it is why a pool
    that is fully exhausted -- every request queued, nothing completing -- still
    shows green. Topic 7's Node experiment measures exactly this.
    """
    return {"status": "ok", "checks": "process only"}


@app.get("/readyz")
async def readyz():
    """Readiness, done properly: it takes a connection from the pool.

    If the pool is exhausted this blocks and then fails, which is the truth.
    A readiness probe that cannot see pool exhaustion is not checking the thing
    that breaks.
    """
    try:
        async with state["engine"].connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - the probe reports, it does not raise
        raise HTTPException(status_code=503, detail=f"pool: {type(exc).__name__}") from exc
    pool = state["engine"].pool
    return {"status": "ok", "pool": pool.status()}


@app.get("/orders")
async def list_orders(limit: int = Query(10, ge=1, le=5000), eager: bool = False):
    """Topic 6. `eager=false` is the N+1; `eager=true` is the single join.

    Both return the same JSON. Only the query count differs, which is the entire
    point of that topic: you cannot tell these apart from the response.
    """
    async with state["session"]() as session:
        rows = (await session.execute(
            text("SELECT id, customer_id, status FROM orders ORDER BY id LIMIT :n"),
            {"n": limit})).all()
        if eager:
            joined = (await session.execute(text("""
                SELECT o.id, c.email, count(li.id) AS items
                FROM orders o
                JOIN customers c ON c.id = o.customer_id
                JOIN line_items li ON li.order_id = o.id
                WHERE o.id = ANY(:ids)
                GROUP BY o.id, c.email
            """), {"ids": [r.id for r in rows]})).all()
            return {"queries": 2, "orders": [dict(r._mapping) for r in joined]}

        out = []
        for r in rows:
            email = (await session.execute(
                text("SELECT email FROM customers WHERE id = :id"),
                {"id": r.customer_id})).scalar()
            items = (await session.execute(
                text("SELECT count(*) FROM line_items WHERE order_id = :id"),
                {"id": r.id})).scalar()
            out.append({"id": r.id, "email": email, "items": items})
        return {"queries": 1 + 2 * len(rows), "orders": out}


@app.get("/slow")
async def slow(seconds: float = Query(0.5, ge=0, le=30)):
    """Topic 7. Holds a pool connection for `seconds`, doing nothing else.

    This is the endpoint that makes every OTHER endpoint's p99 worse, and the
    bulkhead experiment is about giving it its own pool so it cannot.
    """
    async with state["engine"].connect() as conn:
        await conn.execute(text("SELECT pg_sleep(:s)"), {"s": seconds})
    return {"slept": seconds}


@app.post("/orders")
async def create_order(customer_id: int = 1, total_cents: int = 1000):
    """Topic 8. Returns the write's LSN, which is the token the read path needs.

    Captured AFTER the commit. Taken before, it names a position the replica may
    already have passed, the check passes, and the read is still stale.
    """
    async with state["engine"].begin() as conn:
        row = (await conn.execute(text("""
            INSERT INTO orders (id, customer_id, status, total_cents, created_at)
            VALUES ((SELECT coalesce(max(id), 0) + 1 FROM orders),
                    :cid, 'pending', :cents, now())
            RETURNING id
        """), {"cid": customer_id, "cents": total_cents})).one()
    async with state["engine"].connect() as conn:
        lsn = (await conn.execute(text("SELECT pg_current_wal_lsn()"))).scalar()
    return {"id": row.id, "lsn": str(lsn)}


@app.get("/orders/{order_id}")
async def get_order(order_id: int, read: str = "primary", lsn: str | None = None):
    """Topic 8. `read` selects the routing strategy: primary | replica | lsn."""
    replica = state["replica"]
    engine = state["engine"]
    served_by = "primary"

    if read == "replica" and replica is not None:
        engine, served_by = replica, "replica"
    elif read == "lsn" and replica is not None and lsn:
        async with replica.connect() as conn:
            # pg_last_wal_replay_lsn, never pg_last_wal_receive_lsn. Received
            # means the bytes arrived; replayed means a query can see them.
            caught_up = (await conn.execute(
                text("SELECT pg_last_wal_replay_lsn() >= CAST(:lsn AS pg_lsn)"),
                {"lsn": lsn})).scalar()
        if caught_up:
            engine, served_by = replica, "replica"

    async with engine.connect() as conn:
        row = (await conn.execute(
            text("SELECT id, status, total_cents FROM orders WHERE id = :id"),
            {"id": order_id})).first()
    if row is None:
        return {"found": False, "served_by": served_by}
    return {"found": True, "served_by": served_by, **dict(row._mapping)}


@app.get("/poolstats")
async def poolstats():
    """Topic 7's metric. Export the equivalent of this from your real service."""
    pool = state["engine"].pool
    return {
        "status": pool.status(),
        "checked_out": pool.checkedout(),
        "size": pool.size(),
        "overflow": pool.overflow(),
        "pool_size": POOL_SIZE,
        "max_overflow": MAX_OVERFLOW,
        "pool_timeout": POOL_TIMEOUT,
        "max_possible_per_worker": POOL_SIZE + MAX_OVERFLOW,
    }
