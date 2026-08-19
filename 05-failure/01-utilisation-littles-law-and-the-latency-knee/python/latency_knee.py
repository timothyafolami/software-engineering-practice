"""
Layer 5 - Topic 1: the latency knee, measured on the real stack.

Python, because this is the exact shape of the user's production problem:
FastAPI + uvicorn + SQLAlchemy + a real Postgres connection pool. Nothing
here is simulated. `/work` runs `pg_sleep()` inside a real pooled
connection, so a request that cannot get a pool slot really does queue,
exactly as it does in production.

WHAT THIS DEMONSTRATES
  Queueing delay is proportional to 1/(1-rho), not to load. Offered rate
  rises smoothly from 20% to 110% of capacity; p99 does not.

WHAT TO LOOK FOR IN THE OUTPUT
  1. `achieved` plateaus at roughly pool_size / service_time while
     `offered` keeps climbing. That plateau is Little's Law rearranged:
     lambda_max = L / W. The gap between the two columns is the backlog.
  2. p99 tracks the predicted S/(1-rho) column while rho < 1, then leaves
     it behind entirely once the queue stops being able to drain.
  3. `pool wait` is ~0 at rho=0.2 and dominates total latency by rho=0.9.
     The handler code never changed. Only the arithmetic of waiting did.
  4. The second sweep doubles pool_size and nothing else. Capacity and the
     knee move proportionally - the cliff sits at the smallest count.

The load generator is OPEN MODEL (Poisson arrivals at a fixed rate). It
does not wait for a response before sending the next request. Topic 6
explains why a closed-loop generator would erase the whole effect.

RUN
    python3 latency_knee.py

Requires a local Postgres accepting connections (`pg_isready`) and the
packages in requirements.txt. Creates a database called `failure_lab` if it
does not exist; drop it with `dropdb failure_lab` when you are done.
Takes about three minutes: twelve measured steps of twelve seconds each.
"""
from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import os
import random
import statistics
import subprocess
import sys
import time

# ---------------------------------------------------------------- config

SERVICE_SECONDS = 0.040          # pg_sleep duration inside the handler
POOL_SIZES = (5, 10)             # the ONLY thing that changes between sweeps
RHOS = (0.2, 0.5, 0.8, 0.9, 0.95, 1.1)
STEP_SECONDS = 12.0
DRAIN_SECONDS = 3.0
PORT_BASE = 8541

PG_USER = os.environ.get("PGUSER", getpass.getuser())
PG_HOST = os.environ.get("PGHOST", "/tmp")
LAB_DB = "failure_lab"


def sync_dsn(dbname: str) -> str:
    return f"postgresql://{PG_USER}@/{dbname}?host={PG_HOST}"


def async_dsn(dbname: str) -> str:
    return f"postgresql+asyncpg://{PG_USER}@/{dbname}?host={PG_HOST}"


# ------------------------------------------------------------- bootstrap

def ensure_lab_database() -> None:
    """Create failure_lab and its one table if they are not already there."""
    try:
        import psycopg
    except ImportError:
        sys.exit("missing dependencies: python3 -m pip install -r requirements.txt")

    if subprocess.run(["pg_isready", "-h", PG_HOST],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        sys.exit(f"no Postgres accepting connections on {PG_HOST}. "
                 "Start one, then rerun: this topic measures a real pool, not a simulated one.")

    with psycopg.connect(sync_dsn("postgres"), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (LAB_DB,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{LAB_DB}"')
            print(f"created database {LAB_DB}")

    with psycopg.connect(sync_dsn(LAB_DB), autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS lab_rows (id int primary key, payload text)"
        )
        n = conn.execute("SELECT count(*) FROM lab_rows").fetchone()[0]
        if n < 5000:
            conn.execute(
                "INSERT INTO lab_rows "
                "SELECT g, repeat('x', 64) FROM generate_series(1, 5000) g "
                "ON CONFLICT DO NOTHING"
            )


# ---------------------------------------------------------- server (child)

def serve(port: int, pool_size: int, service_seconds: float) -> None:
    """Run the FastAPI app. Executed in a child process, never inline."""
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(
        async_dsn(LAB_DB),
        pool_size=pool_size,
        max_overflow=0,        # so `pool_size` really is the whole budget
        pool_timeout=30.0,     # SQLAlchemy's default; the 30s cliff
    )

    st = {
        "inflight": 0,
        "samples": [],        # in-flight gauge samples -> Little's Law L
        "service": [],        # time holding the connection            -> S
        "wait": [],           # time waiting for a pool slot
        "total": [],          # whole handler                          -> W
        "errors": 0,
        "last_error": "",
    }

    async def sample_inflight() -> None:
        while True:
            await asyncio.sleep(0.02)
            st["samples"].append(st["inflight"])

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        sampler = asyncio.create_task(sample_inflight())
        try:
            yield
        finally:
            sampler.cancel()

    app = FastAPI(lifespan=lifespan)

    @app.get("/work")
    async def work() -> dict:
        t0 = time.perf_counter()
        st["inflight"] += 1
        try:
            async with engine.connect() as conn:
                t1 = time.perf_counter()
                await conn.execute(
                    text("SELECT pg_sleep(:s), count(*) FROM lab_rows"),
                    {"s": service_seconds},
                )
                t2 = time.perf_counter()
            st["wait"].append(t1 - t0)
            st["service"].append(t2 - t1)
            st["total"].append(t2 - t0)
            return {"ok": True}
        except Exception as exc:
            # A 500 here would make uvicorn print a full traceback per failed
            # request, which at overload buries the table under megabytes of
            # stack. The failure is the finding, so count it and report it as
            # a 503 the client can tally.
            st["errors"] += 1
            st["last_error"] = type(exc).__name__
            return JSONResponse({"ok": False, "error": type(exc).__name__},
                                status_code=503)
        finally:
            st["inflight"] -= 1

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/stats")
    async def stats() -> dict:
        """Read and reset. The client calls this at the end of every step."""
        out = {
            "n": len(st["total"]),
            "mean_inflight": (
                statistics.fmean(st["samples"]) if st["samples"] else 0.0
            ),
            "p50_total": pct(st["total"], 50),
            "p99_total": pct(st["total"], 99),
            "p50_wait": pct(st["wait"], 50),
            "p99_wait": pct(st["wait"], 99),
            "mean_service": statistics.fmean(st["service"]) if st["service"] else 0.0,
            "errors": st["errors"],
            "last_error": st["last_error"],
        }
        for key in ("samples", "service", "wait", "total"):
            st[key] = []
        st["errors"] = 0
        st["last_error"] = ""
        return out

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = min(len(ordered) - 1, int(round((p / 100.0) * (len(ordered) - 1))))
    return ordered[k]


# ------------------------------------------------ minimal HTTP (client side)

async def http_get(path: str, port: int, timeout: float = 60.0) -> tuple[int, bytes]:
    """One GET over asyncio streams. Deliberately not httpx.

    The load generator shares an event loop with every in-flight response it
    is parsing. A client library that spends a millisecond of Python per
    request will, at a few hundred rps, stall that loop for long enough that
    the generator sends late -- and a late generator quietly becomes a
    closed-loop one, which is the single most common way this experiment
    lies to you. `Connection: close` keeps the response parse to "read until
    EOF" and costs one loopback handshake.
    """
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port), timeout)
    try:
        writer.write(
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Connection: close\r\n\r\n".encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        status = int(head.split(b" ", 2)[1])
        body = await asyncio.wait_for(reader.read(), timeout)
        return status, body
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------- client (parent)

async def open_loop_step(port: int, path: str, rate: float, seconds: float) -> dict:
    """Poisson arrivals at `rate` per second. Never waits for a response.

    Two details do all the work here, and getting either wrong silently
    turns this into a closed-loop generator that erases the knee:

      * the arrival times are computed UP FRONT, as absolute deadlines.
        A `sleep(expovariate(rate))` loop instead accumulates its own
        overhead into the schedule, so the harder the server is working
        the slower the generator sends -- the client backs off in
        sympathy with the thing it is supposed to be overloading.
      * latency is timed from the moment a request was DUE, not from the
        moment this loop got round to dispatching it. If the generator
        does fall behind, that lateness lands in the latency numbers
        instead of disappearing. `gen late` reports it directly, so a
        run where the client, not the server, was the bottleneck is
        visible rather than plausible-looking. That is coordinated
        omission, and topic 6 is entirely about it.
    """
    latencies: list[float] = []
    lateness: list[float] = []
    completions: list[float] = []
    statuses: dict[str, int] = {}

    begin = time.perf_counter()
    schedule: list[float] = []
    t = begin + random.expovariate(rate)
    while t < begin + seconds:
        schedule.append(t)
        t += random.expovariate(rate)

    inflight: set[asyncio.Task] = set()

    async def one(due: float) -> None:
        try:
            status, _ = await http_get(path, port)
            key = str(status)
        except Exception as exc:
            key = type(exc).__name__
        done = time.perf_counter()
        completions.append(done)
        latencies.append(done - due)
        statuses[key] = statuses.get(key, 0) + 1

    for due in schedule:
        delay = due - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        lateness.append(max(0.0, time.perf_counter() - due))
        task = asyncio.create_task(one(due))
        inflight.add(task)
        task.add_done_callback(inflight.discard)

    window_end = begin + seconds
    # Drain. Past rho=1 this is where the backlog built during the step
    # finally comes out, which is why those rows carry latencies larger
    # than the step itself.
    if inflight:
        await asyncio.wait(inflight, timeout=90.0)

    return {
        "sent": len(schedule),
        "offered_rate": len(schedule) / seconds,
        "target_rate": rate,
        "completed": len(latencies),
        # Completions that landed INSIDE the step, not after the drain.
        # Past rho=1 the gap between this and `offered` is the backlog.
        "achieved_rate": sum(1 for c in completions if c <= window_end) / seconds,
        "p50": pct(latencies, 50),
        "p99": pct(latencies, 99),
        "gen_late_p99": pct(lateness, 99),
        "statuses": statuses,
    }


async def fetch_stats(port: int) -> dict:
    _, body = await http_get("/stats", port, timeout=30.0)
    return json.loads(body)


async def wait_ready(port: int, timeout: float = 30.0) -> None:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        try:
            status, _ = await http_get("/health", port, timeout=2.0)
            if status == 200:
                return
        except Exception:
            pass
        await asyncio.sleep(0.2)
    raise RuntimeError(f"server on port {port} never became ready")


# ------------------------------------------------------------------ sweep

async def sweep(port: int, pool_size: int) -> None:
    await wait_ready(port)

    # Two passes, and the first one is thrown away on purpose. SQLAlchemy
    # fills the pool lazily, so pass one pays for `pool_size` TCP connections
    # to Postgres, the first parse of the statement, and a cold page cache.
    # Measuring S on that pass overstates it by ~50% and every rho below is
    # then computed against a capacity that does not exist -- the sweep runs
    # under-loaded and no knee appears. Discard, then measure.
    await open_loop_step(port, "/work", rate=pool_size * 4.0, seconds=3.0)
    await fetch_stats(port)
    await open_loop_step(port, "/work", rate=pool_size * 2.0, seconds=3.0)
    warm = await fetch_stats(port)
    service = warm["mean_service"]
    capacity = pool_size / service

    print(f"\n=== pool_size={pool_size}, max_overflow=0 "
          f"(uvicorn 1 worker, real Postgres) ===")
    print(f"measured service time S      : {service * 1000:7.2f} ms "
          f"(pg_sleep({SERVICE_SECONDS}) + query + round trip)")
    print(f"predicted capacity  L/S      : {capacity:7.1f} rps "
          f"= {pool_size} slots / {service * 1000:.1f} ms")
    print()
    header = (f"{'rho':>5} {'offered':>8} {'achieved':>9} {'p50':>8} {'p99':>9} "
              f"{'pool p50':>9} {'L (gauge)':>10} {'lam x W':>9} {'S/(1-r)':>9} "
              f"{'gen late':>9}")
    print(header)
    print("-" * len(header))

    measured = []
    for rho in RHOS:
        rate = capacity * rho
        result = await open_loop_step(port, "/work", rate=rate, seconds=STEP_SECONDS)
        server = await fetch_stats(port)
        little = result["achieved_rate"] * server["p50_total"]
        predicted = service / (1 - rho) if rho < 1 else float("inf")
        measured.append((rho, result, server, predicted))
        pred_txt = f"{predicted * 1000:9.1f}" if predicted != float("inf") else "      inf"
        print(f"{rho:5.2f} {result['offered_rate']:8.1f} "
              f"{result['achieved_rate']:9.1f} "
              f"{result['p50'] * 1000:8.1f} {result['p99'] * 1000:9.1f} "
              f"{server['p50_wait'] * 1000:9.1f} "
              f"{server['mean_inflight']:10.1f} "
              f"{little:9.1f} {pred_txt} "
              f"{result['gen_late_p99'] * 1000:9.1f}")
        non_200 = {k: v for k, v in result["statuses"].items() if k != "200"}
        if non_200:
            note = f"        non-200 / failed: {non_200}"
            if server["errors"]:
                note += f"  (server: {server['errors']} x {server['last_error']})"
            print(note)
        await asyncio.sleep(DRAIN_SECONDS)

    print()
    print("  columns: p50/p99 are client-side, ms, timed from when each request")
    print("  was DUE. `offered` is the scheduled arrival rate; `achieved` counts")
    print("  only completions that landed inside the step, so the two diverge")
    print("  exactly when a backlog forms. `pool p50` is server-side time spent")
    print("  waiting for a free pool slot. `L (gauge)` is the mean in-flight count")
    print("  sampled every 20ms in the server. `lam x W` is Little's Law from the")
    print("  achieved rate and the server p50. `S/(1-r)` is the single-server")
    print("  queueing prediction. `gen late` is the client's own dispatch lateness:")
    print("  if it is not small compared to p50, the generator was the bottleneck")
    print("  and the rest of the row is measuring this script, not the server.")
    plot_knee(measured)


def plot_knee(measured: list) -> None:
    """ASCII chart, because the knee is a shape and a table hides shapes."""
    rows = [(rho, r["p99"] * 1000) for rho, r, _s, _p in measured]
    top = max(v for _, v in rows) or 1.0
    width = 56
    print()
    print("  p99 (ms) against rho")
    for rho, value in rows:
        bar = "#" * max(1, int(round(width * value / top)))
        print(f"  rho={rho:<5.2f} |{bar} {value:.0f}")
    print(f"  {'':11}+{'-' * width} {top:.0f} ms full scale")


# ------------------------------------------------------------------- main

async def main() -> None:
    ensure_lab_database()
    print("Latency knee on FastAPI + uvicorn + SQLAlchemy + Postgres")
    print("open-model load generator: Poisson arrivals, no waiting for responses")

    for index, pool_size in enumerate(POOL_SIZES):
        port = PORT_BASE + index
        child = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--serve",
             str(port), str(pool_size), str(SERVICE_SECONDS)],
            stdout=subprocess.DEVNULL,
        )
        try:
            await sweep(port, pool_size)
        finally:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()

    print()
    print("Both sweeps used identical code and an identical ramp. The only")
    print("difference is `pool_size`. Compare the two capacity lines and the")
    print("two rho=0.9 rows: that is the whole topic in two numbers.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        serve(int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]))
    else:
        asyncio.run(main())
