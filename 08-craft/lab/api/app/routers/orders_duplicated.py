"""Topic 2, arm A: `POST /orders` and `POST /orders/draft`, duplicated.

WHAT THIS DEMONSTRATES: the starting point of the staged experiment. The two
handlers share roughly four fifths of their body TODAY. They are the same shape
by coincidence, not by concept -- an order is a commitment and a draft is a
note-to-self -- and the three requirements in the module docstring below are
what pull them apart.

WHAT TO LOOK FOR: apply requirements 1, 2 and 3 here as three separate commits
and diff each against the same three commits made in `orders_shared.py`. The
shared version wins requirement 1 almost every time. The question the experiment
asks is what the curve does by requirement 3.

THE THREE REQUIREMENTS, applied one at a time, in this order:
  1. drafts skip inventory reservation
  2. real orders emit an event
  3. drafts allow a null customer
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import Invalid
from ..db import get_session
from ..models import Order

router = APIRouter(prefix="/dup", tags=["topic2-duplicated"])


class DupOrderIn(BaseModel):
    customer_id: int | None = None
    total_cents: int


class DupOrderOut(BaseModel):
    id: int
    status: str
    total_cents: int


@router.post("/orders", response_model=DupOrderOut, status_code=201)
async def create_order(body: DupOrderIn, session: AsyncSession = Depends(get_session)) -> DupOrderOut:
    if body.total_cents < 0:
        raise Invalid("total_cents must be >= 0")
    if body.customer_id is None:
        raise Invalid("customer_id is required for an order")
    order = Order(customer_id=body.customer_id, status="pending", total_cents=body.total_cents)
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return DupOrderOut(id=order.id, status=order.status, total_cents=order.total_cents)


@router.post("/orders/draft", response_model=DupOrderOut, status_code=201)
async def create_draft(body: DupOrderIn, session: AsyncSession = Depends(get_session)) -> DupOrderOut:
    if body.total_cents < 0:
        raise Invalid("total_cents must be >= 0")
    if body.customer_id is None:
        raise Invalid("customer_id is required for a draft")
    order = Order(customer_id=body.customer_id, status="draft", total_cents=body.total_cents)
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return DupOrderOut(id=order.id, status=order.status, total_cents=order.total_cents)
