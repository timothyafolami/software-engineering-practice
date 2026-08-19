"""
Layer 6 lab - `worker`: the background consumer, and where trace context breaks.

A queue is the one boundary no runtime can help you with. There is no wire and
no header: a job is a row in a table, so the only place a `traceparent` can
live is a column you decided to add and a line of code you remembered to write.
Topic 3's first break is simply not writing it.

  BREAK=queue_no_traceparent   `api` stops writing the column; this consumer
                               then starts a brand-new trace per job, and the
                               Tempo waterfall shows an orphan rather than a
                               truncation. Nothing errors.

VERIFICATION STATUS
-------------------
Run inside the compose stack on 2026-08-19. It consumes jobs, joins the
producer's trace when the traceparent column is written, and starts a fresh
trace when BREAK=queue_no_traceparent removes it. One defect was found and
fixed on that first run: see the json.loads note in process().
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

from opentelemetry import trace
from opentelemetry.propagate import extract
from sqlalchemy import create_engine, text

from app import JsonFormatter  # one formatter, both services

# `api` uses the async driver; this consumer is a plain loop, so it uses the
# synchronous one. Same URL, same psycopg3 package.
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://lab:lab@db:5432/lab")
POLL_INTERVAL = float(os.environ.get("WORKER_POLL_SECONDS", "0.25"))
BATCH = int(os.environ.get("WORKER_BATCH", "10"))

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logging.getLogger().handlers[:] = [handler]
logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger("worker")

tracer = trace.get_tracer("lab.worker")
engine = create_engine(DATABASE_URL, pool_size=5, max_overflow=0)


def claim(conn, limit: int):
    """SKIP LOCKED: the standard Postgres-as-a-queue claim.

    Layer 3 covers why this is the right lock; here it matters only because a
    job has to be claimed exactly once for the trace to make sense.
    """
    return conn.execute(text(
        "UPDATE jobs SET state = 'running', claimed_at = now() "
        "WHERE id IN (SELECT id FROM jobs WHERE state = 'pending' "
        "             ORDER BY id FOR UPDATE SKIP LOCKED LIMIT :limit) "
        "RETURNING id, payload, traceparent"), {"limit": limit}).mappings().all()


def process(job) -> None:
    # The whole of cross-process propagation for a queue: parse the column you
    # wrote, and start the span as a child of it. With no column, `extract`
    # returns an empty context and this span begins a new trace -- which is
    # exactly what an orphan in Tempo is.
    carrier = {}
    if job["traceparent"]:
        carrier["traceparent"] = job["traceparent"]
    context = extract(carrier)

    with tracer.start_as_current_span("process job", context=context) as span:
        span.set_attribute("messaging.operation.name", "process")
        span.set_attribute("job.id", job["id"])
        span.set_attribute("job.had_traceparent", bool(job["traceparent"]))
        # `payload` is a jsonb column, and psycopg3 adapts jsonb to a Python
        # object on the way out -- so this is already a dict and json.loads()
        # raises TypeError on the first job. psycopg2 handed you a string,
        # which is why this line looks right and is the kind of thing that
        # only shows up the first time the consumer sees real traffic.
        payload = job["payload"]
        if isinstance(payload, (str, bytes, bytearray)):
            payload = json.loads(payload)
        log.info("processing job", extra={"extra_fields": {
            "job_id": job["id"],
            "customer_id": payload.get("customer_id", ""),
            "had_traceparent": bool(job["traceparent"]),
        }})
        time.sleep(0.02)   # the pretend work


def main() -> None:
    log.info("worker starting", extra={"extra_fields": {
        "break": os.environ.get("BREAK", "") or None,
        "poll_seconds": POLL_INTERVAL,
    }})
    while True:
        with engine.begin() as conn:
            jobs = claim(conn, BATCH)
        if not jobs:
            time.sleep(POLL_INTERVAL)
            continue
        for job in jobs:
            process(job)
        with engine.begin() as conn:
            conn.execute(text("UPDATE jobs SET state = 'done' WHERE id = ANY(:ids)"),
                         {"ids": [job["id"] for job in jobs]})


if __name__ == "__main__":
    main()
