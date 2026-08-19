"""Topic 2, arm B: the same two endpoints, deduplicated into `_create`.

WHAT THIS DEMONSTRATES: the extraction that looks obviously right in review.
Four fifths of two handlers become one function; the diff is smaller; the
reviewer approves it in ninety seconds.

WHAT TO LOOK FOR: `_create`'s signature after each requirement lands. Every
divergence between the two callers has to be absorbed as a parameter, and a
parameter that exists only to select behaviour is pure interface surface with
zero functionality behind it. Count them off `_create`'s signature after each
requirement lands -- the "number of configuration parameters" row of topic 2's
table is that count, and reading it off the signature is the whole measurement.

The compounding is the point: N boolean flags gives `_create` 2^N behaviours,
and your tests cover about three of them.

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

router = APIRouter(prefix="/shared", tags=["topic2-shared"])


class SharedOrderIn(BaseModel):
    customer_id: int | None = None
    total_cents: int


class SharedOrderOut(BaseModel):
    id: int
    status: str
    total_cents: int


async def _create(
    session: AsyncSession,
    body: SharedOrderIn,
    *,
    status: str,
) -> SharedOrderOut:
    """Shared body. At requirement 0 it has exactly ONE parameter that exists to
    select behaviour (`status`), and that one is arguably data rather than a
    flag -- which is precisely why the extraction looks correct right now.

    Record the count here after each requirement.
    """
    if body.total_cents < 0:
        raise Invalid("total_cents must be >= 0")
    if body.customer_id is None:
        raise Invalid(f"customer_id is required for a {status}")
    order = Order(customer_id=body.customer_id, status=status, total_cents=body.total_cents)
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return SharedOrderOut(id=order.id, status=order.status, total_cents=order.total_cents)


@router.post("/orders", response_model=SharedOrderOut, status_code=201)
async def create_order(body: SharedOrderIn, session: AsyncSession = Depends(get_session)) -> SharedOrderOut:
    return await _create(session, body, status="pending")


@router.post("/orders/draft", response_model=SharedOrderOut, status_code=201)
async def create_draft(body: SharedOrderIn, session: AsyncSession = Depends(get_session)) -> SharedOrderOut:
    return await _create(session, body, status="draft")
