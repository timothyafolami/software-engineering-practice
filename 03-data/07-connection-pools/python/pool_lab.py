"""
Shared harness for Topic 7: an OPEN-LOOP load generator and a pool builder.

Imported by pool_sweep.py, bulkhead.py and retry_storm.py; not run directly.

WHY OPEN-LOOP MATTERS MORE THAN ANYTHING ELSE IN THIS FILE: the most common
load-testing error there is, and it silently inverts the conclusion of every
experiment in this topic.

A CLOSED-LOOP generator has N virtual users, each of which sends a request and
waits for the response before sending the next. When the service slows down,
those users send FEWER requests -- so offered load falls exactly when you most
need it not to, queueing never builds, and p99 looks flat across every pool size
you try. The system appears to have no capacity limit because you stopped
pushing it at one.

An OPEN-LOOP generator sends requests on a SCHEDULE, independent of how long
responses take. If the service cannot keep up, the queue grows -- which is what
real traffic does, because your users are not waiting for each other. Every
number in this topic depends on that difference.

The generator below therefore keeps a fixed arrival schedule and records, for
every request, when it was SUPPOSED to start. Latency is measured from that
scheduled time, not from when a thread got around to it -- otherwise the time a
request spends waiting for a worker disappears from the measurement, which is
the same bug in a different place (coordinated omission).
"""
from __future__ import annotations

import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402

try:
    from sqlalchemy import create_engine, text
except ImportError:  # pragma: no cover - environment guard
    sys.exit("This topic needs SQLAlchemy 2.0.\n"
             "  install: python3 -m pip install 'sqlalchemy>=2.0'")


def engine_url() -> str:
    dsn = lab_db.DSN
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    if dsn.startswith("postgresql:///"):
        return dsn.replace("postgresql:///", "postgresql+psycopg:///", 1)
    return f"postgresql+psycopg:///{dsn}"


def make_engine(pool_size: int, max_overflow: int, pool_timeout: float,
                app_name: str = "sep-pool", statement_timeout_ms: int | None = None):
    """A SQLAlchemy QueuePool with every knob stated explicitly.

    Stated explicitly on purpose: the defaults (pool_size=5, max_overflow=10,
    pool_timeout=30) are the numbers behind most real pool incidents, and
    writing them out is the first step to owning them. Total connections this
    engine can ever open is pool_size + max_overflow -- multiply that by workers
    and by replicas to get the number your database sees.
    """
    connect_args = {"application_name": app_name}
    if statement_timeout_ms is not None:
        # Set on the CONNECTION, not per request: one fewer round trip, and it is
        # where you would set it in production (on the role, or in the DSN).
        connect_args["options"] = f"-c statement_timeout={statement_timeout_ms}"
    return create_engine(
        engine_url(),
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        pool_pre_ping=False,        # an extra round trip per checkout; off, to measure the pool
        connect_args=connect_args,
    )


# The unit of work. A CPU-bound aggregate over the seeded orders table, with
# parallel query disabled per session, so that N concurrent requests genuinely
# contend for N cores' worth of Postgres and the database can be saturated. A
# pg_sleep() would have been easier and would have measured nothing: sleeping
# backends do not compete for CPU, so more connections would keep helping
# forever and the knee this topic is about would not exist.
WORK_SQL = text("""
    SELECT count(*), sum(total_cents)
    FROM orders
    WHERE total_cents > :threshold AND status <> :status
""")

SLOW_SQL = text("SELECT pg_sleep(:seconds), count(*) FROM orders WHERE id < 100")

NO_PARALLEL = text("SET max_parallel_workers_per_gather = 0")


def do_work(conn, threshold: int = 250_000) -> None:
    conn.execute(NO_PARALLEL)
    conn.execute(WORK_SQL, {"threshold": threshold, "status": "refunded"}).fetchone()


def do_slow(conn, seconds: float = 0.5) -> None:
    conn.execute(SLOW_SQL, {"seconds": seconds}).fetchone()


class Result:
    """Per-run collection. Latency is from SCHEDULED start, not from dispatch."""

    def __init__(self):
        self.latencies: list[float] = []
        self.errors: Counter = Counter()
        self.completed = 0
        self.attempts = 0
        self.lock = threading.Lock()

    def ok(self, scheduled_at: float) -> None:
        with self.lock:
            self.completed += 1
            self.latencies.append((time.perf_counter() - scheduled_at) * 1000)

    def fail(self, kind: str, scheduled_at: float) -> None:
        with self.lock:
            self.errors[kind] += 1
            self.latencies.append((time.perf_counter() - scheduled_at) * 1000)

    def summary(self, elapsed: float) -> dict:
        return {
            "completed": self.completed,
            "attempts": self.attempts,
            "rate": self.completed / elapsed if elapsed else 0.0,
            "p50": lab_db.percentile(self.latencies, 50),
            "p99": lab_db.percentile(self.latencies, 99),
            "errors": dict(self.errors),
            "error_count": sum(self.errors.values()),
        }


def classify(exc: BaseException) -> str:
    """Pool timeout, server rejection, or something else. The distinction is the
    finding in several of these experiments, so it is named rather than counted."""
    msg = str(exc)
    if "QueuePool limit" in msg or "connection pool" in msg.lower():
        return "pool timeout"
    if "too many clients" in msg:
        return "server: too many clients"
    if "canceling statement due to statement timeout" in msg:
        return "statement timeout"
    if "canceling statement due to user request" in msg:
        return "cancelled"
    return type(exc).__name__


def open_loop(engine, handler, rate: float, duration: float, result: Result,
              max_inflight: int = 4000) -> float:
    """Fire `rate` requests per second for `duration` seconds, regardless of
    how long they take, and return the wall time actually elapsed.

    `handler(engine, scheduled_at, result)` runs one request. Requests are
    dispatched to a large thread pool so that a slow request delays only itself;
    the pool is the thing under test, not the executor.
    """
    interval = 1.0 / rate
    stop_at = time.perf_counter() + duration
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max_inflight) as pool:
        next_at = started
        while next_at < stop_at:
            now = time.perf_counter()
            if next_at > now:
                time.sleep(next_at - now)
            with result.lock:
                result.attempts += 1
            pool.submit(handler, engine, next_at, result)
            next_at += interval
    return time.perf_counter() - started


def sample_activity(stop: threading.Event, samples: list, every: float = 0.25) -> None:
    """Sample what the SERVER thinks is happening, during the run.

    This is the key measurement of experiment 1: when throughput stops improving
    but p99 keeps climbing, the queue has moved out of your application -- where
    you can see it, bound it and shed it -- and into the database, where you
    cannot. `active` climbing while throughput is flat is that migration,
    happening.
    """
    conn = lab_db.connect()
    try:
        while not stop.is_set():
            rows = conn.execute(
                """
                SELECT state, coalesce(wait_event_type, '-') AS wait, count(*)
                FROM pg_stat_activity
                WHERE datname = current_database() AND pid <> pg_backend_pid()
                  AND application_name LIKE 'sep-%'
                GROUP BY 1, 2
                """
            ).fetchall()
            samples.append({(r[0], r[1]): r[2] for r in rows})
            time.sleep(every)
    finally:
        conn.close()


def mean_activity(samples: list) -> dict:
    """Average concurrent backends per (state, wait) across the samples."""
    if not samples:
        return {}
    totals: Counter = Counter()
    for s in samples:
        totals.update(s)
    return {k: v / len(samples) for k, v in totals.items()}


def activity_line(mean: dict) -> str:
    if not mean:
        return "-"
    active = sum(v for (state, _w), v in mean.items() if state == "active")
    idle_tx = sum(v for (state, _w), v in mean.items() if state == "idle in transaction")
    idle = sum(v for (state, _w), v in mean.items() if state == "idle")
    return f"{active:.1f} / {idle_tx:.1f} / {idle:.1f}"


def measure_service_time(engine, n: int = 12) -> float:
    """One request at a time, no contention: the service time Little's Law needs."""
    times = []
    with engine.connect() as conn:
        for _ in range(n):
            t0 = time.perf_counter()
            do_work(conn)
            times.append((time.perf_counter() - t0) * 1000)
    return lab_db.percentile(times, 50)


def prepare() -> None:
    lab_db.ensure_database()
    with lab_db.connect() as conn:
        lab_db.ensure_big_seed(conn)
