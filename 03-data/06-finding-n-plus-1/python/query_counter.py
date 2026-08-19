"""
Counting queries per request, and the CI gate that stops the next one.

    python3 06-finding-n-plus-1/python/query_counter.py

WHAT IT DEMONSTRATES: N+1 is not detectable by reading code -- the loop and the
query live in different files, often in different layers, and the diff shows
neither. It IS detectable by counting statements per request, which is
mechanical, cheap, and enforceable in CI.

The endpoint below is deliberately ordinary: list some orders, and for each one
show the customer's email and how many line items it has. Nothing about it looks
like a database problem. It issues 2N+1 queries.

WHAT TO LOOK FOR:
  * queries/request against `limit`. LINEAR IN THE RESULT SIZE is the
    fingerprint -- that shape, not the absolute number, is what you learn to
    recognise. Ten rows in development is 21 queries and feels instant; ten
    thousand rows in production is 20,001 and is your p99.
  * the identity-map section. The same endpoint scoped to ONE customer collapses
    from 2N+1 to N+2, because the Session serves repeat access to the same row
    for free. A naive demonstration that happens to use one customer shows
    almost no problem at all -- real behaviour, wrong experiment.
  * the CI gate at the end. That is the deliverable; everything above it is how
    you learned to trust it.

Knobs: LIMITS (comma-separated), BUDGET (the gate's query budget).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orm_lab  # noqa: E402
from orm_lab import Order  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402

LIMITS = [int(x) for x in os.environ.get("LIMITS", "10,100,1000").split(",")]
BUDGET = int(os.environ.get("BUDGET", "3"))


def endpoint_naive(session, limit: int) -> list[dict]:
    """GET /orders?limit=N -- the version everybody writes first.

    Two lazy relationship accesses inside one loop. Neither line looks like a
    query. Both are.
    """
    orders = session.scalars(select(Order).order_by(Order.id).limit(limit)).all()
    return [
        {
            "id": o.id,
            "status": o.status,
            "customer_email": o.customer.email,   # <- one query, per order
            "items": len(o.line_items),           # <- one query, per order
        }
        for o in orders
    ]


def endpoint_one_customer(session, customer_id: int, limit: int) -> list[dict]:
    """The same shape, scoped to a single customer.

    Included because it is the most common way this experiment gets set up
    wrong: every order here has the SAME customer, so the Session's identity map
    answers every `o.customer` access after the first from memory, and the
    customer half of the N+1 vanishes. The line_items half does not.
    """
    orders = session.scalars(
        select(Order).where(Order.customer_id == customer_id).order_by(Order.id).limit(limit)
    ).all()
    return [{"id": o.id, "customer_email": o.customer.email, "items": len(o.line_items)}
            for o in orders]


def endpoint_fixed(session, limit: int) -> list[dict]:
    """The same endpoint, three queries regardless of `limit`.

    joinedload for the many-to-one (one customer per order: a JOIN adds no rows),
    selectinload for the one-to-many (many items per order: a JOIN would multiply
    them). That pairing is the rule, and lazy_vs_eager.py measures why.
    """
    from sqlalchemy.orm import joinedload
    orders = session.scalars(
        select(Order)
        .options(joinedload(Order.customer), selectinload(Order.line_items))
        .order_by(Order.id).limit(limit)
    ).unique().all()
    return [{"id": o.id, "status": o.status, "customer_email": o.customer.email,
             "items": len(o.line_items)} for o in orders]


def measure(engine, counter, fn, *args) -> dict:
    with orm_lab.new_session(engine) as session:
        with counter.request() as state:
            fn(session, *args)
    return dict(state)


def queries_for(engine, counter, fn, *args) -> int:
    """The one function a test needs. This is the whole gate."""
    return measure(engine, counter, fn, *args)["count"]


def show(label: str, state: dict, limit: int) -> None:
    print(f"  {label:<28}{limit:>7}{state['count']:>11,}{state['rows']:>16,}"
          f"{state['ms']:>11.1f}")


def main() -> None:
    engine = orm_lab.make_engine()
    counter = orm_lab.QueryCounter(engine)
    lab_db.banner("Queries per request -- the number you should own")

    print("The endpoint: list N orders, each with its customer's email and its item")
    print("count. Nothing in it looks like a database problem.\n")
    print(f"  {'variant':<28}{'limit':>7}{'queries':>11}{'rows over wire':>16}{'ms':>11}")
    print("  " + "-" * 72)

    naive = {}
    for limit in LIMITS:
        state = measure(engine, counter, endpoint_naive, limit)
        naive[limit] = state
        show("lazy (as written)", state, limit)
    for limit in LIMITS:
        show("fixed (eager loading)", measure(engine, counter, endpoint_fixed, limit), limit)

    print("\n  the shape, which is the actual lesson:")
    for limit in LIMITS:
        print(f"    limit {limit:<6} {naive[limit]['count']:>6,} queries "
              f"= 2 x {limit} + 1")
    print("    Linear in the result size. It cannot trip a threshold in development,")
    print("    because in development the result size is ten.")
    print()
    print("    The fixed variant is 2 queries at limit 10 and 100 and 3 at limit 1000 --")
    print("    not because anything went wrong, but because selectinload splits its")
    print("    `WHERE id IN (...)` list into chunks (500 by default) rather than sending")
    print("    one enormous IN list. Constant-ish, not literally constant, and the")
    print("    difference is worth knowing before it surprises you in a budget assertion.")

    print("\n  the first three statements of the lazy run at limit "
          f"{LIMITS[0]}, as the driver saw them:")
    for sql, rows, ms in naive[LIMITS[0]]["queries"][:3]:
        print(f"    {rows:>5} rows {ms:>7.2f}ms  {sql[:74]}")
    print("    The second and third are the loop. Nobody wrote a call for them.")

    # -----------------------------------------------------------------------
    # The identity map: why a careless demonstration shows nothing.
    # -----------------------------------------------------------------------
    print("\n  the same endpoint scoped to ONE customer (the identity map at work):")
    print(f"  {'variant':<28}{'limit':>7}{'queries':>11}{'rows over wire':>16}{'ms':>11}")
    for limit in (10, 20):
        state = measure(engine, counter, endpoint_one_customer, 4242, limit)
        show("lazy, single customer", state, limit)
    print("    Roughly N+2 instead of 2N+1: the customer is loaded once and every later")
    print("    `o.customer` is served from the Session's identity map. If your reproduction")
    print("    shows a flat query count as `limit` grows, check this first -- you need")
    print("    DISTINCT related rows for the effect to exist at all.")

    # -----------------------------------------------------------------------
    # Detection method 2: pg_stat_statements sorted by calls, not by time.
    # -----------------------------------------------------------------------
    print("\n  detection method 2 -- pg_stat_statements ORDER BY calls:")
    with lab_db.connect() as conn:
        have = lab_db.has_extension(conn, "pg_stat_statements")
        lab_db.gate(
            "pg_stat_statements", have,
            "psql -d postgres -c \"ALTER SYSTEM SET shared_preload_libraries = "
            "'pg_stat_statements';\" && brew services restart postgresql@17 && "
            "psql -d sep_lab_03_data -c 'CREATE EXTENSION pg_stat_statements;'")
        if have:
            conn.execute("SELECT pg_stat_statements_reset()")
            measure(engine, counter, endpoint_naive, 200)
            rows = conn.execute(
                "SELECT calls, round(mean_exec_time::numeric, 4), left(query, 62) "
                "FROM pg_stat_statements ORDER BY calls DESC LIMIT 3").fetchall()
            print(f"    {'calls':>8}{'mean ms':>10}  query")
            for calls, mean, query in rows:
                print(f"    {calls:>8,}{mean:>10}  {' '.join(query.split())}")
            print("    An N+1 is a query with an enormous `calls` count and a TINY mean.")
            print("    Sorting by total_exec_time finds it only when the total happens to")
            print("    reach the top. Sorting by calls always does.")
        else:
            print("    Without the extension, the counter above is the detection method you")
            print("    have, and it is the one that belongs in CI anyway. The production")
            print("    half of this is Topic 4's production_triage.py.")

    # -----------------------------------------------------------------------
    # The deliverable: the gate.
    # -----------------------------------------------------------------------
    print(f"\n  the CI gate (budget: {BUDGET} queries for GET /orders?limit=100):")
    for label, fn in (("lazy (as written)", endpoint_naive), ("fixed", endpoint_fixed)):
        n = queries_for(engine, counter, fn, 100)
        verdict = "PASS" if n <= BUDGET else "FAIL"
        print(f"    assert queries_for({label:<20}) <= {BUDGET}  ->  {n:>5} queries  {verdict}")
    print("""
    The pytest version is four lines:

        def test_orders_query_budget(engine, counter):
            n = queries_for(engine, counter, endpoint, limit=100)
            assert n <= 3, f"query budget blown: {n} queries"

    Make it a TEST GATE, not a dashboard. A regression should fail the build,
    because a dashboard nobody is paid to watch is a dashboard nobody watches --
    and this particular regression is one relationship access, added by someone
    who had no reason to think they were adding a query.""")

    engine.dispose()


if __name__ == "__main__":
    main()
