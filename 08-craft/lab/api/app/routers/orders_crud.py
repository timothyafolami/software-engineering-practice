"""The create -> read -> delete chain, with the OpenAPI links that let a tool follow it.

WHAT THIS DEMONSTRATES: topic 6's stateful phase has nothing to chain unless the
schema says which operation can follow which. Without the `links` blocks below,
schemathesis reports `Missing Open API links` -- which is a finding about the
schema, not a tool failure. With them, it can create an order, read it back, and
delete it, and check that the spec's claims about that sequence hold.

WHAT TO LOOK FOR: `DELETE /orders/{id}` returns 204 whether or not the row
existed. That is topic 3's "define errors out of existence", applied: there is
no 404 branch for any caller to handle, forever, and the stateful phase's
delete-then-get step has a defined answer instead of a race.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import Invalid, NotFound
from ..db import get_session
from ..models import Order

router = APIRouter(prefix="/orders", tags=["orders"])

# Every response model in this app has a globally unique class name, and that is
# a contract requirement rather than a style choice. When two modules both
# declare `OrderOut`, FastAPI disambiguates by prefixing the module path -- and
# WHICH of the two collides is not stable across processes, so the generated
# schema is not byte-stable and `openapi.snapshot.json` diffs against itself.
# A snapshot that cannot be reproduced is not a contract. Found by running
# `python snapshot_openapi.py --check` twice; it is topic 9's naming argument
# arriving as a topic 6 defect.


class OrderIn(BaseModel):
    customer_id: int | None = None
    total_cents: int


class OrderOut(BaseModel):
    id: int
    status: str
    total_cents: int


class ApiError(BaseModel):
    error: str
    message: str


# The link objects. `$response.body#/id` is the runtime expression schemathesis
# evaluates to get the created order's id out of the 201 body and into the next
# operation's path parameter.
_CREATED_LINKS = {
    "GetOrderById": {
        "operationId": "get_order",
        "parameters": {"order_id": "$response.body#/id"},
        "description": "Read back the order this call created.",
    },
    "DeleteOrderById": {
        "operationId": "delete_order",
        "parameters": {"order_id": "$response.body#/id"},
        "description": "Delete the order this call created.",
    },
}


@router.post(
    "",
    response_model=OrderOut,
    status_code=201,
    operation_id="create_order",
    responses={
        201: {"links": _CREATED_LINKS},
        422: {"model": ApiError, "description": "invalid order"},
    },
)
async def create_order(body: OrderIn, session: AsyncSession = Depends(get_session)) -> OrderOut:
    if body.total_cents < 0:
        raise Invalid("total_cents must be >= 0")
    order = Order(customer_id=body.customer_id, status="pending", total_cents=body.total_cents)
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return OrderOut(id=order.id, status=order.status, total_cents=order.total_cents)


@router.get(
    "/{order_id}",
    response_model=OrderOut,
    operation_id="get_order",
    responses={404: {"model": ApiError, "description": "order not found"}},
)
async def get_order(order_id: int, session: AsyncSession = Depends(get_session)) -> OrderOut:
    order = (
        await session.execute(select(Order).where(Order.id == order_id))
    ).scalar_one_or_none()
    if order is None:
        raise NotFound(f"order {order_id} not found", order_id=order_id)
    return OrderOut(id=order.id, status=order.status, total_cents=order.total_cents)


@router.delete(
    "/{order_id}",
    status_code=204,
    operation_id="delete_order",
    responses={204: {"description": "deleted, or already absent"}},
)
async def delete_order(order_id: int, session: AsyncSession = Depends(get_session)) -> Response:
    """Idempotent by construction: 204 whether or not the row existed.

    The read-check-delete version would need a 404 branch in every caller AND
    would race two concurrent deletes into one spurious error. One statement
    with a WHERE clause removes both problems, and the rowcount is available if
    anyone ever needs to know which of the two happened.
    """
    await session.execute(delete(Order).where(Order.id == order_id))
    await session.commit()
    return Response(status_code=204)
