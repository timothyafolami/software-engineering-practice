"""Topic 4, the one worth showing people: the assertion is true, the system is broken.

WHAT THIS DEMONSTRATES: a service method that forgets `await session.commit()`,
and a mocked test asserting `session.commit.assert_awaited_once()` -- which
PASSES, because the surrounding `async with session.begin()` block calls commit
on the mock on the way out. The assertion is about the mock, not about the code
under test, and nothing in the tooling distinguishes the two.

WHAT TO LOOK FOR: `test_commit_assertion_passes_anyway` is green while
`test_the_code_under_test_never_committed` proves the method never called it.
Both are true at once. That is the entire lesson.

    pytest tests/unit/test_commit_mocked.py -q
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call


async def save_order_MISSING_COMMIT(session, order) -> None:
    """The bug: adds the row, opens a transaction, never commits it."""
    async with session.begin():
        session.add(order)
    # await session.commit()   <-- the missing line


def session_double():
    """An AsyncMock session whose `begin()` is a real async context manager.

    `begin().__aexit__` commits, exactly as SQLAlchemy's does. Nothing here is
    unfair or contrived -- this is what a hand-rolled session double looks like
    when someone reproduces the framework's behaviour faithfully.
    """
    session = AsyncMock()
    ctx = AsyncMock()

    async def _aexit(exc_type, exc, tb):
        await session.commit()          # the framework commits, not your code
        return False

    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(side_effect=_aexit)
    session.begin = MagicMock(return_value=ctx)
    session.add = MagicMock()
    return session


async def test_commit_assertion_passes_anyway():
    """GREEN. And the method under test contains no call to commit()."""
    session = session_double()
    await save_order_MISSING_COMMIT(session, object())
    session.commit.assert_awaited_once()


async def test_the_code_under_test_never_committed():
    """The same run, asked the question the assertion above cannot ask.

    A source-level check is the only thing here that can tell "somebody called
    commit" apart from "the code under test called commit" -- which is a decent
    summary of why this whole class of assertion is weak.
    """
    import inspect

    source = inspect.getsource(save_order_MISSING_COMMIT)
    assert "session.commit()" not in source.replace("# await session.commit()", "")
