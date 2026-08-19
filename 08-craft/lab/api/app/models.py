"""SQLAlchemy 2.0 declarative models. Five tables, no inheritance, no mixins.

WHAT THIS DEMONSTRATES: the schema topics 3-8 all measure against. Two details
are load-bearing rather than incidental and both are commented at the column:
`orders.created_at` is deliberately second-resolution so ties occur (topic 5),
and `charges` + `idempotency_keys` are separate tables written in separate
transactions so topic 5's stateful test has a real window to race in.

WHAT TO LOOK FOR: the composite index on (created_at DESC, id DESC). It is the
index `page_composite()` needs and the one `page()`'s single-column cursor
cannot use correctly.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    orders: Mapped[list["Order"]] = relationship(back_populates="customer")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id"), nullable=True, index=True
    )  # nullable because topic 2's third requirement makes drafts customer-less
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # SECOND resolution, on purpose. Real systems get ties from bulk imports and
    # from any column stored without sub-second precision; topic 5's flagship bug
    # does not exist without them, and a microsecond-resolution column would hide
    # it behind a probability rather than fixing it.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    customer: Mapped[Customer | None] = relationship(back_populates="orders")

    __table_args__ = (
        # The index page_composite() is designed around. A cursor over
        # created_at alone cannot use this correctly, which is the physical
        # version of topic 5's argument.
        Index("ix_orders_keyset", "created_at", "id"),
        Index("ix_orders_customer_created", "customer_id", "created_at"),
    )


class Charge(Base):
    """Written in its own transaction. Topic 5's stateful test counts these."""

    __tablename__ = "charges"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)


class IdempotencyKey(Base):
    """The record that is supposed to make a retry free.

    THE PLANTED BUG (topic 5): the service writes the charge first and this row
    second, in a separate transaction. Any retry that lands inside that window
    finds no key, charges again, and only then records the key. The invariant
    "charge rows never outnumber distinct idempotency keys" is what catches it.
    """

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (UniqueConstraint("key", name="uq_idempotency_key"),)


class OutboxEvent(Base):
    """Topic 2's second requirement: real orders emit an event, drafts do not."""

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
