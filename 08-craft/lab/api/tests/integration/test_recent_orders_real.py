"""Topic 4: the same assertions, against a real Postgres 18 in a container.

WHAT THIS DEMONSTRATES: the mocked suite passes. This one does not. The
difference is not test quality -- the assertions are nearly identical -- it is
that nothing here supplies the ordering the missing `ORDER BY` removed.

WHAT TO LOOK FOR: bug 1 is INTERMITTENT. Postgres may return heap order, and
heap order resembles insertion order until rows are UPDATEd. The seed shuffles
inserts and updates 500 rows precisely so heap order has moved. A test that
catches a real bug 3 times in 20 is a different and more realistic object than
one that catches it always, and deciding what to do with it is half the topic.

    pytest tests/integration/test_recent_orders_real.py -q     # fails, twice
    pytest tests/integration -q --count=20                     # intermittency

Needs Docker. Skipped with an unblock message when the daemon is down.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.models import Customer, Order
from app.repositories.orders import recent_orders, recent_orders_fixed

pytestmark = pytest.mark.container

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
CUSTOMER = 9001
LIMIT = 10


@pytest.fixture
async def seeded(pg_session):
    """40 orders for one customer, seeded the way lab/README.md requires.

    Two properties, both load-bearing (and both are what topic 4's
    broken-experiment notes tell you to check first if nothing fails):
      - insert order matches NEITHER id order NOR created_at order
      - enough cancelled rows fall inside the first `LIMIT` rows for bug 2 to bite
    """
    import random

    rng = random.Random(7)
    await pg_session.merge(Customer(id=CUSTOMER, name="topic-4"))
    rows = [
        Order(
            id=100_000 + i,
            customer_id=CUSTOMER,
            # every 3rd order cancelled, so ~3 of the newest 10 are cancelled
            status="cancelled" if i % 3 == 0 else "paid",
            total_cents=100 + i,
            created_at=T0 + timedelta(minutes=i),
        )
        for i in range(40)
    ]
    rng.shuffle(rows)                     # property 1
    for r in rows:
        pg_session.add(r)
    await pg_session.flush()
    # property 1, second half: move heap positions
    await pg_session.execute(
        text("UPDATE orders SET total_cents = total_cents + 1 "
             "WHERE customer_id = :c AND id %% 2 = 0"),
        {"c": CUSTOMER},
    )
    await pg_session.flush()
    return pg_session


async def test_bug_1_returns_orders_newest_first(seeded):
    """The assertion the mocked test also makes. Here nothing hands you the order."""
    got = await recent_orders(seeded, CUSTOMER, LIMIT)
    created = [o.created_at for o in got]
    assert created == sorted(created, reverse=True), (
        "recent_orders() returned rows in an order the query never asked for. "
        "Without ORDER BY, Postgres is free to return heap order -- and it did."
    )


async def test_bug_2_returns_limit_rows_when_enough_exist(seeded):
    """40 non-cancelled-eligible rows exist; asking for 10 must produce 10.

    It produces fewer, because the cancelled filter runs in Python AFTER the
    database applied LIMIT. The mocked test could not express this at all: there
    was no database to apply a limit.
    """
    got = await recent_orders(seeded, CUSTOMER, LIMIT)
    assert len(got) == LIMIT, (
        f"asked for {LIMIT}, got {len(got)} -- the cancelled rows were filtered "
        f"out of an already-limited page instead of out of the query"
    )


async def test_fixed_version_satisfies_both(seeded):
    got = await recent_orders_fixed(seeded, CUSTOMER, LIMIT)
    created = [o.created_at for o in got]
    assert created == sorted(created, reverse=True)
    assert len(got) == LIMIT
    assert all(o.status != "cancelled" for o in got)


async def test_ordering_by_a_non_unique_column_is_not_an_order(seeded):
    """The same defect as topic 5's cursor, in a different disguise.

    `ORDER BY created_at DESC` alone is a PARTIAL order: rows sharing a
    timestamp may come back in any order, and "any order" includes a different
    one on the next run. A total order needs a unique tiebreaker.
    """
    tie_at = T0 + timedelta(days=365)
    for i in range(4):
        seeded.add(Order(id=200_000 + i, customer_id=CUSTOMER, status="paid",
                         total_cents=1, created_at=tie_at))
    await seeded.flush()
    got = await recent_orders_fixed(seeded, CUSTOMER, 4)
    ids = [o.id for o in got]
    assert ids == sorted(ids, reverse=True), (
        "tied rows came back in an order the query did not specify; the fix "
        "orders by (created_at DESC, id DESC) for exactly this reason"
    )
