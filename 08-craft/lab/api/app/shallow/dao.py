"""Layer 4 of 4. Owns SQL. Declares `OrderRow`.

Read the body and ask topic 1's reliability test: does it teach you anything you
had not already inferred from the name? `select_orders` selects orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Order


@dataclass(frozen=True)
class OrderRow:
    """DTO #1 of 3. The database's shape."""

    id: int
    customer_id: int | None
    status: str
    total_cents: int
    created_at: datetime


async def select_orders(
    session: AsyncSession, customer_id: int, limit: int, offset: int
) -> list[OrderRow]:
    stmt = (
        select(Order)
        .where(Order.customer_id == customer_id)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars()
    return [
        OrderRow(
            id=o.id, customer_id=o.customer_id, status=o.status,
            total_cents=o.total_cents, created_at=o.created_at,
        )
        for o in rows
    ]


async def select_order_count(session: AsyncSession, customer_id: int) -> int:
    stmt = select(func.count()).select_from(Order).where(Order.customer_id == customer_id)
    return int((await session.execute(stmt)).scalar_one())
