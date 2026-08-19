"""
Start uvicorn with a worker count derived from the CGROUP QUOTA.

    python serve.py

WHY THIS FILE EXISTS: `workers = 2 * os.cpu_count() + 1` is the conventional
formula, and inside a container os.cpu_count() reports the HOST's cores. At
`cpus: "0.5"` that starts nine workers on half a core -- nine processes' worth
of pool connections, one half-process's worth of CPU, and CFS throttling on top.

worker_count.py holds the quota reading and is runnable on its own:

    docker compose -f lab/docker/compose.yml run --rm api python worker_count.py

Set WORKERS explicitly to override; WORKERS=0 (the default) means "ask the
quota". The startup line prints both numbers, so the difference is in the log
before anything else happens.
"""
from __future__ import annotations

import os

import uvicorn

from worker_count import read_quota, workers_for


def main() -> None:
    override = int(os.environ.get("WORKERS", "0"))
    quota, source = read_quota()
    naive = workers_for(os.cpu_count() or 1)

    if override:
        workers = override
        why = f"WORKERS={override} (explicit)"
    elif quota is not None:
        workers = workers_for(quota)
        why = f"cgroup quota {quota:.2f} cpu via {source}"
    else:
        workers = naive
        why = "no cgroup quota found; falling back to os.cpu_count()"

    print(f"[serve] os.cpu_count()={os.cpu_count()} -> {naive} workers (the naive answer)")
    print(f"[serve] starting {workers} workers -- {why}")
    if quota is not None and workers != naive:
        per_worker = int(os.environ.get("POOL_SIZE", "5")) + int(
            os.environ.get("MAX_OVERFLOW", "10"))
        print(f"[serve] that is {(naive - workers) * per_worker} fewer possible connections "
              f"than the naive answer would have opened, per replica")

    uvicorn.run("main:app", host="0.0.0.0", port=8000, workers=workers,
                log_level=os.environ.get("LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
