"""
7.5 -- the four handler bodies, mounted over the harness service.

WHAT THIS DEMONSTRATES
  The same /db endpoint, written four ways, in one file so the diff between
  them is the whole experiment:

    1. `async def` calling psycopg2 directly.
       The sync driver inside the async handler. The event loop stops for
       the duration of every query -- every other in-flight request on this
       worker waits, whether or not it touches the database. This is Layer
       1 Topic 3 exactly, with a database driver in the role of
       time.sleep().

    2. `def` calling psycopg2.
       Starlette sees a plain `def` and runs it via anyio.to_thread.run_sync().
       Correct, and fine up to ANYIO_THREAD_TOKENS concurrent requests per
       process -- after which request 41 blocks acquiring a token, with no
       exception, no log line and no metric.

    3. Same as (2) with the limiter raised.
       More concurrency, and more runnable threads in the same cgroup
       draining the same CPU bucket. Past a point this makes p99 WORSE, and
       finding that point is the experiment.

    4. `async def` with asyncpg.
       The actual fix. No blocking call, no thread pool, no extra threads
       in the cgroup.

  The variant is chosen by the VARIANT environment variable, so the
  container spec is byte-identical across all four runs -- which is the
  only way the throttle-ratio column means anything.

WHAT TO LOOK FOR IN THE OUTPUT
  Each response carries the pid and the variant, and /variant-info reports
  the live anyio token count and the thread census. Watch the thread count
  rise between variants 2 and 3: that is Stall A's fix becoming Stall B's
  cause.

RUN
  Not directly. It is mounted over the harness app by
  ../python/run_variants.py, or by hand:

    docker compose run --rm \
      -v "$PWD/../05-the-sync-driver-inside-the-async-endpoint/app/variants.py:/app/variants.py:ro" \
      -e VARIANT=1 api

  On macOS against a local Postgres you can run variants 1 and 4 on the
  host and see Stall A with no possibility of Stall B contaminating it --
  which is worth doing first, precisely because the throttle ratio is then
  guaranteed to be zero.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import anyio
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "local"))
from cgroup import cpu_quota, cpu_stat  # noqa: E402

VARIANT = int(os.environ.get("VARIANT", "1"))
DB_SLEEP_S = float(os.environ.get("DB_SLEEP_S", "0.050"))
POOL_MAX = int(os.environ.get("POOL_MAX", "10"))
ANYIO_THREAD_TOKENS = int(os.environ.get("ANYIO_THREAD_TOKENS", "40"))
DSN = os.environ.get("DATABASE_URL", "postgresql://lab:lab@db:5432/container_lab")

# The query every variant runs. pg_sleep is not cheating: it models the
# network + planning + IO wait of a query crossing a socket, which is what
# a p50 is actually made of. Below ~20ms the wait is too short for the
# event-loop stall to be measurable, which is its own lesson about why
# people fail to reproduce this bug locally.
QUERY = "SELECT id, pg_sleep(%s) FROM lab_rows WHERE id = %s"
QUERY_ASYNCPG = "SELECT id, pg_sleep($1) FROM lab_rows WHERE id = $2"

app = FastAPI(title=f"7.5 variant {VARIANT}")

_sync_pool = None   # psycopg2 ThreadedConnectionPool, variants 1-3
_async_pool = None  # asyncpg pool, variant 4


def thread_census() -> int:
    status = Path("/proc/self/status")
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith("Threads:"):
                return int(line.split()[1])
    return threading.active_count()


@app.on_event("startup")
async def startup() -> None:
    global _sync_pool, _async_pool

    # Raising the limiter is one line, and it is variant 3's entire change.
    # It must happen in a lifespan handler: the limiter is per-process and
    # the default one has already been created by the time a request runs.
    if VARIANT == 3:
        anyio.to_thread.current_default_thread_limiter().total_tokens = ANYIO_THREAD_TOKENS

    if VARIANT in (1, 2, 3):
        from psycopg2.pool import ThreadedConnectionPool

        # maxconn matches the async pool's max_size so the four variants
        # differ in exactly one thing: how the query is called. A different
        # pool size would make the comparison meaningless.
        _sync_pool = ThreadedConnectionPool(1, POOL_MAX, DSN)
    else:
        import asyncpg

        _async_pool = await asyncpg.create_pool(DSN, min_size=2, max_size=POOL_MAX)

    quota = cpu_quota()
    print("=" * 68, flush=True)
    print(f"  7.5 variant {VARIANT}: {DESCRIPTIONS[VARIANT]}", flush=True)
    print(f"  pid {os.getpid()}", flush=True)
    print(f"  DB_SLEEP_S {DB_SLEEP_S}  POOL_MAX {POOL_MAX}", flush=True)
    print(f"  anyio tokens: "
          f"{anyio.to_thread.current_default_thread_limiter().total_tokens}", flush=True)
    print(f"  cpu.max     : {f'{quota:.2f} CPU' if quota else 'no quota'}", flush=True)
    print(f"  OS threads  : {thread_census()}", flush=True)
    print("=" * 68, flush=True)


@app.on_event("shutdown")
async def shutdown() -> None:
    if _sync_pool is not None:
        _sync_pool.closeall()
    if _async_pool is not None:
        await _async_pool.close()


DESCRIPTIONS = {
    1: "async def + psycopg2 -- the sync driver inside the async handler",
    2: "def + psycopg2 -- Starlette offloads to the anyio thread pool (40 tokens)",
    3: f"def + psycopg2 with the limiter raised to {ANYIO_THREAD_TOKENS}",
    4: "async def + asyncpg -- the actual fix",
}


def _query_sync() -> int:
    """The blocking call. Identical in variants 1, 2 and 3 -- what differs
    is only WHERE it runs: on the event loop's thread, or on a worker."""
    conn = _sync_pool.getconn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(QUERY, (DB_SLEEP_S, 1))
            row = cursor.fetchone()
        conn.commit()
        return row[0]
    finally:
        _sync_pool.putconn(conn)


if VARIANT == 1:
    # The bug. `async def` means this coroutine owns the event loop's one
    # thread until it returns, and _query_sync() does not yield -- it sits
    # in a socket read inside a C extension. Every other request in flight
    # on this worker waits DB_SLEEP_S, whether or not it touches the
    # database. The handler is `async`, it passes review, and the tests
    # pass because tests are one request at a time.
    @app.get("/db")
    async def db() -> dict:
        started = time.perf_counter()
        row_id = _query_sync()
        return {"id": row_id, "variant": 1, "pid": os.getpid(),
                "ms": round((time.perf_counter() - started) * 1000, 1)}

elif VARIANT in (2, 3):
    # Plain `def`. Starlette runs it via anyio.to_thread.run_sync(), so the
    # event loop keeps turning. Correct -- and the fix costs threads, which
    # is Stall B's input. Variant 3 is this exact code with a bigger
    # limiter, set in the lifespan handler above.
    @app.get("/db")
    def db() -> dict:  # noqa: D401 -- deliberately not async
        started = time.perf_counter()
        row_id = _query_sync()
        return {"id": row_id, "variant": VARIANT, "pid": os.getpid(),
                "ms": round((time.perf_counter() - started) * 1000, 1)}

else:
    # The fix. asyncpg's socket read is awaited, so the loop is free during
    # the wait and no thread pool is involved at all. Note what this does
    # to the cgroup: it REMOVES runnable threads rather than adding them,
    # which is why it can improve p99 and the throttle ratio at once.
    @app.get("/db")
    async def db() -> dict:
        started = time.perf_counter()
        async with _async_pool.acquire() as conn:
            row = await conn.fetchrow(QUERY_ASYNCPG, DB_SLEEP_S, 1)
        return {"id": row["id"], "variant": 4, "pid": os.getpid(),
                "ms": round((time.perf_counter() - started) * 1000, 1)}


@app.get("/variant-info")
async def variant_info() -> dict:
    """Everything a load script needs to pair a latency with its cause.

    The two readings that separate Stall A from Stall B are both here:
    nr_throttled (the cgroup froze you) and the live token count plus
    thread census (the thread pool queued you).
    """
    limiter = anyio.to_thread.current_default_thread_limiter()
    return {
        "variant": VARIANT,
        "description": DESCRIPTIONS[VARIANT],
        "pid": os.getpid(),
        "anyio_total_tokens": limiter.total_tokens,
        "anyio_borrowed_tokens": limiter.borrowed_tokens,
        "os_threads": thread_census(),
        "cpu_quota": cpu_quota(),
        "cpu_stat": cpu_stat(),
        "db_sleep_s": DB_SLEEP_S,
        "pool_max": POOL_MAX,
    }


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "variant": VARIANT}
