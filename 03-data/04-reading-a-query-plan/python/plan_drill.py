"""
The plan-reading drill: ten queries, predicted first, then explained.

    python3 04-reading-a-query-plan/python/plan_drill.py

WHAT IT DEMONSTRATES: the ten query shapes in `sql/plans/`, each run under
EXPLAIN (ANALYZE, BUFFERS) and reduced to the four numbers you actually read a
plan with:

  est vs act    the estimated and actual row counts of the top node, and the
                ratio between them. A large ratio is the root cause more often
                than everything else combined, because every choice made
                downstream of that node was made on the strength of the estimate.
  loops         reported node times are PER LOOP. `actual time=0.02 loops=2000000`
                cost forty seconds, not 0.02ms. The `max loops` column here is
                the largest loop count anywhere in the plan; when it is large,
                go read that node's per-loop time and multiply.
  buffers       shared hit is cache, shared read is disk. Two identical-looking
                plans with wildly different times usually differ here.
  removed       Rows Removed by Filter: work done and then thrown away.

HOW TO USE IT: open `sql/plans/`, read the header of each file, and write your
predicted plan down BEFORE running this. The header of every file tells you what
to predict and deliberately does not tell you the answer. Scoring yourself is
the entire exercise -- the table this program prints is worth very little if you
did not commit to an answer first.

WHAT TO LOOK FOR: rows 6 and 7 are the same query with and without extended
statistics, and row 8's two lines are the same query with and without an
expression index. Those three pairs are the ones where a plan changed because
you told the planner something true, rather than because you forced it.

This program creates an index on orders(created_at), a small correlated table,
one extended-statistics object and one expression index, and drops all of them
again on the way out.
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402

PLANS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sql", "plans")

CREATED_AT_INDEX = "idx_plan_drill_orders_created"
EMAIL_EXPR_INDEX = "idx_plan_drill_lower_email"
CORR_TABLE = "plan_drill_corr"
CORR_STATS = "plan_drill_corr_stats"


# ---------------------------------------------------------------------------
# Prerequisites. Created here rather than in the lab seed because they belong to
# this topic: the lab's base schema deliberately has NO index on created_at, so
# that Topic 3 can add one and watch it matter.
# ---------------------------------------------------------------------------

def build_prereqs(conn) -> None:
    conn.execute(f"CREATE INDEX IF NOT EXISTS {CREATED_AT_INDEX} ON orders (created_at)")

    # A correlated pair, on purpose, and strongly enough that the arithmetic is
    # easy to check by hand: 'NG' is 10% of rows, 'failed' is 5% of rows, and
    # EVERY failed row is an NG row. Assuming independence gives
    # 0.10 x 0.05 x 500,000 = 2,500 rows. The true answer is 25,000 -- ten times
    # more -- because once you have filtered to 'failed' the country predicate
    # removes nothing at all.
    #
    # Why a purpose-built table rather than the lab's own country/status columns:
    # those live on DIFFERENT tables (customers.country, orders.status), and
    # extended statistics are per-table. A cross-table correlation like that one
    # cannot be repaired this way at all, which is a real limit worth knowing --
    # see the note printed at the end of this program.
    conn.execute(f"DROP STATISTICS IF EXISTS {CORR_STATS}")
    conn.execute(f"DROP TABLE IF EXISTS {CORR_TABLE}")
    conn.execute(
        f"""
        CREATE TABLE {CORR_TABLE} AS
        SELECT g AS id,
               CASE WHEN mod(g, 100) < 10 THEN 'NG'
                    WHEN mod(g, 100) < 40 THEN 'US'
                    WHEN mod(g, 100) < 70 THEN 'GB'
                    ELSE 'KE' END AS country,
               CASE WHEN mod(g, 100) < 5 THEN 'failed'      -- every failure is an NG row
                    ELSE 'complete' END AS status,
               100 + mod(g::bigint * 7919, 90000) AS amount_cents
        FROM generate_series(1, 500000) g
        """
    )
    conn.execute(f"ANALYZE {CORR_TABLE}")


def drop_prereqs(conn) -> None:
    conn.execute(f"DROP INDEX IF EXISTS {CREATED_AT_INDEX}")
    conn.execute(f"DROP INDEX IF EXISTS {EMAIL_EXPR_INDEX}")
    conn.execute(f"DROP STATISTICS IF EXISTS {CORR_STATS}")
    conn.execute(f"DROP TABLE IF EXISTS {CORR_TABLE}")


# ---------------------------------------------------------------------------
# Running one plan file. Everything before the `-- @explain` marker is setup and
# is executed as-is; everything after it is the one statement we measure. The
# marker is a SQL comment, so each file also runs unchanged in psql.
# ---------------------------------------------------------------------------

def split_plan_file(text: str) -> tuple[str, str]:
    if "-- @explain" not in text:
        return "", text
    setup, query = text.split("-- @explain", 1)
    setup_sql = "\n".join(
        line for line in setup.splitlines() if not line.strip().startswith("--")
    ).strip()
    return setup_sql, query.strip()


def max_loops(explained: dict) -> int:
    return max((n.get("Actual Loops", 1) or 1) for n in lab_db.walk_plan(lab_db.plan_root(explained)))


def worst_estimate(explained: dict) -> tuple[str, int, int, float]:
    """The node whose estimate is furthest from reality, and by how much.

    Reading the ROOT node's est-vs-actual is a trap: the root of nearly every
    query here is an Aggregate returning exactly one row, estimated at exactly
    one row, which looks perfect no matter how wrong the plan underneath it is.
    The number you want is the worst estimate anywhere in the tree -- that node
    is where the plan went wrong, and everything above it inherited the mistake.

    Both `Plan Rows` and `Actual Rows` are per-loop, so they are directly
    comparable without touching `Actual Loops`.
    """
    worst = ("-", 0, 0, 1.0)
    for n in lab_db.walk_plan(lab_db.plan_root(explained)):
        est = n.get("Plan Rows") or 0
        act = n.get("Actual Rows") or 0
        if max(est, act) < 50:      # noise: a 2-vs-1 row node is not a finding
            continue
        ratio = max(est, act) / max(min(est, act), 1)
        if ratio > worst[3] or (ratio == worst[3] and act > worst[2]):
            worst = (n["Node Type"], est, act, ratio)
    if worst[0] == "-":
        root = lab_db.plan_root(explained)
        return (root["Node Type"], root.get("Plan Rows") or 0, root.get("Actual Rows") or 0, 1.0)
    return worst


def measure(conn, sql: str) -> dict:
    # Three runs, keep the warm one. The first run measures the cache, not the
    # plan, and a cold first run is the most common way to conclude the wrong
    # thing from this whole exercise.
    for _ in range(2):
        lab_db.explain(conn, sql)
    ex = lab_db.explain(conn, sql)
    hit, read = lab_db.total_buffers(ex)
    node, est, act, ratio = worst_estimate(ex)
    return {
        "plan": lab_db.scan_summary(ex),
        "node": node,
        "est": est,
        "act": act,
        "ratio": ratio,
        "loops": max_loops(ex),
        "hit": hit,
        "read": read,
        "removed": lab_db.rows_removed(ex),
        "plan_ms": ex.get("Planning Time", float("nan")),
        "exec_ms": ex["Execution Time"],
    }


# est/act are 11 and 12 wide, not 9 and 9: six-figure row counts with thousands
# separators overflow a 9-column field and the two numbers run together
# ("173,125138,889.0"), which is unreadable exactly on the rows where the
# estimate matters most.
HEADER = (f"  {'#':<3}{'query':<25}{'worst-estimated node':<21}{'est':>11}{'act':>12}"
          f"{'off by':>8}{'loops':>8}{'buf hit/read':>18}{'removed':>10}{'exec ms':>9}")


def row(label: str, n: str, r: dict) -> str:
    off = "-" if r["ratio"] < 1.5 else f"{r['ratio']:.0f}x"
    return (f"  {n:<3}{label:<25}{r['node'][:20]:<21}{r['est']:>11,}{r['act']:>12,.1f}"
            f"{off:>8}{r['loops']:>8,}{r['hit']:>10,}/{r['read']:<7,}{r['removed']:>10,}"
            f"{r['exec_ms']:>9.2f}")


# ---------------------------------------------------------------------------

def run_drill(conn) -> list[tuple[str, str, dict]]:
    files = sorted(f for f in os.listdir(PLANS_DIR) if f.endswith(".sql"))
    results = []
    for fname in files:
        num, label = fname[:-4].split("_", 1)
        with open(os.path.join(PLANS_DIR, fname)) as fh:
            setup, query = split_plan_file(fh.read())
        if setup:
            conn.execute(setup)
        results.append((num, label.replace("_", " "), measure(conn, query)))
    return results


def query_8_repaired(conn) -> dict:
    """Same query as plan 8, with an index that actually stores lower(email)."""
    conn.execute(f"CREATE INDEX IF NOT EXISTS {EMAIL_EXPR_INDEX} ON customers (lower(email))")
    conn.execute("ANALYZE customers")
    return measure(conn, "SELECT id, country FROM customers WHERE lower(email) = 'user4242@example.com'")


def cast_from_the_driver(conn) -> None:
    """Plan 9, but reached the way an application reaches it: through a bound
    parameter whose Python type decides the SQL type.

    The negative result here is the useful part. Everybody repeats "a string
    bound to an integer column kills your index"; with psycopg3 that is not
    true, because a `str` goes to the server as `unknown` and the server
    resolves it to bigint. A `Decimal` or a `float` carries a real type, and
    that type is what forces the cast onto the column.
    """
    print("\n  the same cast miss, reached through a bound parameter:")
    print(f"    {'python type':<14}{'predicate the server ended up with':<46}{'plan':<22}{'ms':>8}")
    cases = [
        ("int", 424242),
        ("str", "424242"),
        ("Decimal", Decimal("424242")),
        ("float", 424242.0),
    ]
    sql = "SELECT count(*) FROM orders WHERE id = %s"
    for name, value in cases:
        for _ in range(2):
            lab_db.explain(conn, sql, (value,))
        ex = lab_db.explain(conn, sql, (value,))
        node = lab_db.node_by_type(ex, "Index Scan", "Index Only Scan", "Seq Scan",
                                   "Parallel Seq Scan", "Bitmap Index Scan") or lab_db.plan_root(ex)
        pred = node.get("Index Cond") or node.get("Filter") or "-"
        kind = node["Node Type"]
        print(f"    {name:<14}{pred[:44]:<46}{kind:<22}{ex['Execution Time']:>8.2f}")
    print("    A `str` does NOT reproduce the bug on psycopg3. A `Decimal` -- which is")
    print("    what a JSON body or a pydantic model hands you -- does, every time.")


def join_strategy_flip(conn) -> None:
    """Plan 5's query at three date ranges, so you watch the join strategy change.

    Nothing about the query changes across these three runs. The number of rows
    coming out of `orders` changes, and the planner picks a different join
    algorithm because of it. That is what "the query got slow and nothing
    changed" looks like from the inside.
    """
    print("\n  the same join at three range widths -- only the row count changes:")
    print(f"    {'range':<16}{'orders rows':>12}  {'join':<14}{'exec ms':>9}")
    ranges = [("1 month", "2024-06-01", "2024-07-01"),
              ("6 months", "2024-01-01", "2024-07-01"),
              ("12 months", "2024-01-01", "2025-01-01")]
    sql = """
        SELECT o.status, count(*) AS items, sum(li.price_cents * li.qty) AS cents
        FROM orders o JOIN line_items li ON li.order_id = o.id
        WHERE o.created_at >= %s::timestamptz AND o.created_at < %s::timestamptz
        GROUP BY o.status
    """
    for label, lo, hi in ranges:
        for _ in range(2):
            lab_db.explain(conn, sql, (lo, hi))
        ex = lab_db.explain(conn, sql, (lo, hi))
        joins = [n["Node Type"] for n in lab_db.walk_plan(lab_db.plan_root(ex))
                 if "Join" in n["Node Type"] or n["Node Type"] == "Nested Loop"]
        n_orders = conn.execute(
            "SELECT count(*) FROM orders WHERE created_at >= %s::timestamptz "
            "AND created_at < %s::timestamptz", (lo, hi)).fetchone()[0]
        print(f"    {label:<16}{n_orders:>12,}  {(joins[0] if joins else '-'):<14}"
              f"{ex['Execution Time']:>9.2f}")


def main() -> None:
    lab_db.ensure_database()
    with lab_db.connect() as conn:
        lab_db.tune_session(conn)
        lab_db.banner(f"Plan-reading drill -- {lab_db.describe_server(conn)}")
        lab_db.ensure_big_seed(conn)

        rpc = conn.execute("SHOW random_page_cost").fetchone()[0]
        print(f"random_page_cost = {rpc} in THIS session (the 4.0 default would distort "
              f"every decision below)")
        if lab_db.server_version(conn) < 180000:
            print("Server is older than PG18: BUFFERS is not on by default, so these plans ask")
            print("for it explicitly. `Index Searches` is not reported at all before 18.")
        print("\nWrite your ten predictions down before reading any further. The file headers")
        print(f"in {os.path.relpath(PLANS_DIR)} tell you what to predict and not what the answer is.")

        build_prereqs(conn)
        try:
            results = run_drill(conn)
            print()
            print(HEADER)
            print("  " + "-" * 125)
            for num, label, r in results:
                print(row(label, num, r))

            r8b = query_8_repaired(conn)
            print(row("function on column FIXED", "8b", r8b))

            print("\n  the plan each one chose:")
            for num, label, r in results:
                print(f"    {num} {label:<26}{r['plan']}")
            print(f"    8b {'function on column FIXED':<26}{r8b['plan']}")

            join_strategy_flip(conn)
            cast_from_the_driver(conn)

            print("\n  Read rows 6 and 7 against each other first. Same query, same data, same")
            print("  server -- the only difference is that the planner was told the two columns")
            print("  are not independent, and the estimate moves from ~10x low to about right.")
            print("  Then rows 2 and 3: same shape, same index, opposite decision, and the only")
            print("  variable is how many rows come back.")
            print()
            print("  One limit worth carrying away: CREATE STATISTICS is per-TABLE. The lab's")
            print("  own correlated pair -- customers.country and orders.status -- spans two")
            print("  tables, and no extended statistics object can repair a join selectivity")
            print("  estimate across them. The fixes there are different ones: denormalise the")
            print("  column, or restructure the query so the correlated pair lands on one side.")
        finally:
            drop_prereqs(conn)
            print("\n(prerequisites dropped -- this program leaves the lab as it found it)")


if __name__ == "__main__":
    main()
