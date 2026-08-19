"""
Shared Postgres helper for Layer 4 (distributed systems).

WHY THIS EXISTS: the layer README is written against a `docker compose` stack --
postgres:18 primary + streaming standby, Toxiproxy, Redpanda, etcd, k6. The
Docker daemon is not running on this machine and k6 is not installed, so the
Postgres-backed topics (2, 4, 6, 7) talk to whatever Postgres is already
listening locally and create ONE scratch database for the whole layer.

  database name : sep_lab_04_dist       (override with LAB_DSN)
  teardown      : python3 lab/local/teardown_lab.py
  what runs here: python3 lab/local/check_env.py

Every topic program calls the ensure_* function it needs, so each program stays
runnable with a single command and no hidden setup step.

DELIBERATE NON-GOAL: this helper never edits postgresql.conf and never touches a
database it did not create. Everything it changes is either inside
sep_lab_04_dist or session-scoped.
"""
from __future__ import annotations

import os
import sys

try:
    import psycopg
except ImportError:  # pragma: no cover - environment guard, not logic
    sys.exit(
        "Layer 4's Postgres topics need psycopg 3.\n"
        "  install: python3 -m pip install 'psycopg[binary]'"
    )

DB_NAME = "sep_lab_04_dist"
ADMIN_DSN = os.environ.get("LAB_ADMIN_DSN", "postgresql:///postgres")
DSN = os.environ.get("LAB_DSN", f"postgresql:///{DB_NAME}")


def connect(dsn: str = DSN, autocommit: bool = True) -> "psycopg.Connection":
    conn = psycopg.connect(dsn, autocommit=autocommit)
    conn.execute("SET application_name = 'sep-layer4-lab'")
    return conn


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


def open_lab(reset_sql: str = "", ddl: str = "") -> "psycopg.Connection":
    """The three lines every topic program starts with, in one call."""
    ensure_database()
    conn = connect()
    if ddl:
        conn.execute(ddl)
    if reset_sql:
        conn.execute(reset_sql)
    return conn


def server_version(conn: "psycopg.Connection") -> int:
    """Numeric server version, e.g. 170005 for 17.5, 180000 for 18.0."""
    return int(conn.execute("SHOW server_version_num").fetchone()[0])


def describe_server(conn: "psycopg.Connection") -> str:
    return conn.execute("SELECT version()").fetchone()[0].split(" on ")[0]


def percentile(values, q: float) -> float:
    """Nearest-rank percentile. Small samples, so no interpolation games."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * len(ordered) + 0.5)) - 1))
    return ordered[idx]


def banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def section(title: str) -> None:
    print()
    print("-" * 78)
    print(title)
    print("-" * 78)
