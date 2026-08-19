"""
The one service every experiment in Topic 7 drives.

WHAT THIS DEMONSTRATES
  Nothing on its own. It is the load-bearing shape: a FastAPI service with
  three endpoints of deliberately different cost profiles, so that a single
  container spec can be measured against CPU-bound work, IO-bound work, and
  the mixture that real handlers actually are.

    /cpu     ~15ms of pure hashing. Stands in for serialisation, template
             rendering, Pydantic validation -- the CPU a "thin" handler
             really spends. This is the endpoint CFS quota bites.
    /db      one indexed SELECT plus a short pg_sleep. The pg_sleep is not
             cheating: it models the network + planning + IO wait of a
             query that crosses a socket, which is what your p50 is made
             of. This endpoint is nearly free on CPU.
    /mixed   both, in the order a real handler does them: query, then
             serialise. This is the one that shows throttling worst,
             because it has enough CPU to drain the bucket and enough wait
             to keep many requests in flight at once.
    /stat    the container's own /sys/fs/cgroup/cpu.stat, as JSON, so a
             load script can read ground truth without a shell.

WHAT TO LOOK FOR IN THE OUTPUT
  At startup it prints the gap this whole topic is about: how many CPUs the
  runtime thinks it has, versus how many the kernel will actually let it
  use. On a laptop those are equal. Under `--cpus=1.0` on an 8-core host
  they are 8 and 1.00, and every default the runtime picked was picked for
  the 8.

RUN (host, no container -- endpoints work, cgroup fields read "n/a")
    pip install -r requirements.txt
    uvicorn main:app --port 8000

RUN (the real thing)
    docker compose up -d --build      # from 00-harness/
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI

# cgroup.py lives in ../local on the host and beside this file in the image.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "local"))
from cgroup import cpu_stat, cpu_quota, memory_limit, runtime_cpu_answers

# 256 KiB is big enough that hashlib drops the GIL around each update(),
# which matters once this process runs more than one worker thread.
HASH_BLOCK = os.urandom(256 * 1024)

# Tuned so /cpu costs roughly 15ms. Measured at import, never hardcoded:
# an M1 and a c6i core are not the same core, and a wrong constant here
# silently changes what every experiment in the topic is measuring.
#
# Two things this got wrong the first time it was run for real, both of
# which quietly corrupt every worker-count comparison in 7.2:
#
#   1. A single timing sample is not a calibration. Under `--workers 4`
#      all four workers calibrate at the same instant inside the same
#      1.0-CPU cgroup, so each one measures a per-round cost inflated by
#      the other three -- and therefore picks FEWER rounds. Measured here:
#      ~10.8ms of CPU per /cpu request at 4 workers against ~16.4ms at 1
#      worker, from the same image, same quota, same offered rate. The
#      4-worker "baseline" was doing two thirds of the work of the 1-worker
#      "fix", which is the comparison inverting itself before it starts.
#      Best-of-N fixes it: contention can only ever make a sample slower.
#
#   2. There was no way to pin it. Any experiment that recreates the
#      container between cells needs every cell to do identical work, so
#      CPU_ROUNDS is now readable from the environment and reported by
#      /stat. Calibrate once, pin it, then sweep the variable you meant to
#      sweep.
CPU_ROUNDS = 1
CPU_ROUNDS_PINNED = os.environ.get("CPU_ROUNDS", "").strip()


def _calibrate(target_ms: float = 15.0, trials: int = 5) -> int:
    if CPU_ROUNDS_PINNED:
        return max(1, int(CPU_ROUNDS_PINNED))
    best_ms = float("inf")
    for _ in range(trials):
        mark = time.thread_time()
        digest = hashlib.sha256()
        for _ in range(32):
            digest.update(HASH_BLOCK)
        digest.hexdigest()
        best_ms = min(best_ms, (time.thread_time() - mark) * 1000.0 / 32)
    return max(1, round(target_ms / best_ms))


def burn_cpu(rounds: int) -> str:
    digest = hashlib.sha256()
    for _ in range(rounds):
        digest.update(HASH_BLOCK)
    return digest.hexdigest()[:16]


app = FastAPI(title="07-inside-a-container harness")

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://lab:lab@db:5432/container_lab"
)
DB_SLEEP_S = float(os.environ.get("DB_SLEEP_S", "0.020"))

_pool = None


@app.on_event("startup")
async def startup() -> None:
    global CPU_ROUNDS, _pool
    CPU_ROUNDS = _calibrate()

    answers = runtime_cpu_answers()
    quota = cpu_quota()
    print("=" * 68, flush=True)
    print(f"  pid {os.getpid()} starting", flush=True)
    print(f"  the runtime thinks it has : {answers}", flush=True)
    print(
        "  the kernel will let it use: "
        + (f"{quota:.2f} CPU (cpu.max)" if quota else "no quota (cpu.max = max)"),
        flush=True,
    )
    print(f"  memory.max               : {memory_limit() or 'unlimited'}", flush=True)
    print(
        f"  /cpu calibrated to        : {CPU_ROUNDS} hash rounds ~= 15ms"
        + ("  (PINNED via CPU_ROUNDS)" if CPU_ROUNDS_PINNED else ""),
        flush=True,
    )
    print("=" * 68, flush=True)

    try:
        import asyncpg

        _pool = await asyncpg.create_pool(
            DSN.replace("postgresql+asyncpg://", "postgresql://"),
            min_size=int(os.environ.get("POOL_MIN", "2")),
            max_size=int(os.environ.get("POOL_MAX", "10")),
        )
    except Exception as exc:  # a DB-less run still exercises /cpu
        print(f"  no database pool ({exc.__class__.__name__}: {exc})", flush=True)
        _pool = None


@app.on_event("shutdown")
async def shutdown() -> None:
    if _pool is not None:
        await _pool.close()


@app.get("/cpu")
async def cpu() -> dict:
    return {"digest": burn_cpu(CPU_ROUNDS)}


@app.get("/db")
async def db() -> dict:
    if _pool is None:
        return {"rows": None, "note": "no pool"}
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, payload, pg_sleep($1) FROM lab_rows "
            "WHERE id = $2",
            DB_SLEEP_S,
            1,
        )
    return {"id": row["id"]}


@app.get("/mixed")
async def mixed() -> dict:
    result = await db()
    return {"db": result, "digest": burn_cpu(CPU_ROUNDS)}


@app.get("/stat")
async def stat() -> dict:
    """Ground truth, over HTTP, so a load script can sample it mid-run."""
    return {
        "cpu_stat": cpu_stat(),
        "cpu_quota": cpu_quota(),
        "pid": os.getpid(),
        # So an experiment can pin every cell to one workload instead of
        # re-calibrating per container and comparing two different tests.
        "cpu_rounds": CPU_ROUNDS,
        "cpu_rounds_pinned": bool(CPU_ROUNDS_PINNED),
    }


@app.get("/healthz")
async def healthz() -> dict:
    # Deliberately trivial. A health check that passes while p99 is
    # destroyed is exactly how throttling stays invisible for months.
    return {"ok": True}
