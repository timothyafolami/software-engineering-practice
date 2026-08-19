"""
Shared lab helper for Layer 3 (data and databases).

WHY PYTHON: every topic in this layer is about what a real Postgres does under
concurrency, and the user's production stack is FastAPI + SQLAlchemy + psycopg.
The lab helper is therefore Python so that topic programs read like application
code, not like a benchmark harness.

WHAT THIS IS: the no-Docker path. The layer README describes a docker compose
stack (Postgres 18 primary + replica + pgbouncer + k6). The Docker daemon is not
running on this machine, so this module talks to whatever Postgres is already
listening locally and creates ONE scratch database for the whole layer.

  database name : sep_lab_03_data       (override with LAB_DSN)
  teardown      : python3 lab/local/teardown_lab.py

Every topic program calls ensure_* itself, so each program is runnable with a
single command and no hidden setup step. The first program you run pays for the
seed; the rest find it already there.

SCALE: LAB_SCALE=small (default) or LAB_SCALE=full. "full" is the layer README's
5M orders / 20M line_items. "small" is 1/5th of that, which is still far past the
few-thousand-row point where the planner starts making interesting decisions.
"""
from __future__ import annotations

import os
import sys
import time

try:
    import psycopg
except ImportError:  # pragma: no cover - environment guard, not logic
    sys.exit(
        "This layer needs psycopg 3.\n"
        "  install: python3 -m pip install 'psycopg[binary]'"
    )

DB_NAME = "sep_lab_03_data"
ADMIN_DSN = os.environ.get("LAB_ADMIN_DSN", "postgresql:///postgres")
DSN = os.environ.get("LAB_DSN", f"postgresql:///{DB_NAME}")

SCALES = {
    # scale name : (customers, orders, line_items_per_order)
    "small": (50_000, 1_000_000, 3),
    "full": (200_000, 5_000_000, 4),
}
SCALE = os.environ.get("LAB_SCALE", "small")

# The 20 countries are deliberately skewed: NG dominates, the tail is rare.
# Uniform data hides every interesting planner decision, so this is not cosmetic.
COUNTRY_WEIGHTS = [
    ("NG", 38), ("US", 14), ("GB", 9), ("GH", 7), ("KE", 6), ("ZA", 5),
    ("CA", 4), ("DE", 3), ("FR", 2), ("IN", 2), ("BR", 1), ("EG", 1),
    ("ES", 1), ("IE", 1), ("NL", 1), ("PT", 1), ("SE", 1), ("AE", 1),
    ("AU", 1), ("JP", 1),
]
# A 100-slot lookup table: pick a slot per row and you get the weights above.
COUNTRY_SLOTS = [c for c, w in COUNTRY_WEIGHTS for _ in range(w)]
assert len(COUNTRY_SLOTS) == 100


def connect(dsn: str = DSN, autocommit: bool = True) -> "psycopg.Connection":
    conn = psycopg.connect(dsn, autocommit=autocommit)
    conn.execute("SET application_name = 'sep-layer3-lab'")
    return conn


def tune_session(conn: "psycopg.Connection") -> None:
    """Per-session config the layer README asks for.

    Set at session scope on purpose: this lab must never edit the postgresql.conf
    of a Postgres you are using for something else. random_page_cost = 4.0 is the
    default and assumes spinning rust; on an SSD it distorts every plan in this
    layer, so every program that reads a plan sets 1.1 first.
    """
    conn.execute("SET random_page_cost = 1.1")
    conn.execute("SET effective_cache_size = '1GB'")
    try:
        conn.execute("SET track_io_timing = on")  # superuser-only on some builds
    except psycopg.errors.InsufficientPrivilege:
        conn.rollback()


def server_version(conn: "psycopg.Connection") -> int:
    """Numeric server version, e.g. 170005 for 17.5, 180000 for 18.0."""
    return int(conn.execute("SHOW server_version_num").fetchone()[0])


def describe_server(conn: "psycopg.Connection") -> str:
    v = conn.execute("SELECT version()").fetchone()[0]
    return v.split(" on ")[0]


def ensure_database() -> None:
    """Create the scratch database if it does not exist. Idempotent."""
    try:
        connect().close()
        return
    except psycopg.OperationalError:
        pass
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,)
        ).fetchone()
        if not exists:
            admin.execute(f'CREATE DATABASE "{DB_NAME}"')
            print(f"[lab] created database {DB_NAME}")


CORE_DDL = """
CREATE TABLE IF NOT EXISTS accounts (
    id            bigint PRIMARY KEY,
    balance_cents bigint NOT NULL
);

CREATE TABLE IF NOT EXISTS oncall (
    shift_id  int     NOT NULL,
    doctor_id int     NOT NULL,
    on_call   boolean NOT NULL,
    PRIMARY KEY (shift_id, doctor_id)
);

CREATE TABLE IF NOT EXISTS jobs (
    id          bigserial PRIMARY KEY,
    payload     text        NOT NULL,
    state       text        NOT NULL DEFAULT 'ready',
    claimed_by  text,
    claimed_at  timestamptz
);
"""


def ensure_core_tables(conn: "psycopg.Connection") -> None:
    """accounts / oncall / jobs: the small contention tables. Cheap, always safe."""
    conn.execute(CORE_DDL)


def reset_accounts(conn: "psycopg.Connection", n: int = 10_000, balance: int = 100_000) -> None:
    ensure_core_tables(conn)
    conn.execute("TRUNCATE accounts")
    conn.execute(
        "INSERT INTO accounts (id, balance_cents) "
        "SELECT g, %s FROM generate_series(1, %s) g",
        (balance, n),
    )


def reset_oncall(conn: "psycopg.Connection", shifts: int = 100) -> None:
    """100 shifts x 2 doctors, everybody on call. Invariant: >= 1 per shift."""
    ensure_core_tables(conn)
    conn.execute("TRUNCATE oncall")
    conn.execute(
        "INSERT INTO oncall (shift_id, doctor_id, on_call) "
        "SELECT s, d, true FROM generate_series(1, %s) s, generate_series(1, 2) d",
        (shifts,),
    )
    conn.execute("ANALYZE oncall")


BIG_DDL = """
CREATE TABLE IF NOT EXISTS customers (
    id      bigint PRIMARY KEY,
    email   text   NOT NULL UNIQUE,
    country text   NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id          bigint      PRIMARY KEY,
    customer_id bigint      NOT NULL REFERENCES customers(id),
    status      text        NOT NULL,
    total_cents bigint      NOT NULL,
    created_at  timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS line_items (
    id          bigint PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES orders(id),
    sku         text   NOT NULL,
    qty         int    NOT NULL,
    price_cents bigint NOT NULL
);
"""


def _row_count(conn: "psycopg.Connection", table: str) -> int:
    exists = conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0]
    if exists is None:
        return 0
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def ensure_big_seed(conn: "psycopg.Connection", scale: str = SCALE, quiet: bool = False) -> None:
    """customers / orders / line_items, generated in SQL. Idempotent.

    Generated with generate_series rather than through an ORM because seeding 1M
    rows one INSERT at a time is the slowest possible way to do it and teaches
    nothing. Deliberately skewed: ~92% of orders are 'complete', country is
    heavily weighted to NG.
    """
    n_customers, n_orders, items_per_order = SCALES[scale]
    conn.execute(BIG_DDL)
    have = _row_count(conn, "orders")
    if have >= n_orders:
        return

    say = (lambda *a: None) if quiet else print
    say(f"[lab] seeding scale={scale}: {n_customers:,} customers, "
        f"{n_orders:,} orders, ~{n_orders * items_per_order:,} line items")
    say("[lab] this runs once; later programs find it already seeded")

    t0 = time.perf_counter()
    conn.execute("TRUNCATE line_items, orders, customers RESTART IDENTITY CASCADE")
    conn.execute(
        """
        INSERT INTO customers (id, email, country)
        SELECT g,
               'user' || g || '@example.com',
               (%s::text[])[1 + mod(g * 7919, 100)]
        FROM generate_series(1, %s) g
        """,
        (COUNTRY_SLOTS, n_customers),
    )
    say(f"[lab]   customers   {time.perf_counter() - t0:6.1f}s")

    t1 = time.perf_counter()
    conn.execute(
        """
        INSERT INTO orders (id, customer_id, status, total_cents, created_at)
        SELECT g,
               1 + mod(g * 2654435761::bigint, %s),
               CASE WHEN mod(g, 100) < 92 THEN 'complete'
                    WHEN mod(g, 100) < 96 THEN 'pending'
                    WHEN mod(g, 100) < 99 THEN 'refunded'
                    ELSE 'failed' END,
               100 + mod(g::bigint * 7919, 500000),
               timestamptz '2023-01-01' + (g::float8 / %s) * interval '3 years'
        FROM generate_series(1, %s) g
        """,
        (n_customers, n_orders, n_orders),
    )
    say(f"[lab]   orders      {time.perf_counter() - t1:6.1f}s")

    t2 = time.perf_counter()
    conn.execute(
        """
        INSERT INTO line_items (id, order_id, sku, qty, price_cents)
        SELECT (o.id - 1) * %s + i,
               o.id,
               'SKU-' || lpad(((mod(o.id * 31 + i, 5000)))::text, 5, '0'),
               1 + mod(i, 3),
               50 + mod(o.id * 13 + i, 20000)
        FROM orders o, generate_series(1, %s) i
        """,
        (items_per_order, items_per_order),
    )
    say(f"[lab]   line_items  {time.perf_counter() - t2:6.1f}s")

    t3 = time.perf_counter()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_line_items_order ON line_items(order_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id)")
    say(f"[lab]   base index  {time.perf_counter() - t3:6.1f}s")

    # ANALYZE after seeding, every time. Half the "the planner ignored my index"
    # bugs in the world are stale statistics.
    t4 = time.perf_counter()
    conn.execute("ANALYZE customers, orders, line_items")
    say(f"[lab]   ANALYZE     {time.perf_counter() - t4:6.1f}s")
    say(f"[lab] seed complete in {time.perf_counter() - t0:.1f}s")


def table_bytes(conn: "psycopg.Connection", table: str) -> int:
    return conn.execute("SELECT pg_total_relation_size(%s)", (table,)).fetchone()[0]


def human_bytes(n: int) -> str:
    for unit in ("B", "kB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n} B"


def percentile(values, q: float) -> float:
    """Nearest-rank percentile. Small samples make interpolation a lie."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(q / 100.0 * len(ordered) + 0.5)) - 1))
    return ordered[k]


def banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------------------
# Concurrency helpers.
#
# Every anomaly in this layer is load-shaped: none of them reproduce from one
# connection running statements in order. These helpers exist so each topic
# program can produce real concurrency without re-inventing a thread harness.
# ---------------------------------------------------------------------------

import threading  # noqa: E402  (grouped with the helpers that use it)
from concurrent.futures import ThreadPoolExecutor  # noqa: E402


class Worker:
    """One thread, one connection. A pool slot, in the shape your app has."""

    def __init__(self, name: str, dsn: str = DSN, isolation: str | None = None):
        self.name = name
        self.conn = connect(dsn, autocommit=True)
        # SET does not take bound parameters; set_config() is the parameterised form.
        self.conn.execute("SELECT set_config('application_name', %s, false)", (f"sep-{name}",))
        if isolation:
            # Session default, so every BEGIN in this worker inherits it. The
            # topic programs verify it took effect rather than trusting it.
            self.conn.execute(f"SET default_transaction_isolation = '{isolation}'")

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001 - teardown must not mask results
            pass


def run_workers(n: int, body, *, isolation: str | None = None, dsn: str = DSN):
    """Start n workers, hand each `body(worker, index)`, collect the results.

    Connections are created before any work starts, so the measured window does
    not include connection setup (Layer 2 measured that; here it is noise).
    """
    workers = [Worker(f"w{i}", dsn=dsn, isolation=isolation) for i in range(n)]
    try:
        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = [pool.submit(body, w, i) for i, w in enumerate(workers)]
            return [f.result() for f in futures]
    finally:
        for w in workers:
            w.close()


def sqlstate(exc: BaseException) -> str | None:
    """SQLSTATE of a psycopg error, or None. 40001 = serialization failure,
    40P01 = deadlock detected, 55P03 = lock_not_available."""
    return getattr(exc, "sqlstate", None) or getattr(getattr(exc, "orig", None), "sqlstate", None)


# ---------------------------------------------------------------------------
# EXPLAIN helpers.
# ---------------------------------------------------------------------------

def explain(conn: "psycopg.Connection", sql: str, params=None, analyze: bool = True) -> dict:
    """Return the top plan node as a dict (EXPLAIN ... FORMAT JSON).

    BUFFERS is requested explicitly. PG18 turns it on by default with ANALYZE;
    asking for it anyway is what makes these programs work on 15/16/17 too.
    """
    opts = "ANALYZE, BUFFERS, VERBOSE, FORMAT JSON" if analyze else "BUFFERS, FORMAT JSON"
    row = conn.execute(f"EXPLAIN ({opts}) {sql}", params).fetchone()[0]
    return row[0] if isinstance(row, list) else row


def walk_plan(node: dict):
    """Yield every node in a plan tree, parents before children."""
    yield node
    for child in node.get("Plans", []) or []:
        yield from walk_plan(child)


def plan_root(explained: dict) -> dict:
    return explained["Plan"]


def scan_summary(explained: dict) -> str:
    """One line naming the scan/join nodes in the order the planner nested them."""
    names = [n["Node Type"] for n in walk_plan(plan_root(explained))]
    return " -> ".join(names)


def node_by_type(explained: dict, *types: str) -> dict | None:
    for n in walk_plan(plan_root(explained)):
        if n["Node Type"] in types:
            return n
    return None


def total_buffers(explained: dict) -> tuple[int, int]:
    hit = read = 0
    for n in walk_plan(plan_root(explained)):
        hit += n.get("Shared Hit Blocks", 0) or 0
        read += n.get("Shared Read Blocks", 0) or 0
    return hit, read


def rows_removed(explained: dict) -> int:
    return sum((n.get("Rows Removed by Filter", 0) or 0) * (n.get("Actual Loops", 1) or 1)
               for n in walk_plan(plan_root(explained)))


def index_searches(explained: dict) -> int | None:
    """PG18 reports `Index Searches: N` per index node. None on older servers."""
    found = [n.get("Index Searches") for n in walk_plan(plan_root(explained))
             if n.get("Index Searches") is not None]
    return sum(found) if found else None


# ---------------------------------------------------------------------------
# Capability gates. A blocked experiment must say so and print its unblock
# command; it must never fail silently or, worse, print a made-up number.
# ---------------------------------------------------------------------------

def gate(label: str, ok: bool, unblock: str) -> bool:
    print(f"  [{'ok     ' if ok else 'BLOCKED'}] {label}")
    if not ok:
        print(f"            unblock: {unblock}")
    return ok


def has_extension(conn: "psycopg.Connection", name: str) -> bool:
    row = conn.execute("SELECT 1 FROM pg_extension WHERE extname = %s", (name,)).fetchone()
    if row:
        return True
    preload = conn.execute("SHOW shared_preload_libraries").fetchone()[0]
    if name not in preload:
        return False
    try:
        conn.execute(f"CREATE EXTENSION IF NOT EXISTS {name}")
        return True
    except Exception:  # noqa: BLE001
        return False


def table_stats(conn: "psycopg.Connection", table: str) -> dict:
    row = conn.execute(
        """
        SELECT n_live_tup, n_dead_tup, n_tup_upd, n_tup_hot_upd,
               COALESCE(last_autovacuum, last_vacuum) AS last_vac
        FROM pg_stat_user_tables WHERE relname = %s
        """,
        (table,),
    ).fetchone()
    if not row:
        return {"live": 0, "dead": 0, "upd": 0, "hot": 0, "last_vac": None}
    return {"live": row[0], "dead": row[1], "upd": row[2], "hot": row[3], "last_vac": row[4]}
