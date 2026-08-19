"""Layer 3 of 4. Declares `OrderRecord`. Forwards to the dao.

This is the layer Ousterhout's "pass-through method" describes exactly: the
signature is nearly identical to the one it calls, so there are now two places
to look, two names for one idea, and two things to keep in sync.

The stated justification -- "decouple from the ORM" -- does not survive reading
the file: SQLAlchemy's `Session` already IS that abstraction, and this layer
re-exposes it verbatim.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from .dao import OrderRow, select_order_count, select_orders


@dataclass(frozen=True)
class OrderRecord:
    """DTO #2 of 3. Structurally identical to OrderRow. Declared anyway."""

    id: int
    customer_id: int | None
    status: str
    total_cents: int
    created_at: datetime

    @classmethod
    def from_row(cls, row: OrderRow) -> "OrderRecord":
        return cls(
            id=row.id, customer_id=row.customer_id, status=row.status,
            total_cents=row.total_cents, created_at=row.created_at,
        )


async def fetch_orders(
    session: AsyncSession, customer_id: int, limit: int, offset: int
) -> list[OrderRecord]:
    rows = await select_orders(session, customer_id, limit, offset)
    return [OrderRecord.from_row(r) for r in rows]


async def count_orders(session: AsyncSession, customer_id: int) -> int:
    return await select_order_count(session, customer_id)
