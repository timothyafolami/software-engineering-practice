"""
The 3am workflow: what is slow, why is it slow, who called it.

    python3 04-reading-a-query-plan/python/production_triage.py

WHAT IT DEMONSTRATES: the three tools that let you answer those questions on a
server you cannot attach a debugger to, with data you cannot reproduce.

  pg_stat_statements   WHAT is slow. One row per normalised statement, with
                       calls, total time and mean time. Sort it two ways --
                       by total_exec_time for the expensive ones, by calls for
                       the N+1s (Topic 6's finding, and the sort people skip).
  auto_explain         WHY it is slow. Logs the actual PLAN of any statement
                       over a duration threshold, in production, without you
                       being there. It can be loaded into ONE SESSION with
                       `LOAD 'auto_explain'` -- no restart, no config change --
                       which is the single most useful thing to know about it.
  SQLCommenter         WHO called it. A structured comment appended to the SQL
                       (/*controller='orders',route='/orders'*/) so the slow
                       statement carries its own callsite. Django has it as
                       SQLCOMMENT; SQLAlchemy gets it from a
                       before_cursor_execute hook; here it is fifteen lines,
                       because seeing it done by hand is worth more than a
                       framework flag.

THE CAVEAT THAT BITES: pg_stat_statements normalises PARAMETERS but not
COMMENTS. Put a dynamic value in the comment -- a request id, a user id, a
timestamp -- and every request becomes its own row, your statement cache blows
out, and the tool you installed to find the slow query is now the slow query.
Tag with low-cardinality things only: controller, route, action.

WHAT TO LOOK FOR: the same workload ranked by total time and by calls. The two
orderings disagree, and the disagreement is the point.

BLOCKED, HONESTLY: pg_stat_statements needs shared_preload_libraries and a
server restart. If it is not loaded, this program prints the exact command and
falls back to measuring the same thing client-side -- clearly labelled, because
a client-side substitute is not the same tool. It cannot see statements from
other processes, and in production that is most of them.
"""
from __future__ import annotations

import os
import random
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402

REQUESTS = int(os.environ.get("REQUESTS", "300"))

# The workload: one cheap statement issued constantly, one expensive statement
# issued rarely. That shape is what makes the two orderings disagree.
ROUTES = [
    # (controller, route, weight, sql, params_fn)
    ("orders", "/orders/{id}", 70,
     "SELECT id, status, total_cents FROM orders WHERE id = %s",
     lambda: (random.randint(1, 1_000_000),)),
    ("orders", "/orders/{id}/items", 20,
     "SELECT li.sku, li.qty FROM line_items li WHERE li.order_id = %s",
     lambda: (random.randint(1, 1_000_000),)),
    ("reports", "/reports/revenue", 3,
     "SELECT status, count(*), sum(total_cents) FROM orders "
     "WHERE created_at >= %s::timestamptz GROUP BY status",
     lambda: ("2024-01-01",)),
    ("customers", "/customers/search", 7,
     "SELECT id, country FROM customers WHERE lower(email) = %s",
     lambda: (f"user{random.randint(1, 50_000)}@example.com",)),
]


def sqlcommenter(sql: str, controller: str, route: str) -> str:
    """Append a SQLCommenter tag.

    Deliberately low-cardinality: controller and route only. Adding a request id
    here would give pg_stat_statements a new row per request, which is the
    failure mode described at the top of this file.
    """
    return f"{sql} /*controller='{controller}',route='{route}'*/"


def pick_route():
    total = sum(r[2] for r in ROUTES)
    x = random.randint(1, total)
    for route in ROUTES:
        x -= route[2]
        if x <= 0:
            return route
    return ROUTES[-1]


def run_workload(conn) -> dict:
    """Issue REQUESTS statements and time each one client-side.

    The client-side timing is the fallback ranking AND the honest comparison
    for the server-side one: they measure different things (this one includes
    the round trip and result handling), and seeing them differ is useful.
    """
    stats = defaultdict(lambda: {"calls": 0, "total_ms": 0.0, "sql": "", "route": ""})
    for _ in range(REQUESTS):
        controller, route, _w, sql, params_fn = pick_route()
        tagged = sqlcommenter(sql, controller, route)
        t0 = time.perf_counter()
        conn.execute(tagged, params_fn()).fetchall()
        dt = (time.perf_counter() - t0) * 1000
        key = f"{controller}:{route}"
        s = stats[key]
        s["calls"] += 1
        s["total_ms"] += dt
        s["sql"] = sql
        s["route"] = route
    return stats


def show_client_side(stats: dict) -> None:
    print("\n  CLIENT-SIDE substitute (this process only -- not the real tool):")
    for order_by, label in (("total_ms", "total time"), ("calls", "calls")):
        print(f"\n    ranked by {label}:")
        print(f"      {'route':<30}{'calls':>7}{'total ms':>11}{'mean ms':>10}")
        for key, s in sorted(stats.items(), key=lambda kv: -kv[1][order_by])[:5]:
            print(f"      {key:<30}{s['calls']:>7,}{s['total_ms']:>11.1f}"
                  f"{s['total_ms'] / s['calls']:>10.3f}")


def show_pg_stat_statements(conn) -> None:
    for order_by, label in (("total_exec_time", "total_exec_time"), ("calls", "calls")):
        rows = conn.execute(
            f"""
            SELECT calls, total_exec_time, mean_exec_time, query
            FROM pg_stat_statements
            WHERE query LIKE '%%controller=%%'
            ORDER BY {order_by} DESC LIMIT 5
            """
        ).fetchall()
        print(f"\n    pg_stat_statements ordered by {label}:")
        print(f"      {'calls':>7}{'total ms':>11}{'mean ms':>10}  query")
        for calls, total, mean, query in rows:
            one_line = " ".join(query.split())
            print(f"      {calls:>7,}{total:>11.1f}{mean:>10.3f}  {one_line[:70]}")
    print("\n    The two orderings disagree, and that is the whole reason to run both.")
    print("    total_exec_time finds the expensive statement. `calls` finds the cheap")
    print("    statement issued ten thousand times -- which is an N+1, has a tiny mean,")
    print("    and can sit below the fold of a total-time ranking indefinitely.")
    print("    The /*controller=...*/ tag survived normalisation, so each row still")
    print("    names the route that emitted it.")


def demo_auto_explain(conn) -> None:
    """auto_explain, loaded into this one session. No restart, no config edit.

    This is the part worth remembering: `LOAD 'auto_explain'` works in any
    session where the .so is installed, which is every standard Postgres
    package. You can turn plan logging on for one connection, reproduce the
    slow request through it, and turn it off, without touching the server.
    """
    print("\n  auto_explain:")
    try:
        conn.execute("LOAD 'auto_explain'")
    except Exception as exc:  # noqa: BLE001 - reporting, not control flow
        print(f"    BLOCKED: {exc}")
        print("    unblock: install the postgresql-contrib package for your server")
        return

    conn.execute("SET auto_explain.log_min_duration = '10ms'")
    conn.execute("SET auto_explain.log_analyze = on")
    conn.execute("SET auto_explain.log_buffers = on")
    conn.execute("SET auto_explain.log_nested_statements = on")

    dest = conn.execute("SHOW log_destination").fetchone()[0]
    collector = conn.execute("SHOW logging_collector").fetchone()[0]
    logdir = conn.execute("SHOW log_directory").fetchone()[0]
    print("    loaded into THIS session only. No restart, no postgresql.conf edit.")
    print("    thresholds: log_min_duration=10ms, log_analyze=on, log_buffers=on")

    # Something guaranteed to exceed the threshold.
    conn.execute("SELECT count(*), sum(total_cents) FROM orders "
                 "WHERE lower(status) = 'complete' /*controller='reports',route='/slow'*/"
                 ).fetchall()

    print("    a >10ms statement has just been run; its full plan is now in the server")
    print(f"    log (log_destination={dest}, logging_collector={collector}, "
          f"log_directory={logdir}).")
    print("    On Homebrew that is usually /opt/homebrew/var/log/postgresql@NN.log;")
    print("    `SHOW data_directory` plus log_directory locates it on any install.")
    print("    In production this is the difference between having the plan of the")
    print("    slow request and having a theory about it.")


def main() -> None:
    lab_db.ensure_database()
    with lab_db.connect() as conn:
        lab_db.tune_session(conn)
        lab_db.banner(f"Production triage -- {lab_db.describe_server(conn)}")
        lab_db.ensure_big_seed(conn)

        print("  three questions, three tools:")
        print("    what is slow  -> pg_stat_statements")
        print("    why is it slow -> auto_explain")
        print("    who called it  -> SQLCommenter tags in the SQL itself")

        preload = conn.execute("SHOW shared_preload_libraries").fetchone()[0]
        have_pgss = lab_db.has_extension(conn, "pg_stat_statements")
        print(f"\n  shared_preload_libraries = '{preload or ''}'")
        lab_db.gate(
            "pg_stat_statements", have_pgss,
            "psql -d postgres -c \"ALTER SYSTEM SET shared_preload_libraries = "
            "'pg_stat_statements';\" && brew services restart postgresql@17 && "
            "psql -d sep_lab_03_data -c 'CREATE EXTENSION pg_stat_statements;'")

        if have_pgss:
            conn.execute("SELECT pg_stat_statements_reset()")

        print(f"\n  running {REQUESTS} tagged requests across "
              f"{len(ROUTES)} routes, weighted so the cheap one dominates...")
        stats = run_workload(conn)

        example = sqlcommenter("SELECT id FROM orders WHERE id = %s", "orders", "/orders/{id}")
        print(f"\n  what actually went over the wire:\n    {example}")

        if have_pgss:
            show_pg_stat_statements(conn)
        else:
            print("\n  pg_stat_statements is not loaded, so the ranking below is a")
            print("  CLIENT-SIDE substitute. It measures this process only. In production")
            print("  the statements you most need to find were issued by a process you are")
            print("  not attached to, which is exactly why the real tool lives in the server.")
        show_client_side(stats)

        demo_auto_explain(conn)

        print("\n  The workflow, in the order you actually use it:")
        print("    1. pg_stat_statements ORDER BY total_exec_time  -- and again by calls")
        print("    2. the /*controller=...*/ tag on the winning row -- that is the callsite")
        print("    3. auto_explain's logged plan for that statement -- that is the why")
        print("  Three steps, no reproduction, no debugger, and it works at 3am.")


if __name__ == "__main__":
    main()
