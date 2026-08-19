"""
The exact selectivity at which the planner changes its mind -- and the hidden
column statistic that decides it.

    python3 04-reading-a-query-plan/python/flip_threshold.py

WHAT IT DEMONSTRATES: one query shape -- a range over an indexed column -- asked
for a widening slice of the table, bisected to find the exact percentage where
the plan changes:

    index scan  ->  bitmap heap scan  ->  seq scan

and then the same bisection run against a SECOND indexed column of the same
table, with the same data type distribution and the same row count, where the
answer comes out completely different.

THE HIDDEN VARIABLE, which is the real content of this program:
`pg_stats.correlation` -- how closely the column's sort order matches the
table's physical row order. The lab seeds `orders` in created_at order, so
created_at has correlation ~1.0: an index scan on it reads the heap almost
sequentially and stays cheap far past where anyone expects. total_cents is
scattered across the heap by the seed's own arithmetic, correlation ~0.0, and an
index scan on it means genuinely random heap access, so it gives up much sooner.

Same table. Same index type. Same query shape. Two very different answers, and
neither of them is in the query.

WHAT TO LOOK FOR:
  * the correlation figures printed first, then the two thresholds under them;
  * what happens when random_page_cost goes from 1.1 to the 4.0 default. 4.0
    assumes a seek penalty an SSD does not have. One number in a config file,
    moving which plan a million-row query gets.

The middle state is the one people misread. A bitmap heap scan is not a degraded
index scan: it builds a bitmap of matching heap PAGES from the index, sorts it,
and reads the heap in physical order, converting random I/O into sequential
I/O. It means "too many rows for an index scan, too few for a seq scan", and it
is very often exactly right.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402

# (column, index name, the payload column the query also needs from the heap).
# The payload matters: an index-only scan never touches the heap and so never
# flips where a real query flips.
COLUMNS = [
    ("created_at",  "idx_flip_created", "total_cents"),
    ("total_cents", "idx_flip_total",   "customer_id"),
]

SWEEP = [0.01, 0.1, 1, 5, 10, 20, 40]


def sql_for(column: str, payload: str) -> str:
    return f"SELECT count(*), sum({payload}) FROM orders WHERE {column} >= %s"


def boundary_for(conn, column: str, fraction_pct: float):
    """The value that makes `column >= value` return this share of the table.

    Read out of the data with percentile_disc rather than computed from the
    seed's formula, so this keeps working if the seed changes shape.
    """
    q = 1.0 - fraction_pct / 100.0
    return conn.execute(
        f"SELECT percentile_disc(%s) WITHIN GROUP (ORDER BY {column}) FROM orders", (q,)
    ).fetchone()[0]


def classify(conn, column: str, payload: str, boundary) -> str:
    """Which of the three plan families the planner picked.

    EXPLAIN without ANALYZE: we are asking what it would choose, and choosing is
    free. Bisection needs a hundred of these, so making each one execute the
    query would turn a two-second answer into a two-minute one.
    """
    ex = lab_db.explain(conn, sql_for(column, payload), (boundary,), analyze=False)
    types = [n["Node Type"] for n in lab_db.walk_plan(lab_db.plan_root(ex))]
    if any("Bitmap" in t for t in types):
        return "bitmap"
    if any(t.startswith("Index") for t in types):
        return "index"
    return "seq"


def measure(conn, column: str, payload: str, boundary):
    sql = sql_for(column, payload)
    for _ in range(2):
        lab_db.explain(conn, sql, (boundary,))
    ex = lab_db.explain(conn, sql, (boundary,))
    hit, read = lab_db.total_buffers(ex)
    return lab_db.scan_summary(ex), ex["Execution Time"], hit, read


def bisect(conn, column: str, payload: str, lo_pct: float, hi_pct: float,
           lo_kind: str, tol: float = 0.01) -> float | None:
    """Smallest selectivity in (lo, hi] whose plan is no longer `lo_kind`.

    None means the plan never changes across the interval. That happens, and it
    is a result rather than a failure -- see the correlation discussion above.
    """
    if classify(conn, column, payload, boundary_for(conn, column, hi_pct)) == lo_kind:
        return None
    while hi_pct - lo_pct > tol:
        mid = (lo_pct + hi_pct) / 2
        if classify(conn, column, payload, boundary_for(conn, column, mid)) == lo_kind:
            lo_pct = mid
        else:
            hi_pct = mid
    return hi_pct


def rows_at(conn, column: str, pct: float) -> int:
    return conn.execute(
        f"SELECT count(*) FROM orders WHERE {column} >= %s", (boundary_for(conn, column, pct),)
    ).fetchone()[0]


def sweep(conn, column: str, payload: str) -> None:
    print(f"\n  sweep over {column}:")
    print(f"  {'selectivity':>12}{'rows':>10}  {'plan':<44}{'buf hit/read':>18}{'exec ms':>10}")
    print("  " + "-" * 96)
    for pct in SWEEP:
        boundary = boundary_for(conn, column, pct)
        plan, ms, hit, read = measure(conn, column, payload, boundary)
        rows = rows_at(conn, column, pct)
        print(f"  {pct:>11.2f}%{rows:>10,}  {plan[:42]:<44}{hit:>10,}/{read:<7,}{ms:>10.2f}")


def thresholds(conn, column: str, payload: str) -> list[str]:
    """Bisect every transition from the smallest slice up to 60%."""
    out = []
    pct = 0.001
    kind = classify(conn, column, payload, boundary_for(conn, column, pct))
    out.append(f"    plan at 0.001%: {kind}")
    for _ in range(2):   # at most two transitions exist: index -> bitmap -> seq
        nxt = bisect(conn, column, payload, pct, 60.0, kind)
        if nxt is None:
            out.append(f"    `{kind}` holds all the way to 60% -- no further transition.")
            break
        new_kind = classify(conn, column, payload, boundary_for(conn, column, nxt))
        out.append(f"    {kind:>6} -> {new_kind:<6} at {nxt:>6.2f}%   "
                   f"({rows_at(conn, column, nxt):,} rows)")
        pct, kind = nxt, new_kind
    return out


def main() -> None:
    lab_db.ensure_database()
    with lab_db.connect() as conn:
        lab_db.tune_session(conn)
        lab_db.banner(f"Where the planner changes its mind -- {lab_db.describe_server(conn)}")
        lab_db.ensure_big_seed(conn)

        for column, index, _payload in COLUMNS:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {index} ON orders ({column})")
        conn.execute("ANALYZE orders")

        total = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
        print(f"orders: {total:,} rows, "
              f"{lab_db.human_bytes(lab_db.table_bytes(conn, 'orders'))} with indexes")
        print("\nPredict both numbers before you scroll: at what percentage of the table does")
        print("this query stop using an index scan, and at what percentage does it stop")
        print("touching the index at all? Predict them once, then look at the correlation")
        print("figures below and decide whether you want to change your answer.")

        print("\n  pg_stats.correlation -- how well each column's order matches the heap's:")
        for column, _index, _payload in COLUMNS:
            corr = conn.execute(
                "SELECT correlation FROM pg_stats WHERE tablename = 'orders' AND attname = %s",
                (column,)).fetchone()[0]
            print(f"    {column:<14}{corr:>8.4f}   "
                  f"{'physically ordered -- index scan reads the heap sequentially'
                     if abs(corr) > 0.9 else
                     'scattered -- index scan means random heap access'}")

        try:
            for column, _index, payload in COLUMNS:
                sweep(conn, column, payload)

            print("\n  bisected transitions, random_page_cost = 1.1 (SSD):")
            for column, _index, payload in COLUMNS:
                print(f"  {column}:")
                for line in thresholds(conn, column, payload):
                    print(line)

            conn.execute("SET random_page_cost = 4.0")
            print("\n  the same bisection, random_page_cost = 4.0 (the default, and a"
                  " spinning disk):")
            for column, _index, payload in COLUMNS:
                print(f"  {column}:")
                for line in thresholds(conn, column, payload):
                    print(line)
            conn.execute("SET random_page_cost = 1.1")

            print("\n  Three settings decided every number above, and none of them is in the")
            print("  query: random_page_cost, effective_cache_size, and the correlation")
            print("  statistic ANALYZE measured for you. Check the first two with SHOW in the")
            print("  session that ran the query -- they are session-scoped, and reading them in")
            print("  a different psql window is exactly how people end up explaining a plan they")
            print("  did not get.")
            print()
            print("  If a column shows no bitmap stage at all, that is not a broken run. A")
            print("  perfectly correlated column has no random I/O for the bitmap stage to")
            print("  rescue it from, so the planner goes straight from index scan to seq scan.")
        finally:
            for _column, index, _payload in COLUMNS:
                conn.execute(f"DROP INDEX IF EXISTS {index}")
            print("\n(indexes dropped -- this program leaves the lab as it found it)")


if __name__ == "__main__":
    main()
