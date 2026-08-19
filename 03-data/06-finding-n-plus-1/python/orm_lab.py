"""
Shared SQLAlchemy 2.0 setup for Topic 6: models, and the query counter.

Imported by query_counter.py and lazy_vs_eager.py; not run directly.

WHY AN ORM AT ALL, IN A LAYER THAT OTHERWISE WRITES SQL BY HAND: because the bug
this topic is about does not exist without one. N+1 is what happens when reading
an attribute emits SQL, and the whole point is that the loop and the query live
in different files. Writing the queries by hand would remove the bug and the
lesson with it.

The models map onto the lab's existing tables. Nothing here creates or alters a
table -- the seed in lab/local/lab_db.py owns the schema.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402

try:
    from sqlalchemy import BigInteger, DateTime, ForeignKey, String, create_engine, event
    from sqlalchemy.orm import (DeclarativeBase, Mapped, Session, mapped_column,
                                relationship)
except ImportError:  # pragma: no cover - environment guard
    sys.exit("This topic needs SQLAlchemy 2.0.\n"
             "  install: python3 -m pip install 'sqlalchemy>=2.0'")


def engine_url() -> str:
    """The lab DSN as a SQLAlchemy URL, psycopg3 driver."""
    dsn = lab_db.DSN
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    if dsn.startswith("postgresql:///"):
        return dsn.replace("postgresql:///", "postgresql+psycopg:///", 1)
    return f"postgresql+psycopg:///{dsn}"


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String)
    country: Mapped[str] = mapped_column(String)
    orders: Mapped[list["Order"]] = relationship(back_populates="customer")


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    customer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("customers.id"))
    status: Mapped[str] = mapped_column(String)
    total_cents: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))

    # Both relationships are left at SQLAlchemy's DEFAULT loader strategy, which
    # is lazy="select": touching the attribute emits a query, from wherever the
    # code that touched it happens to live. That default is the bug this whole
    # topic is about, and changing it here would hide the thing we are measuring.
    customer: Mapped["Customer"] = relationship(back_populates="orders")
    line_items: Mapped[list["LineItem"]] = relationship(back_populates="order")


class LineItem(Base):
    __tablename__ = "line_items"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("orders.id"))
    sku: Mapped[str] = mapped_column(String)
    qty: Mapped[int] = mapped_column(BigInteger)
    price_cents: Mapped[int] = mapped_column(BigInteger)
    order: Mapped["Order"] = relationship(back_populates="line_items")


# ---------------------------------------------------------------------------
# The query counter. Ten lines on before_cursor_execute, and it is the whole of
# detection method 1 -- the cheapest of the four, the only one that runs in CI,
# and the one that catches most N+1s before they are ever deployed.
#
# Django ships this as assertNumQueries(n). SQLAlchemy does not, so here it is.
# ---------------------------------------------------------------------------

class QueryCounter:
    """Counts statements and rows per 'request', from the driver's own hooks.

    Counting at this level rather than in application code is what makes it
    trustworthy: it sees every statement any layer emits, including the ones
    emitted by an ORM attribute access nobody wrote a call for.
    """

    def __init__(self, engine):
        self.engine = engine
        self.local = threading.local()
        event.listen(engine, "before_cursor_execute", self._before)
        event.listen(engine, "after_cursor_execute", self._after)

    def _state(self):
        if not hasattr(self.local, "state"):
            self.local.state = {"on": False, "queries": [], "rows": 0}
        return self.local.state

    def _before(self, conn, cursor, statement, params, context, executemany):
        state = self._state()
        if state["on"]:
            context._sep_t0 = time.perf_counter()

    def _after(self, conn, cursor, statement, params, context, executemany):
        state = self._state()
        if not state["on"]:
            return
        ms = (time.perf_counter() - getattr(context, "_sep_t0", time.perf_counter())) * 1000
        # rowcount is the number of rows the server actually sent. It is the
        # number that explains why joinedload can lose to the N+1 it replaced.
        rows = max(0, cursor.rowcount or 0)
        state["queries"].append((" ".join(statement.split())[:90], rows, ms))
        state["rows"] += rows

    @contextmanager
    def request(self):
        """One 'request'. Everything inside is counted; nothing outside is."""
        state = self._state()
        state.update(on=True, queries=[], rows=0)
        t0 = time.perf_counter()
        try:
            yield state
        finally:
            state["on"] = False
            state["ms"] = (time.perf_counter() - t0) * 1000
            state["count"] = len(state["queries"])


def make_engine(echo: bool = False):
    """One engine for the whole program. Built here rather than at import time
    in the callers, because an engine created before a fork is inherited by
    every child with the same sockets -- Topic 7's trap, avoided by habit."""
    lab_db.ensure_database()
    with lab_db.connect() as conn:
        lab_db.ensure_big_seed(conn)
    return create_engine(engine_url(), echo=echo, pool_pre_ping=True)


def new_session(engine) -> Session:
    """A fresh Session per request.

    Fresh matters: a Session carries an IDENTITY MAP, and a second access to a
    row it has already loaded is free. Reusing one Session across 'requests'
    would make the second request look dramatically faster than the first for a
    reason that has nothing to do with the loader strategy under test.
    """
    return Session(engine, expire_on_commit=False)
