"""The endpoints topics 3-7 drive load against, plus the operational readouts.

WHAT THIS DEMONSTRATES: `GET /customers/{id}/orders` is the one topic 3 breaks
and topic 7 slows down. `GET /_pool` and `GET /_config` exist so a recorded
measurement always has the pool state and the active configuration beside it --
which is the difference between a number and a finding.

WHAT TO LOOK FOR: every declared error response is in the `responses=` block.
A 404 that is not in the schema is a contract violation no tool in topic 6 can
check, because the tool only knows what the schema says.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.errors import NotFound
from ..db import get_session, pool_wait_stats
from ..models import Customer
from ..repositories.orders import (
    count_orders_for_customer,
    orders_for_customer,
    recent_orders,
)

router = APIRouter(tags=["customers"])


class CustomerOrderOut(BaseModel):
    id: int
    status: str
    total_cents: int


class CustomerOrderListOut(BaseModel):
    items: list[CustomerOrderOut]
    total: int


class ApiError(BaseModel):
    error: str
    message: str


@router.get(
    "/customers/{customer_id}/orders",
    response_model=CustomerOrderListOut,
    responses={
        404: {"model": ApiError, "description": "customer not found"},
        503: {"model": ApiError, "description": "orders database unavailable"},
    },
)
async def customer_orders(
    customer_id: int,
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> CustomerOrderListOut:
    """Topic 3's subject. With ERROR_MODE=swallow and the database cut, this
    returns 200 with an empty list, faster than the healthy path."""
    orders = await orders_for_customer(session, customer_id, status=status, limit=limit)
    total = await count_orders_for_customer(session, customer_id, status=status)
    return CustomerOrderListOut(
        items=[CustomerOrderOut(id=o.id, status=o.status, total_cents=o.total_cents) for o in orders],
        total=total,
    )


@router.get("/customers/{customer_id}/orders/recent", response_model=list[CustomerOrderOut],
            responses={404: {"model": ApiError}})
async def customer_recent_orders(
    customer_id: int,
    limit: int = Query(10, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> list[CustomerOrderOut]:
    """Topic 4's subject, over HTTP. Two planted bugs; see repositories/orders.py."""
    found = (
        await session.execute(select(Customer.id).where(Customer.id == customer_id))
    ).scalar_one_or_none()
    if found is None:
        raise NotFound(f"customer {customer_id} not found")
    orders = await recent_orders(session, customer_id, limit)
    return [CustomerOrderOut(id=o.id, status=o.status, total_cents=o.total_cents) for o in orders]


@router.get("/_pool", include_in_schema=False)
async def pool() -> dict:
    """Connection checkout wait, p50/p99. Topic 7's 'pool wait p99' column."""
    return pool_wait_stats()


@router.get("/_config", include_in_schema=False)
async def config() -> dict:
    """The active configuration, so a measurement is never orphaned from it."""
    return {"config": settings.describe()}


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    """Deliberately does NOT touch the database.

    A health check that queries Postgres turns topic 7's slow dependency into a
    restart loop, then a thundering herd against a cold cache -- the metastable
    sequence the topic describes. Keeping it dependency-free is the choice that
    makes the ladder measurable instead of chaotic.
    """
    return {"ok": True}
