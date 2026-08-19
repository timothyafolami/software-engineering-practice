"""
Composite index column order: what the sort order lets the index actually do.

    python3 03-indexes/python/column_order.py

WHAT IT DEMONSTRATES: two indexes over the same three columns in different
orders, and four query shapes run against them. A B-tree on (a, b, c) is sorted
by a, then b within equal a, then c. Everything about which predicates it can
serve falls out of that one fact:

  WHERE a = ? AND b = ?   contiguous range          -> both are Index Cond
  WHERE a = ? AND c = ?   finds the a range, then FILTERS every row in it
  WHERE a > ? AND b = ?   the range on a breaks the sort for b; only a bounds it
  WHERE b = ?            leading column missing -- the index cannot start anywhere

WHAT TO LOOK FOR: the `Cond / Filter` column and `rows removed`. "The index was
used" is not the question. The question is how far into the index the scan got
before it had to start throwing rows away, and `Rows Removed by Filter` is that
number.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402

IDX_A = "idx_orders_cust_status_created"   # (customer_id, status, created_at)
IDX_B = "idx_orders_status_cust_created"   # (status, customer_id, created_at)

QUERIES = [
    ("eq + eq",
     "SELECT count(*), max(total_cents) FROM orders WHERE customer_id = %s AND status = %s",
     (4242, "pending")),
    ("eq + range",
     "SELECT count(*), max(total_cents) FROM orders WHERE customer_id = %s AND created_at > %s",
     (4242, "2025-01-01")),
    ("range + eq",
     "SELECT count(*), max(total_cents) FROM orders WHERE customer_id > %s AND status = %s",
     (49_900, "pending")),
    ("second column only",
     "SELECT count(*), max(total_cents) FROM orders WHERE status = %s AND created_at > %s",
     ("failed", "2025-06-01")),
]


def index_node(explained: dict) -> dict | None:
    return lab_db.node_by_type(explained, "Index Scan", "Index Only Scan", "Bitmap Index Scan",
                               "Seq Scan", "Index Scan Backward")


def describe(explained: dict) -> dict:
    node = index_node(explained) or lab_db.plan_root(explained)
    cond = node.get("Index Cond") or "-"
    filt = node.get("Filter") or node.get("Recheck Cond") or "-"
    hit, read = lab_db.total_buffers(explained)
    return {
        "scan": lab_db.scan_summary(explained),
        "index": node.get("Index Name", "-"),
        "cond": cond,
        "filter": filt,
        "removed": lab_db.rows_removed(explained),
        "searches": lab_db.index_searches(explained),
        "buffers": f"{hit}/{read}",
        "ms": explained["Execution Time"],
        "est": lab_db.plan_root(explained).get("Plan Rows"),
        "act": lab_db.plan_root(explained).get("Actual Rows"),
    }


def run(conn, sql: str, params) -> dict:
    # Three times, keep the warm one: the first run measures the cache, not the plan.
    for _ in range(2):
        lab_db.explain(conn, sql, params)
    return describe(lab_db.explain(conn, sql, params))


def show(conn, title: str, only_index: str | None) -> None:
    print(f"\n{title}")
    print(f"  {'query':<20}{'index used':<34}{'rows removed':>13}{'searches':>10}"
          f"{'buf hit/read':>14}{'ms':>9}")
    print("  " + "-" * 100)
    results = []
    for label, sql, params in QUERIES:
        r = run(conn, sql, params)
        results.append((label, r))
        searches = "-" if r["searches"] is None else str(r["searches"])
        print(f"  {label:<20}{r['index']:<34}{r['removed']:>13,}{searches:>10}"
              f"{r['buffers']:>14}{r['ms']:>9.1f}")
    for label, r in results:
        print(f"    {label}:")
        print(f"      plan       {r['scan']}")
        print(f"      Index Cond {r['cond']}")
        print(f"      Filter     {r['filter']}")


def main() -> None:
    lab_db.ensure_database()
    with lab_db.connect() as conn:
        lab_db.tune_session(conn)
        lab_db.banner(f"Composite index column order -- {lab_db.describe_server(conn)}")
        lab_db.ensure_big_seed(conn)
        n = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
        print(f"orders: {n:,} rows. status is ~92% 'complete', ~1% 'failed'.")
        if lab_db.index_searches(lab_db.explain(conn, "SELECT count(*) FROM orders WHERE id = 1")) is None:
            print("This server does not report `Index Searches` (PG18+ only), so that column is '-'.")

        conn.execute(f"DROP INDEX IF EXISTS {IDX_A}")
        conn.execute(f"DROP INDEX IF EXISTS {IDX_B}")

        conn.execute(f"CREATE INDEX {IDX_A} ON orders (customer_id, status, created_at)")
        conn.execute("ANALYZE orders")
        show(conn, f"A. only {IDX_A} (customer_id, status, created_at)", IDX_A)

        conn.execute(f"DROP INDEX {IDX_A}")
        conn.execute(f"CREATE INDEX {IDX_B} ON orders (status, customer_id, created_at)")
        conn.execute("ANALYZE orders")
        show(conn, f"B. only {IDX_B} (status, customer_id, created_at)", IDX_B)

        conn.execute(f"CREATE INDEX {IDX_A} ON orders (customer_id, status, created_at)")
        conn.execute("ANALYZE orders")
        show(conn, "C. both present -- now the planner chooses", None)

        conn.execute(f"DROP INDEX IF EXISTS {IDX_A}")
        conn.execute(f"DROP INDEX IF EXISTS {IDX_B}")
        print("\n(indexes dropped again -- this program leaves `orders` as it found it)")
        print()
        print("The rule these four rows are evidence for: equality columns first, then the one")
        print("range column, then anything along for the ride.")
        print()
        print("Read that in the BUFFERS column, not in Cond-vs-Filter. Every qual above is an")
        print("Index Cond and every `rows removed` is 0, because a B-tree evaluates a qual on")
        print("any indexed column INSIDE the index -- `Filter` is for quals the index cannot")
        print("evaluate at all, which is why the only Filter here sits under a Seq Scan. What")
        print("a leading range costs you is how much of the index gets walked: compare")
        print("`eq + range` with `range + eq` on the same index in section A.")


if __name__ == "__main__":
    main()
