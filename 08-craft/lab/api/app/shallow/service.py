"""Layer 2 of 4. Declares `OrderListing`. Forwards to the repository.

The one thing this layer genuinely adds is the 404, which is four lines. Note
where it sits: to answer "what happens if customer_id doesn't exist?" you have
to know that the *service* decides that, that the *repository* returns an empty
list rather than raising, and that the *dao* has no opinion at all. Three files
open to answer one question -- that is the cognitive-load row, counted honestly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import NotFound
from ..models import Customer
from .repository import count_orders, fetch_orders


@dataclass(frozen=True)
class OrderListing:
    """DTO #3 of 3."""

    id: int
    status: str
    total_cents: int
    created_at: datetime


@dataclass(frozen=True)
class OrderPage:
    items: list[OrderListing]
    total: int


async def list_customer_orders(
    session: AsyncSession, customer_id: int, limit: int, offset: int
) -> OrderPage:
    """Raises NotFound when the customer does not exist."""
    exists = (
        await session.execute(select(Customer.id).where(Customer.id == customer_id))
    ).scalar_one_or_none()
    if exists is None:
        raise NotFound(f"customer {customer_id} not found", customer_id=customer_id)

    records = await fetch_orders(session, customer_id, limit, offset)
    total = await count_orders(session, customer_id)
    items = [
        OrderListing(id=r.id, status=r.status, total_cents=r.total_cents, created_at=r.created_at)
        for r in records
    ]
    return OrderPage(items=items, total=total)
