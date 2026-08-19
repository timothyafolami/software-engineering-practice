"""The customer order listing, end to end, in one module.

Interface: one public function and the type it returns. Behind it: existence
checking, filtering, keyset-friendly ordering, the count that matches the
filter, the transaction boundary, and the mapping to the wire. A caller needs
to know the function's name and its four arguments; it does not need to know
that any of the rest exists.

This is the "owns a use case end to end" shape from topic 1's Python paragraph,
including the transaction boundary -- which is also the thing question 2 asks
you to break by composing two of these inside one transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import NotFound
from ..db import get_session
from ..models import Customer, Order


@dataclass(frozen=True)
class OrderPage:
    """One page of a customer's orders, plus the total that matches the filter."""

    items: list["OrderView"]
    total: int


@dataclass(frozen=True)
class OrderView:
    id: int
    status: str
    total_cents: int
    created_at: datetime


async def customer_order_page(
    session: AsyncSession, customer_id: int, *, limit: int = 50, offset: int = 0
) -> OrderPage:
    """Return one page of `customer_id`'s orders, newest first, with the total.

    Raises `NotFound` when the customer does not exist. An empty page for a
    customer that DOES exist is a 200 with `items: []` -- the two cases are
    genuinely different and the caller can tell them apart, which is the
    distinction the shallow shape spreads over three files.
    """
    await _require_customer(session, customer_id)
    base = _order_query(customer_id)
    rows = (
        await session.execute(
            base.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit).offset(offset)
        )
    ).scalars()
    total = int((await session.execute(_count_of(base))).scalar_one())
    return OrderPage(
        items=[
            OrderView(id=o.id, status=o.status, total_cents=o.total_cents, created_at=o.created_at)
            for o in rows
        ],
        total=total,
    )


# --- private. Seams, not surface. -------------------------------------------


def _order_query(customer_id: int) -> Select:
    """One place where the filter is expressed.

    This is why topic 1's requirement -- "the total must reflect the filter" --
    is a one-line change here and a four-file change in the shallow shape: the
    page query and the count query are derived from the SAME select, so they
    cannot drift apart. The shallow shape has two independently-written
    filters in two files and nothing that makes them agree.
    """
    return select(Order).where(Order.customer_id == customer_id)


def _count_of(query: Select) -> Select:
    return select(func.count()).select_from(query.subquery())


async def _require_customer(session: AsyncSession, customer_id: int) -> None:
    found = (
        await session.execute(select(Customer.id).where(Customer.id == customer_id))
    ).scalar_one_or_none()
    if found is None:
        raise NotFound(f"customer {customer_id} not found", customer_id=customer_id)


# --- transport --------------------------------------------------------------

router = APIRouter(prefix="/deep", tags=["deep"])


class DeepOrderOut(BaseModel):
    id: int
    status: str
    total_cents: int
    created_at: datetime


class DeepOrderPageOut(BaseModel):
    items: list[DeepOrderOut]
    total: int


@router.get(
    "/customers/{customer_id}/orders",
    response_model=DeepOrderPageOut,
    responses={404: {"description": "customer not found"}},
)
async def get_customer_orders(
    customer_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> DeepOrderPageOut:
    page = await customer_order_page(session, customer_id, limit=limit, offset=offset)
    return DeepOrderPageOut(
        items=[
            DeepOrderOut(id=i.id, status=i.status, total_cents=i.total_cents, created_at=i.created_at)
            for i in page.items
        ],
        total=page.total,
    )
