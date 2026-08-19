"""Topic 1's PRECONDITION, asserted rather than assumed: the two shapes agree.

WHAT THIS DEMONSTRATES: `app/shallow/` and `app/deep/` implement the identical
feature. Topic 1's whole measurement -- change amplification, cognitive load,
interface surface -- is only about *design* if the two shapes behave the same;
the moment they diverge you are measuring a difference in behaviour and will
attribute it to design. That is topic 1's fourth broken-experiment note, and
this file is the check that catches it.

WHAT TO LOOK FOR: the assertion is on the raw response BYTES, not on a parsed
object. Two encoders that disagree about how a timestamp is rendered would pass
a parsed comparison and fail a real consumer.

    DATABASE_URL=sqlite+aiosqlite:///:memory: pytest tests/integration/test_shapes_are_identical.py -q

Runs natively against whatever DATABASE_URL points at -- sqlite in memory here,
Postgres 18 inside the compose stack -- so it is not blocked by the daemon.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db import SessionLocal, engine
from app.main import app
from app.models import Base, Customer, Order

# One event loop for the whole module. `app.db.engine` is created at import
# time and aiosqlite pins its connection to the loop that first used it, so a
# fresh per-test loop would deadlock on the second test rather than fail.
pytestmark = pytest.mark.asyncio(loop_scope="module")

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
CUSTOMER = 990_001
MISSING = 990_002


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def seeded():
    """Twelve orders, with deliberate created_at ties, for one customer.

    Ties are here on purpose: `ORDER BY created_at DESC, id DESC` is a total
    order and both shapes use it, so a tie must not make the two disagree. If
    one shape dropped the `id DESC` tiebreaker this fixture is what would
    catch it.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as s:
        await s.execute(delete(Order).where(Order.customer_id == CUSTOMER))
        await s.execute(delete(Customer).where(Customer.id.in_([CUSTOMER, MISSING])))
        s.add(Customer(id=CUSTOMER, name="topic-1"))
        for i in range(12):
            s.add(Order(
                id=990_000 + i,
                customer_id=CUSTOMER,
                status="paid" if i % 3 else "cancelled",
                total_cents=100 + i,
                created_at=T0 + timedelta(minutes=i // 2),   # pairs share a timestamp
            ))
        await s.commit()
    yield
    async with SessionLocal() as s:
        await s.execute(delete(Order).where(Order.customer_id == CUSTOMER))
        await s.execute(delete(Customer).where(Customer.id == CUSTOMER))
        await s.commit()
    # DISPOSE. aiosqlite runs each connection on its own non-daemon thread, so
    # an engine left open keeps the interpreter alive after the last test
    # reports -- pytest prints "46 passed" and then the process simply never
    # exits. This is the only file in the suite that opens a real connection,
    # which is why it is also the only one that has to close one.
    await engine.dispose()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://lab") as c:
        yield c


@pytest.mark.parametrize("limit,offset", [(50, 0), (5, 0), (5, 5), (1, 11), (5, 100)])
async def test_both_shapes_return_byte_identical_bodies(client, seeded, limit, offset):
    q = f"?limit={limit}&offset={offset}"
    shallow = await client.get(f"/shallow/customers/{CUSTOMER}/orders{q}")
    deep = await client.get(f"/deep/customers/{CUSTOMER}/orders{q}")
    assert shallow.status_code == deep.status_code == 200
    assert shallow.content == deep.content, (
        "the two shapes disagree, so any change-amplification number measured "
        "against them is measuring behaviour rather than design"
    )


async def test_both_shapes_404_the_same_way(client, seeded):
    """The 404 is topic 1's cognitive-load question, so it has to match too."""
    shallow = await client.get(f"/shallow/customers/{MISSING}/orders")
    deep = await client.get(f"/deep/customers/{MISSING}/orders")
    assert shallow.status_code == deep.status_code == 404
    assert shallow.json() == deep.json()


async def test_the_total_is_the_unfiltered_count_in_both(client, seeded):
    """Requirement 0, recorded before the status filter is added.

    Topic 1 asks you to implement "total must reflect the filter" in both
    shapes and diff them. This pins the starting point, so a diff taken later
    is against a state somebody checked rather than one they remember.
    """
    for prefix in ("shallow", "deep"):
        r = await client.get(f"/{prefix}/customers/{CUSTOMER}/orders?limit=3")
        body = r.json()
        assert body["total"] == 12
        assert len(body["items"]) == 3
