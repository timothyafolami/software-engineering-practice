"""Topic 5 stateful: the idempotency key written one transaction too late.

WHAT THIS DEMONSTRATES: `create_order` charges first and records the key second,
in a SEPARATE transaction. Any retry that arrives inside that window finds no
key, charges again, and only then records one. An example test that calls
create-then-retry sequentially will never see it; a `RuleBasedStateMachine`
that interleaves the two rules finds it, because interleaving is exactly what
it generates.

WHAT TO LOOK FOR: the invariant is `charges <= distinct idempotency keys used`.
It is a counting invariant over the whole run, not an assertion about one call,
which is why it survives the reordering the state machine does to the rules.

If the stateful test finds nothing, check that the two writes are still in
separate transactions -- committing the key inside the charge's transaction
removes the racing window and the machine can no longer reproduce it.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import Charge, IdempotencyKey, Order


async def create_order_with_key(
    factory: async_sessionmaker[AsyncSession],
    key: str,
    customer_id: int,
    amount_cents: int,
    *,
    window_s: float = 0.0,
) -> int:
    """THE PLANTED BUG. Charge and key are written in two transactions.

    `window_s` widens the race deterministically so the failure is reproducible
    rather than a matter of luck on the day. Set it to 0 and the bug is still
    there; it is just rarer, which is the realistic version.
    """
    async with factory() as s:
        existing = (
            await s.execute(select(IdempotencyKey).where(IdempotencyKey.key == key))
        ).scalar_one_or_none()
        if existing is not None:
            return existing.order_id

    # transaction 1: the order and the charge
    async with factory() as s:
        order = Order(customer_id=customer_id, status="pending", total_cents=amount_cents)
        s.add(order)
        await s.flush()
        s.add(Charge(order_id=order.id, amount_cents=amount_cents))
        await s.commit()
        order_id = order.id

    if window_s:
        await asyncio.sleep(window_s)   # <-- the window a retry lands in

    # transaction 2: the record that was supposed to make the retry free
    async with factory() as s:
        s.add(IdempotencyKey(key=key, order_id=order_id))
        await s.commit()

    return order_id


async def create_order_with_key_fixed(
    factory: async_sessionmaker[AsyncSession],
    key: str,
    customer_id: int,
    amount_cents: int,
) -> int:
    """The fix, and it is not "add a lock".

    The key is inserted in the SAME transaction as the charge, and the unique
    constraint on `idempotency_keys.key` is what makes the second writer lose.
    That is Ousterhout's "define errors out of existence" applied to a race:
    there is no window, so there is nothing to serialise around.
    """
    async with factory() as s:
        existing = (
            await s.execute(select(IdempotencyKey).where(IdempotencyKey.key == key))
        ).scalar_one_or_none()
        if existing is not None:
            return existing.order_id
        order = Order(customer_id=customer_id, status="pending", total_cents=amount_cents)
        s.add(order)
        await s.flush()
        s.add(Charge(order_id=order.id, amount_cents=amount_cents))
        s.add(IdempotencyKey(key=key, order_id=order.id))
        await s.commit()
        return order.id


async def counts(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    """(charge rows, distinct idempotency keys) -- the invariant's two numbers."""
    async with factory() as s:
        charges = int((await s.execute(select(func.count()).select_from(Charge))).scalar_one())
        keys = int(
            (await s.execute(select(func.count(func.distinct(IdempotencyKey.key))))).scalar_one()
        )
    return charges, keys
