"""Topic 4: the mocked suite. It passes. It proves nothing.

WHAT THIS DEMONSTRATES: `recent_orders()` has two planted bugs (no ORDER BY, and
a filter applied after LIMIT). Every test in this file passes anyway.

WHAT TO LOOK FOR: the line marked `# <-- YOU supplied the ordering`. That is the
deep point of the topic: the mock does not merely fail to catch the bug, it
ACTIVELY SUPPLIES the property the bug removed, because you wrote the fixture
with the correct behaviour in mind. A mocked suite gets more confident as the
code gets more wrong.

    pytest tests/unit/test_recent_orders_mocked.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.repositories.orders import recent_orders

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def order(id: int, minutes: int, status: str = "paid"):
    return SimpleNamespace(
        id=id, customer_id=1, status=status,
        total_cents=100 * id, created_at=T0 + timedelta(minutes=minutes),
    )


def mocked_session(rows):
    """A session whose execute() returns exactly `rows`, in exactly this order."""
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value = iter(rows)
    session.execute.return_value = result
    return session


async def test_returns_newest_first():
    """Passes. The function has no ORDER BY at all."""
    rows = [                                  # <-- YOU supplied the ordering.
        order(3, 30), order(2, 20), order(1, 10),   # newest first, by hand,
    ]                                          #     because that is what you meant.
    got = await recent_orders(mocked_session(rows), customer_id=1, limit=10)
    assert [o.id for o in got] == [3, 2, 1]


async def test_excludes_cancelled():
    """Passes. The filter genuinely runs -- after LIMIT, which the mock cannot show."""
    rows = [order(3, 30), order(2, 20, status="cancelled"), order(1, 10)]
    got = await recent_orders(mocked_session(rows), customer_id=1, limit=10)
    assert all(o.status != "cancelled" for o in got)


async def test_respects_limit():
    """Passes, and this is the most misleading one in the file.

    The assertion is `<= limit`, and the mock returns however many rows the
    fixture holds, so LIMIT is never exercised at all. Bug 2 -- filtering after
    the database already applied LIMIT, so a caller asking for 10 gets 7 --
    cannot be expressed here, because there is no database to apply a limit.
    """
    rows = [order(i, i) for i in range(5, 0, -1)]
    got = await recent_orders(mocked_session(rows), customer_id=1, limit=3)
    assert len(got) <= 3 or True   # note what had to be written for this to pass


@pytest.mark.parametrize("attr", ["commit", "flush", "rollback", "any_method_at_all"])
async def test_automock_invents_whatever_you_ask_for(attr):
    """The mechanism, isolated: AsyncMock returns another AsyncMock for ANY
    attribute. There is no seam, no signature check, and no way for the double
    to refuse. `create_autospec(AsyncSession)` is the cheap mitigation and it
    would fail this test, which is the argument for it.
    """
    session = AsyncMock()
    await getattr(session, attr)()
    getattr(session, attr).assert_awaited_once()
