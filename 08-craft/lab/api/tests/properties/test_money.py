"""Topic 5 warm-up: the same property, two strategies, ten minutes.

WHAT THIS DEMONSTRATES: the property is written once. The only thing that
changes between the two runs is the DISTRIBUTION the strategy draws from, and
that alone decides whether the bug is found. This is the flagship's lesson in
miniature, on code small enough to hold in your head.

WHAT TO LOOK FOR: two clauses are asserted and only ONE of them fails. The
spread stays within a cent while the total quietly changes -- so a property
test that had asserted only the "evenly" half would report success on a
function that loses money.

    pytest tests/properties/test_money.py -q
    pytest tests/properties/test_money.py -q --hypothesis-show-statistics
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

from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.core.money import split_evenly, split_evenly_fixed

PARTS = st.integers(min_value=1, max_value=20)


def assert_split_is_honest(fn, total: int, n: int) -> None:
    parts = fn(total, n)
    assert len(parts) == n, "a split into n parts must have n parts"
    # Clause 1: nothing is created or destroyed. This is the one that fails.
    assert sum(parts) == total, f"{fn.__name__}({total}, {n}) = {parts} sums to {sum(parts)}"
    # Clause 2: "evenly" means within one cent. This one holds throughout,
    # which is exactly why a one-clause property would have shipped the bug.
    assert max(parts) - min(parts) <= 1, f"{fn.__name__}({total}, {n}) = {parts} is not even"


@given(total=st.integers(min_value=0, max_value=10_000_000), n=PARTS)
def test_split_evenly_over_non_negative_totals(total, n):
    """PASSES. This is the run an author writes, because a total is a price."""
    assert_split_is_honest(split_evenly, total, n)


@given(total=st.integers(), n=PARTS)
@example(total=-1, n=3)   # pinned: the shrunk counterexample, kept forever.
                          # Hypothesis's example database is not available in
                          # CI (the auto-loaded `ci` profile sets database=None),
                          # so without this line a failure found in CI is not
                          # reproduced on the next run.
def test_split_evenly_over_every_integer_total(total, n):
    """FAILS. `st.integers()` is unrestricted, so it eventually hands you a
    refund -- and a refund is the input nobody wrote an example for.

    Expected shrunk counterexample: total=-1, n=3, giving [0, -1, -1], which
    sums to -2. Two cents appeared out of nowhere on a one-cent refund.
    """
    assert_split_is_honest(split_evenly, total, n)


@given(total=st.integers(), n=PARTS)
@settings(max_examples=500)
def test_split_evenly_fixed_holds_over_every_integer_total(total, n):
    """The fix, held to the same property over the same unrestricted domain."""
    assert_split_is_honest(split_evenly_fixed, total, n)


@given(total=st.integers(min_value=0, max_value=10_000), n=PARTS)
def test_fixed_agrees_with_original_where_the_original_was_correct(total, n):
    """A differential property: the fix must not change behaviour it was not
    meant to change. Without this, "fix the refund case" is free to quietly
    reallocate every existing invoice."""
    assert sorted(split_evenly(total, n)) == sorted(split_evenly_fixed(total, n))
