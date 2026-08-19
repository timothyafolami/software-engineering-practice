"""
Prepared statements through PgBouncer in transaction mode -- experiment 4b.

    MAX_PREPARED_STATEMENTS=200 docker compose -f lab/docker/compose.yml \
        --profile pooler up -d pgbouncer
    LAB_DSN=postgresql://lab:lab@127.0.0.1:6432/sep_lab_03_data \
        python3 07-connection-pools/python/pgbouncer_prepared.py

    # then the other way round, and compare:
    MAX_PREPARED_STATEMENTS=0 docker compose -f lab/docker/compose.yml \
        --profile pooler up -d --force-recreate pgbouncer

WHY THIS FILE EXISTS. Topic 7 experiment 4 has two halves. `pool_sweep.py`
pointed at port 6432 does the first half (large application-side pools stop
producing `too many clients`, because PgBouncer caps what reaches the server).
It CANNOT do the second half, and this is worth understanding before you read
its output as evidence: every request in `pool_sweep.py` runs inside a
transaction, and in transaction mode PgBouncer pins one server connection for
the whole of a transaction. A statement prepared and executed inside the same
transaction therefore always finds itself on the connection it was prepared on,
and nothing ever breaks -- at `max_prepared_statements = 0` just as much as at
200. Sweeping it and seeing no difference is not the same as measuring one.

The failure needs AUTOCOMMIT traffic: each statement is its own transaction, so
between the PREPARE and a later EXECUTE, PgBouncer is free to hand that client a
different server connection. That is what this program generates.

WHAT TO LOOK FOR: at max_prepared_statements = 0, psycopg 3 prepares after its
default `prepare_threshold` of 5 executions and the run dies with
`DuplicatePreparedStatement: prepared statement "_pg3_0" already exists` -- the
name is per-CLIENT, the server connection is shared, and two clients collide.
At 200, PgBouncer tracks the prepared statements itself and the identical code
completes. That is the whole of "asyncpg needs statement_cache_size = 0" being
obsolete on PgBouncer 1.21+, as a measurement rather than as advice.

Knobs: CLIENTS (default 25), ROUNDS (default 12).
"""
from __future__ import annotations

import os
import sys

try:
    import psycopg
except ImportError:  # pragma: no cover - environment guard
    sys.exit("This topic needs psycopg 3.\n"
             "  install: python3 -m pip install 'psycopg[binary]'")

DSN = os.environ.get("LAB_DSN", "postgresql://lab:lab@127.0.0.1:6432/sep_lab_03_data")
CLIENTS = int(os.environ.get("CLIENTS", "25"))
ROUNDS = int(os.environ.get("ROUNDS", "12"))

WORK = "SELECT count(*) FROM orders WHERE total_cents > %s"


def admin_dsn(dsn: str) -> str:
    """The same host and port, but PgBouncer's own admin database."""
    head, _, _ = dsn.rpartition("/")
    return f"{head}/pgbouncer"


def pgbouncer_config(dsn: str) -> dict[str, str] | None:
    try:
        with psycopg.connect(admin_dsn(dsn), autocommit=True, connect_timeout=5) as conn:
            version = conn.execute("SHOW VERSION").fetchone()[0]
            cfg = {r[0]: r[1] for r in conn.execute("SHOW CONFIG").fetchall()}
            cfg["version"] = version
            return cfg
    except Exception:
        return None


def run(dsn: str) -> tuple[int, str | None]:
    """Execute the same parameterised query from many autocommit clients."""
    conns = [psycopg.connect(dsn, autocommit=True) for _ in range(CLIENTS)]
    executed = 0
    try:
        for _ in range(ROUNDS):
            for conn in conns:
                conn.execute(WORK, (5000,)).fetchone()
                executed += 1
        return executed, None
    except Exception as exc:  # noqa: BLE001 - the error IS the result
        return executed, f"{type(exc).__name__}: {exc}"
    finally:
        for conn in conns:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def main() -> None:
    print("=" * 78)
    print("Prepared statements through a transaction-mode pooler")
    print("=" * 78)

    cfg = pgbouncer_config(DSN)
    if cfg is None:
        print(f"  dsn           {DSN}")
        print("\n  BLOCKED: nothing answering as PgBouncer on that host and port -- its")
        print("  admin database refused the connection, so this DSN is either a direct")
        print("  Postgres or a pooler whose ADMIN_USERS does not include this role.")
        print("  Pointed straight at Postgres this program proves nothing: a server")
        print("  connection is never shared, so a prepared statement cannot collide.")
        print("\n  unblock:")
        print("    docker compose -f lab/docker/compose.yml --profile pooler up -d pgbouncer")
        print("    LAB_DSN=postgresql://lab:lab@127.0.0.1:6432/sep_lab_03_data \\")
        print("      python3 07-connection-pools/python/pgbouncer_prepared.py")
        sys.exit(1)

    mps = cfg.get("max_prepared_statements", "?")
    print(f"  pooler        {cfg['version']}")
    print(f"  pool_mode     {cfg.get('pool_mode')}")
    print(f"  max_prepared_statements = {mps}")
    with psycopg.connect(DSN, autocommit=True) as probe:
        print(f"  driver        psycopg {psycopg.__version__}, "
              f"prepare_threshold = {probe.prepare_threshold}")
    print(f"  workload      {CLIENTS} autocommit clients x {ROUNDS} rounds of one "
          f"parameterised query")
    print("                (autocommit on purpose: a transaction would pin the server")
    print("                 connection and there would be nothing to collide with)")

    executed, error = run(DSN)
    print()
    print(f"  executions completed   {executed} of {CLIENTS * ROUNDS}")
    if error is None:
        print("  result                 no error")
        print()
        if mps == "0":
            print("  Unexpected at max_prepared_statements = 0. Either the driver never")
            print("  reached its prepare_threshold, or every client kept its own server")
            print("  connection because CLIENTS is below default_pool_size -- raise CLIENTS")
            print("  above it and run again before recording this as a clean result.")
        else:
            print(f"  PgBouncer is tracking prepared statements for {mps} entries per")
            print("  connection, so the driver's statement cache is safe to leave ON.")
            print("  Re-run at MAX_PREPARED_STATEMENTS=0 -- the same code should fail.")
    else:
        print(f"  result                 {error}")
        print()
        print("  That is the pooler handing a client a server connection on which its")
        print("  own statement name is already taken. The name is per-client, the")
        print("  connection is not. Re-run at MAX_PREPARED_STATEMENTS=200 -- the same")
        print("  code should complete.")


if __name__ == "__main__":
    main()
