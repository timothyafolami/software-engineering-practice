"""
Four ways to load the same data, and the row arithmetic that chooses between them.

    python3 06-finding-n-plus-1/python/lazy_vs_eager.py

WHAT IT DEMONSTRATES: the same endpoint -- N orders, each with its customer and
its line items -- loaded four ways:

  lazy          the default. 2N+1 queries.
  selectinload  one extra query per relationship, `WHERE id IN (...)`.
  joinedload    one query, one JOIN, and for a ONE-TO-MANY relationship it
                multiplies rows: 1,000 orders x 3 line items is 3,000 rows to
                send and then de-duplicate in Python.
  single join   the query you would write by hand, returning exactly the
                columns the response needs and no ORM objects at all.

WHAT TO LOOK FOR: the `rows over wire` column, and then the timings. Work the
row arithmetic out on paper BEFORE running this -- it is the number that
explains the result, and doing it by hand once is what makes the rule
memorable rather than looked-up.

  joinedload on a one-to-many: limit x items_per_order rows
  selectinload on the same:    limit + (limit x items_per_order) rows, but as
                               two flat result sets with no de-duplication

THE RULE, DERIVED RATHER THAN MEMORISED: `joinedload` for many-to-one (one
customer per order, so a JOIN adds no rows), `selectinload` for one-to-many (many
items per order, so a JOIN multiplies them). Django has the same distinction
under different names: select_related vs prefetch_related.

THE DEEPER POINT: the fix is not "always eager load". That produces enormous
queries that are their own problem and moves the cost from the database into
your serialiser. The fix is KNOWING THE NUMBER AND CHOOSING.

Knobs: LIMITS (comma-separated), REPEATS.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orm_lab  # noqa: E402
from orm_lab import LineItem, Order  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import joinedload, selectinload  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402

LIMITS = [int(x) for x in os.environ.get("LIMITS", "100,1000").split(",")]
REPEATS = int(os.environ.get("REPEATS", "7"))


def v_lazy(session, limit: int):
    orders = session.scalars(select(Order).order_by(Order.id).limit(limit)).all()
    return [(o.id, o.customer.email, len(o.line_items)) for o in orders]


def v_selectinload(session, limit: int):
    orders = session.scalars(
        select(Order)
        .options(selectinload(Order.customer), selectinload(Order.line_items))
        .order_by(Order.id).limit(limit)
    ).all()
    return [(o.id, o.customer.email, len(o.line_items)) for o in orders]


def v_joinedload(session, limit: int):
    """joinedload on BOTH relationships -- including the one-to-many, on purpose.

    `.unique()` is not optional here and SQLAlchemy 2.0 will raise without it:
    the JOIN returns one row per line item, so the same Order object comes back
    three times and the ORM makes you acknowledge the de-duplication. That
    requirement is the API telling you what this loader strategy costs.
    """
    orders = session.scalars(
        select(Order)
        .options(joinedload(Order.customer), joinedload(Order.line_items))
        .order_by(Order.id).limit(limit)
    ).unique().all()
    return [(o.id, o.customer.email, len(o.line_items)) for o in orders]


def v_single_join(session, limit: int):
    """No ORM objects at all: one query returning exactly the response's shape.

    Often the right answer for a read endpoint, and always the baseline the
    other three should be compared against. It is also the variant that cannot
    accidentally acquire an N+1 six months from now.
    """
    sub = select(Order.id).order_by(Order.id).limit(limit).subquery()
    stmt = (
        select(Order.id, orm_lab.Customer.email, func.count(LineItem.id))
        .join(orm_lab.Customer, orm_lab.Customer.id == Order.customer_id)
        .join(LineItem, LineItem.order_id == Order.id)
        .join(sub, sub.c.id == Order.id)
        .group_by(Order.id, orm_lab.Customer.email)
    )
    return session.execute(stmt).all()


VARIANTS = [
    ("lazy", v_lazy),
    ("selectinload", v_selectinload),
    ("joinedload", v_joinedload),
    ("single join", v_single_join),
]


def measure(engine, counter, fn, limit: int) -> dict:
    latencies = []
    state = {}
    for _ in range(REPEATS):
        with orm_lab.new_session(engine) as session:
            with counter.request() as st:
                t0 = time.perf_counter()
                fn(session, limit)
                latencies.append((time.perf_counter() - t0) * 1000)
            state = dict(st)
    return {
        "queries": state["count"],
        "rows": state["rows"],
        "p50": lab_db.percentile(latencies, 50),
        "p99": lab_db.percentile(latencies, 99),
        "min": min(latencies),
    }


# ---------------------------------------------------------------------------
# The deeper nesting, where joinedload finally loses.
#
# orders -> line_items fans out by 3 in this seed, which turns out not to be
# enough for row multiplication to beat a single round trip. customers ->
# orders -> line_items fans out by ~20 and then by 3 again, so each CUSTOMER
# row is repeated ~60 times in the joined result -- twenty times the
# duplication of the first table, on the same hardware and the same data.
#
# Whether that is enough to reverse the ordering on YOUR machine is a question
# this program answers rather than assumes, and the answer is noisy enough that
# it is worth running twice. The transferable part is not which one wins here:
# it is that "fewer queries" and "less work" are different quantities, and the
# rows-over-wire column is the one that tracks the second.
# ---------------------------------------------------------------------------

NESTED_LIMITS = [int(x) for x in os.environ.get("NESTED_LIMITS", "200,500").split(",")]


def n_joinedload(session, limit: int):
    customers = session.scalars(
        select(orm_lab.Customer)
        .options(joinedload(orm_lab.Customer.orders).joinedload(Order.line_items))
        .order_by(orm_lab.Customer.id).limit(limit)
    ).unique().all()
    return sum(len(o.line_items) for c in customers for o in c.orders)


def n_selectinload(session, limit: int):
    customers = session.scalars(
        select(orm_lab.Customer)
        .options(selectinload(orm_lab.Customer.orders).selectinload(Order.line_items))
        .order_by(orm_lab.Customer.id).limit(limit)
    ).all()
    return sum(len(o.line_items) for c in customers for o in c.orders)


def nested_comparison(engine, counter) -> None:
    print("\n  the same two strategies one level deeper -- customers -> orders -> items,")
    print("  where each customer row is repeated ~60 times by the JOIN:")
    print(f"  {'variant':<16}{'customers':>10}{'queries':>10}{'rows over wire':>16}"
          f"{'p50 ms':>10}{'best ms':>10}")
    print("  " + "-" * 74)
    out = {}
    for limit in NESTED_LIMITS:
        for label, fn in (("joinedload", n_joinedload), ("selectinload", n_selectinload)):
            r = measure(engine, counter, fn, limit)
            out[(label, limit)] = r
            print(f"  {label:<16}{limit:>10}{r['queries']:>10,}{r['rows']:>16,}"
                  f"{r['p50']:>10.1f}{r['min']:>10.1f}")
    biggest = NESTED_LIMITS[-1]
    j, s_ = out[("joinedload", biggest)], out[("selectinload", biggest)]
    print()
    if j["p50"] > s_["p50"]:
        print(f"  At {biggest} customers joinedload is {j['p50'] / max(s_['p50'], 1e-9):.2f}x slower than")
        print(f"  selectinload -- with ONE query against {s_['queries']}, and "
              f"{j['rows']:,} rows against {s_['rows']:,}.")
        print("  Fewer queries, more work. That is the whole point: query count is a proxy")
        print("  for cost, and it stops being a good one exactly here.")
    else:
        print(f"  joinedload still wins at {biggest} customers on this machine "
              f"({j['p50']:.0f}ms vs {s_['p50']:.0f}ms).")
        print("  Push NESTED_LIMITS higher until it reverses, and note the number: that is")
        print("  the fan-out at which row multiplication overtakes a saved round trip on")
        print("  YOUR hardware, which is the only version of the rule worth carrying.")


def main() -> None:
    engine = orm_lab.make_engine()
    counter = orm_lab.QueryCounter(engine)
    lab_db.banner("Lazy, eager, and eager done wrong")

    with lab_db.connect() as conn:
        items = conn.execute(
            "SELECT round(avg(n), 2) FROM (SELECT count(*) n FROM line_items "
            "GROUP BY order_id LIMIT 1000) s").fetchone()[0]
    print(f"Average line items per order in this seed: {items}. Work the row counts")
    print("out on paper before reading the table -- that is the exercise.\n")

    print(f"  {'variant':<16}{'limit':>7}{'queries':>10}{'rows over wire':>16}"
          f"{'p50 ms':>10}{'p99 ms':>10}{'best ms':>10}")
    print("  " + "-" * 80)
    results = {}
    for limit in LIMITS:
        for label, fn in VARIANTS:
            r = measure(engine, counter, fn, limit)
            results[(label, limit)] = r
            print(f"  {label:<16}{limit:>7}{r['queries']:>10,}{r['rows']:>16,}"
                  f"{r['p50']:>10.1f}{r['p99']:>10.1f}{r['min']:>10.1f}")
        print()

    big = LIMITS[-1]
    lazy = results[("lazy", big)]
    sel = results[("selectinload", big)]
    join = results[("joinedload", big)]
    hand = results[("single join", big)]

    print(f"  at limit {big}:")
    print(f"    lazy          {lazy['queries']:>6,} queries  "
          f"{lazy['rows']:>7,} rows  p50 {lazy['p50']:>7.1f}ms")
    print(f"    selectinload  {sel['queries']:>6,} queries  "
          f"{sel['rows']:>7,} rows  p50 {sel['p50']:>7.1f}ms  "
          f"({lazy['p50'] / max(sel['p50'], 1e-9):.1f}x faster than lazy)")
    print(f"    joinedload    {join['queries']:>6,} queries  "
          f"{join['rows']:>7,} rows  p50 {join['p50']:>7.1f}ms  "
          f"({lazy['p50'] / max(join['p50'], 1e-9):.1f}x faster than lazy)")
    print(f"    single join   {hand['queries']:>6,} queries  "
          f"{hand['rows']:>7,} rows  p50 {hand['p50']:>7.1f}ms")

    print()
    if join["p50"] > sel["p50"]:
        factor = join["p50"] / max(sel["p50"], 1e-9)
        print(f"  joinedload is {factor:.1f}x SLOWER than selectinload here, on the same data,")
        print("  with fewer queries. Fewer queries is not the goal; less work is. The JOIN")
        print(f"  returned {join['rows']:,} rows against selectinload's {sel['rows']:,}, and every")
        print("  duplicate had to be de-duplicated in Python before the ORM could hand you")
        print("  the objects. That Python-side pass is not free and does not appear in any")
        print("  query count.")
    else:
        print("  joinedload beat selectinload here -- and on this seed it keeps beating it")
        print("  at every `limit`, because orders fan out to only three line items. A 3x")
        print("  row multiplication of five narrow columns is cheaper than the extra round")
        print("  trips selectinload pays for. That is a real answer, not a failed")
        print("  experiment: joinedload on a one-to-many is fine when the fan-out is small")
        print("  and the parent row is narrow. The section below finds where it stops")
        print("  being fine, by going one level deeper.")

    nested_comparison(engine, counter)

    print()
    print("  The arithmetic to do before reaching for a loader strategy:")
    print("    many-to-one (order -> customer):  a JOIN adds 0 rows   -> joinedload")
    print("    one-to-many (order -> items):     a JOIN multiplies    -> selectinload")
    print("  And if the endpoint is read-only and shaped like a report, consider that the")
    print("  last row of the table exists: one query, no ORM objects, no loader strategy")
    print("  to get wrong later.")
    engine.dispose()


if __name__ == "__main__":
    main()
