#!/usr/bin/env python3
"""Seed the lab database. Two properties of this seed are load-bearing.

WHAT THIS DEMONSTRATES: a seed written to make experiments *work* rather than to
look tidy. Both properties below are called out in lab/README.md and both are
easy to get wrong in the direction that silently deletes a topic's finding.

  1. INSERT ORDER MUST NOT MATCH ANY SORT ORDER YOU LATER ASSERT. Topic 4's
     missing `ORDER BY` is invisible when rows happen to come back in insertion
     order, so rows are shuffled before insert and a few hundred are UPDATEd
     afterwards to move their heap positions.
  2. `created_at` MUST CONTAIN DELIBERATE TIES. Topic 5's flagship bug does not
     exist without two rows sharing a timestamp. Real systems get ties from bulk
     imports and from any column with second-level resolution; this reproduces
     that on purpose rather than hoping for a collision.

WHAT TO LOOK FOR: the summary line at the end prints the tie count. If it says
zero, topic 5's flagship cannot fail and topic 4's ordering bug will look fixed.

    make seed                 # SEED_ORDERS from the environment, default 50000
"""
from __future__ import annotations

import asyncio
import os
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Customer, Order

N_ORDERS = int(os.environ.get("SEED_ORDERS", "50000"))
N_CUSTOMERS = 2000
STATUSES = ["pending", "paid", "shipped", "cancelled"]
SEED = 20260818          # fixed, so two people comparing numbers seeded the same rows


async def main() -> None:
    url = os.environ["DATABASE_URL"]
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    rng = random.Random(SEED)

    async with factory() as s:
        s.add_all([Customer(id=i, name=f"customer-{i}") for i in range(1, N_CUSTOMERS + 1)])
        await s.commit()

    # Second-resolution timestamps over a 30-day window. With N_ORDERS well
    # above the number of distinct seconds we allow, ties are guaranteed rather
    # than probable -- and the tie count is printed so you can check.
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    distinct_seconds = max(1, N_ORDERS // 8)   # ~8 orders per timestamp on average

    rows = [
        Order(
            id=i,
            customer_id=rng.randint(1, N_CUSTOMERS),
            status=rng.choice(STATUSES),
            total_cents=rng.randint(100, 500_00),
            created_at=base + timedelta(seconds=rng.randrange(distinct_seconds)),
        )
        for i in range(1, N_ORDERS + 1)
    ]
    # Property 1: shuffle so insertion order matches neither id order nor
    # created_at order. Without this line topic 4's experiment reports a fixed bug.
    rng.shuffle(rows)

    async with factory() as s:
        for chunk in (rows[i : i + 2000] for i in range(0, len(rows), 2000)):
            s.add_all(chunk)
            await s.flush()
        await s.commit()

    # Property 1, second half: UPDATE moves a row's physical position in the heap
    # under Postgres MVCC, so heap order stops resembling insertion order at all.
    async with factory() as s:
        victims = rng.sample(range(1, N_ORDERS + 1), min(500, N_ORDERS))
        await s.execute(
            text("UPDATE orders SET total_cents = total_cents + 1 WHERE id = ANY(:ids)"),
            {"ids": victims},
        )
        await s.commit()

    async with factory() as s:
        total = int((await s.execute(select(func.count()).select_from(Order))).scalar_one())
        distinct_ts = int(
            (await s.execute(select(func.count(func.distinct(Order.created_at))))).scalar_one()
        )
        cancelled = int(
            (
                await s.execute(
                    select(func.count()).select_from(Order).where(Order.status == "cancelled")
                )
            ).scalar_one()
        )

    print(f"seeded {total} orders across {N_CUSTOMERS} customers")
    print(f"  distinct created_at values : {distinct_ts}")
    print(f"  rows sharing a timestamp   : {total - distinct_ts}   <-- topic 5 needs this > 0")
    print(f"  cancelled orders           : {cancelled}   <-- topic 4 bug 2 needs these inside the limit window")
    print(f"  rows UPDATEd after insert  : {min(500, N_ORDERS)}   <-- topic 4 bug 1 needs heap order to have moved")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
