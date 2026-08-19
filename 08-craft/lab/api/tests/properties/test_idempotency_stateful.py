"""Topic 5 stateful: a RuleBasedStateMachine finds the interleaving.

WHAT THIS DEMONSTRATES: `create_order_with_key` writes the charge in one
transaction and the idempotency key in a second. A retry that lands between the
two finds no key and charges again. An example test that calls create-then-retry
sequentially never sees it; a state machine that INTERLEAVES the rules does.

WHAT TO LOOK FOR: the invariant is a count over the whole run -- charge rows
never outnumber the distinct idempotency keys used -- not an assertion about one
call. That is what makes it survive the reordering the machine does.

If it finds nothing: check the two writes are still in separate transactions.
Committing the key inside the charge's transaction removes the window, and the
machine can no longer reproduce it (which is also the fix).

    pytest tests/properties/test_idempotency_stateful.py -q --hypothesis-show-statistics
"""
from __future__ import annotations

import pytest

# Skip the whole module rather than erroring collection when Hypothesis is
# absent, so `pytest tests` still runs the rest of the suite and the reason is
# printed with the command that fixes it.
pytest.importorskip(
    "hypothesis",
    reason="Hypothesis is not installed. Unblock with: "
    "python3 -m pip install 'hypothesis==6.165.*'",
)

import asyncio

from hypothesis import HealthCheck, settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, initialize, rule
from hypothesis import strategies as st

from app.services.idempotency import (
    counts, create_order_with_key, create_order_with_key_fixed,
)

pytestmark = pytest.mark.container

KEYS = st.sampled_from(["k1", "k2", "k3"])


class IdempotencyMachine(RuleBasedStateMachine):
    """Rules: create with a key, retry the same key, and check the counts.

    `retry_create` deliberately fires the SAME key concurrently with an
    in-flight create, which is the interleaving a hand-written test does not
    think to write.
    """

    impl = staticmethod(create_order_with_key)

    @initialize()
    def setup(self):
        self.factory = _factory()
        self.keys_used: set[str] = set()
        # reset per run: a state machine that accumulates state across runs
        # reports a violation that belongs to an earlier example
        _reset(self.factory)

    @rule(key=KEYS)
    def create(self, key):
        self.keys_used.add(key)
        _run(self.impl(self.factory, key, customer_id=1, amount_cents=500, window_s=0.01))

    @rule(key=KEYS)
    def retry_concurrently(self, key):
        """Two callers, same key, overlapping. This is the shape of a client
        retry after a timeout -- the first request is still running."""
        self.keys_used.add(key)

        async def both():
            await asyncio.gather(
                self.impl(self.factory, key, customer_id=1, amount_cents=500, window_s=0.01),
                self.impl(self.factory, key, customer_id=1, amount_cents=500, window_s=0.01),
            )

        _run(both())

    @invariant()
    def charges_never_outnumber_keys(self):
        if not hasattr(self, "factory"):
            return
        charges, keys = _run(counts(self.factory))
        assert charges <= max(1, len(self.keys_used)), (
            f"{charges} charge rows for {len(self.keys_used)} distinct idempotency "
            f"keys -- a retry inside the write window charged twice"
        )


class FixedIdempotencyMachine(IdempotencyMachine):
    """Same rules, same invariant, key written in the charge's transaction."""

    impl = staticmethod(lambda f, k, customer_id, amount_cents, window_s=0.0:
                        create_order_with_key_fixed(f, k, customer_id, amount_cents))


TestIdempotency = IdempotencyMachine.TestCase
TestIdempotencyFixed = FixedIdempotencyMachine.TestCase
# deadline=None: anything touching a container flakes against the 200ms default,
# and a DeadlineExceeded is not a finding about your code.
TestIdempotency.settings = settings(
    max_examples=50, deadline=None, stateful_step_count=12,
    suppress_health_check=[HealthCheck.too_slow],
)
TestIdempotencyFixed.settings = TestIdempotency.settings


# --- plumbing ---------------------------------------------------------------
# Hypothesis's stateful rules are synchronous, so each rule drives its own event
# loop. One loop for the whole machine, created lazily, so the pool is not
# rebuilt per step.

_LOOP: asyncio.AbstractEventLoop | None = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP.run_until_complete(coro)


def _factory():
    import os

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "set TEST_DATABASE_URL to a Postgres 18 instance, e.g. "
            "cd 08-craft/lab && docker compose up -d postgres && "
            "TEST_DATABASE_URL=postgresql+asyncpg://app:app@localhost:55442/craft_lab "
            "pytest tests/properties/test_idempotency_stateful.py"
        )
    engine = create_async_engine(url, pool_size=10, max_overflow=10)
    return async_sessionmaker(engine, expire_on_commit=False)


def _reset(factory):
    from sqlalchemy import delete

    from app.models import Base, Charge, IdempotencyKey, Order

    async def go():
        async with factory() as s:
            bind = s.get_bind()
            async with bind.begin() as conn:  # type: ignore[union-attr]
                await conn.run_sync(Base.metadata.create_all)
        async with factory() as s:
            for model in (Charge, IdempotencyKey, Order):
                await s.execute(delete(model))
            await s.commit()

    _run(go())
