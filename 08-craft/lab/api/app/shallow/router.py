"""Layer 1 of 4. Declares the response model. Forwards to the service.

Mounted at /shallow. `GET /shallow/customers/{id}/orders` and
`GET /deep/customers/{id}/orders` return byte-identical bodies, which is the
precondition for the whole measurement: if the two shapes behaved differently,
any difference you measured afterwards would be a difference in behaviour.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from .service import list_customer_orders

router = APIRouter(prefix="/shallow", tags=["shallow"])


class ShallowOrderOut(BaseModel):
    """DTO #4. The wire shape. Also structurally identical to the other three."""

    id: int
    status: str
    total_cents: int
    created_at: datetime


class ShallowOrderPageOut(BaseModel):
    items: list[ShallowOrderOut]
    total: int


@router.get(
    "/customers/{customer_id}/orders",
    response_model=ShallowOrderPageOut,
    responses={404: {"description": "customer not found"}},
)
async def get_customer_orders(
    customer_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> ShallowOrderPageOut:
    page = await list_customer_orders(session, customer_id, limit, offset)
    return ShallowOrderPageOut(
        items=[
            ShallowOrderOut(id=i.id, status=i.status, total_cents=i.total_cents, created_at=i.created_at)
            for i in page.items
        ],
        total=page.total,
    )
