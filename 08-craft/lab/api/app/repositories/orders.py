"""Topics 3 and 4 both live in this file, and they are the same file on purpose.

WHAT THIS DEMONSTRATES:
  - topic 3: one `except` block, three variants, selected by ERROR_MODE. The
    `swallow` variant turns a dead database into a fast, successful, empty
    response -- 200, no error rate, better p99 than healthy.
  - topic 4: `recent_orders()` ships with two planted bugs that a mocked test
    cannot see, because the mock supplies the property the bug removed.

WHAT TO LOOK FOR: the latency column in topic 3's table. The swallowing variant
is not merely wrong, it is wrong AND FAST, which is the exact signature that
survives a dashboard built on error rate and p99.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.errors import Unavailable
from ..models import Order

logger = logging.getLogger("craft.repo.orders")


async def orders_for_customer(
    session: AsyncSession, customer_id: int, *, status: str | None = None,
    limit: int = 50, before: datetime | None = None,
) -> list[Order]:
    """The query topic 3 breaks three different ways.

    ERROR_MODE selects which `except` block is compiled into the request path.
    Everything else about the three variants is identical, so any difference
    you measure is the error handling and nothing else.
    """
    stmt = select(Order).where(Order.customer_id == customer_id)
    if status is not None:
        stmt = stmt.where(Order.status == status)
    if before is not None:
        stmt = stmt.where(Order.created_at < before)
    stmt = stmt.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit)

    mode = settings.error_mode

    if mode == "swallow":
        # THE ANTI-PATTERN, written the way it actually appears in production:
        # broad, "defensive", logged at WARNING so it looks handled. Note how
        # wide the `try` is -- it covers the result materialisation too, so a
        # bug in row construction is also reported as "order lookup failed".
        try:
            result = await session.execute(stmt)
            return list(result.scalars())
        except Exception:
            logger.warning("order lookup failed")
            return []

    if mode == "none":
        # No handling at all. The driver's OperationalError propagates to the
        # top-level handler and becomes a 500. Loud, discoverable, and a strictly
        # better outcome than the variant above.
        result = await session.execute(stmt)
        return list(result.scalars())

    # mode == "correct": catch exactly the thing that can actually fail, at
    # exactly the statement that can fail it, translate it into the taxonomy
    # with the cause preserved, and let everything else through untouched.
    try:
        result = await session.execute(stmt)
    except OperationalError as exc:
        raise Unavailable("orders database unreachable", retry_after=1) from exc
    return list(result.scalars())


async def count_orders_for_customer(
    session: AsyncSession, customer_id: int, *, status: str | None = None
) -> int:
    """Counted with the SAME filter the page used.

    Topic 1's requirement is exactly this: `total` must reflect the filter. In
    the shallow shape the filter and the count live in different files, which is
    why the requirement touches four of them.
    """
    stmt = select(func.count()).select_from(Order).where(Order.customer_id == customer_id)
    if status is not None:
        stmt = stmt.where(Order.status == status)
    return int((await session.execute(stmt)).scalar_one())


async def recent_orders(session: AsyncSession, customer_id: int, limit: int) -> list[Order]:
    """TOPIC 4'S SUBJECT. Two planted bugs, both invisible to a mock.

    BUG 1 -- no ORDER BY. Without one, Postgres may return heap order, and heap
    order is not insertion order once rows have been UPDATEd. A mocked test
    cannot see this because the fixture hands back a list *you* wrote in the
    right order: the mock supplies the ordering guarantee the missing clause
    removed. Against a real database it is INTERMITTENT, which is a more
    realistic and more interesting object than a test that always fails.

    BUG 2 -- the cancelled-order filter runs in Python, after the database has
    already applied LIMIT. Ask for 10 and you get however many of the first 10
    rows happened not to be cancelled. Invisible to a mock for the same reason:
    the fixture contains the rows the author expected the filter to keep.
    """
    stmt = select(Order).where(Order.customer_id == customer_id).limit(limit)
    result = await session.execute(stmt)
    orders = [o for o in result.scalars() if o.status != "cancelled"]
    return orders


async def recent_orders_fixed(session: AsyncSession, customer_id: int, limit: int) -> list[Order]:
    """Both bugs fixed: filter in SQL, sort in SQL, limit last.

    Note that the fix for bug 1 is a *total* order -- `created_at DESC, id DESC`
    -- not just `created_at DESC`. Ordering by a non-unique column is not an
    order, it is a partial one, and it is the same defect as topic 5's cursor.
    """
    stmt = (
        select(Order)
        .where(Order.customer_id == customer_id, Order.status != "cancelled")
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())
