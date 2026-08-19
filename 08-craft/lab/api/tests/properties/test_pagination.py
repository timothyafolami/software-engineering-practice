"""Topic 5 flagship: walking the pages must yield every row exactly once.

WHAT THIS DEMONSTRATES: one property, one implementation, THREE strategies --
and the strategy alone decides whether the bug is found:

  narrow (created_at in 0..3)   ties are common          -> fails in a few examples
  wide   (0..2^53)              ties are ~impossible IF
                                sampling is uniform -- and
                                Hypothesis's is not      -> RECORD the outcome
  unsorted (no .map)            violates the docstring's precondition -> fails
                                for the WRONG reason, with a counterexample
                                that looks exactly like the right one

WHAT TO LOOK FOR: the counterexample must contain a REPEATED created_at. If it
does not, you found a precondition violation rather than the tie bug, and you
are about to "confirm" a bug you have not reproduced. That is the single easiest
way to get a confident false result out of this experiment.

    pytest tests/properties/test_pagination.py -q                          # narrow: fails
    PAGINATION_STRATEGY=wide pytest tests/properties/test_pagination.py -rX  # record what it does
    pytest tests/properties/test_pagination.py -q --hypothesis-show-statistics
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

import os
from collections import Counter

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from app.core.pagination import (
    Row, page, page_composite, page_inclusive, sorted_desc, sorted_desc_composite, walk_pages,
)

LIMITS = st.integers(min_value=1, max_value=5)

# --- the two strategies, and the .map() that carries the topic ---------------

def _rows(timestamps: st.SearchStrategy[int]) -> st.SearchStrategy[list[Row]]:
    return st.lists(
        st.builds(Row, created_at=timestamps, id=st.integers()),
        # unique_by ID, NOT by created_at. Making created_at unique would delete
        # the bug from the INPUT SPACE rather than from the code, and the test
        # would pass while the defect shipped.
        unique_by=lambda r: r.id,
        max_size=12,
    ).map(sorted_desc)
    # ^ page()'s docstring states a precondition: rows arrive sorted by
    #   created_at descending, because in production they came from
    #   ORDER BY created_at DESC. A strategy that generates arbitrary order
    #   tests a function that does not exist.
    #
    #   .map rather than .filter, for a mechanical reason: filtering for
    #   sortedness rejects almost every generated list, and Hypothesis abandons
    #   a run that filters too much. Mapping keeps every example AND preserves
    #   shrinking, because Hypothesis shrinks the underlying list and re-applies
    #   the map.

NARROW = _rows(st.integers(min_value=0, max_value=3))
WIDE = _rows(st.integers(min_value=0, max_value=2**53))   # st.datetimes()'s tie
                                                          # probability, without
                                                          # the datetime plumbing
UNSORTED = st.lists(
    st.builds(Row, created_at=st.integers(min_value=0, max_value=3), id=st.integers()),
    unique_by=lambda r: r.id, max_size=12,
)

ACTIVE = WIDE if os.environ.get("PAGINATION_STRATEGY", "narrow") == "wide" else NARROW


def assert_walk_is_complete(rows, limit, impl=page) -> None:
    """Every input row appears in the walk exactly once. No more, no less."""
    seen = walk_pages(rows, limit, impl=impl)
    counts = Counter(r.id for r in seen)
    expected = Counter(r.id for r in rows)
    missing = expected - counts
    extra = counts - expected
    assert not missing, (
        f"rows dropped: {sorted(missing)} -- "
        f"created_at values in input: {[r.created_at for r in rows]}, limit={limit}"
    )
    assert not extra, f"rows served twice: {sorted(extra)}"


@given(rows=ACTIVE, limit=LIMITS)
@settings(max_examples=2000, deadline=None)
@example(                                     # the shrunk counterexample, pinned.
    rows=[Row(created_at=0, id=0), Row(created_at=0, id=1)],
    limit=1,
)
def test_pagination_yields_every_row_once(rows, limit):
    """THE FLAGSHIP. Fails under `narrow`, passes under `wide`, same code.

    The bug: `WHERE created_at < :cursor` skips every row that shares the
    boundary timestamp with the last row of the previous page. Minimum
    counterexample is two rows with equal created_at and limit=1.
    """
    assert_walk_is_complete(rows, limit)


@pytest.mark.xfail(
    strict=False,
    reason="the OUTCOME of this run is the measurement, not the expectation -- "
    "read it and write it into topic 5's table",
)
@given(rows=WIDE, limit=LIMITS)
@settings(max_examples=2000, deadline=None)
def test_wide_strategy_is_the_measurement_not_the_expectation(rows, limit):
    """Same property, same code, a 2^53-wide timestamp range. Does it still fail?

    UNDER UNIFORM SAMPLING the answer is no: a timestamp drawn uniformly from a
    2^53-wide range collides with probability ~n^2/2^54 per example, which over
    2000 examples of at most 12 rows is on the order of 1e-11. That derivation
    is arithmetic and it is sound.

    What the derivation does NOT establish is what Hypothesis actually does,
    because `st.integers(min_value, max_value)` does not sample uniformly -- it
    oversamples boundaries and small magnitudes, the same policy that made both
    of this topic's cross-check probes keep finding the bug at their widest
    range (see `probes/node-fc`, where only `unbiased: true` hides it, and
    `probes/go-rapid`, where widening hides nothing at all).

    So this is marked non-strict xfail: green whichever way it lands, because
    the result is a number you record rather than a gate. Run it with `-rX` and
    put the outcome in topic 5's table. If it FAILS, the lesson is not weaker --
    it is that the range you declare and the distribution you get are different
    things, which is a sharper statement of this topic's thesis.
    """
    assert_walk_is_complete(rows, limit)


@pytest.mark.xfail(
    strict=True,
    reason="precondition violation, kept as a RUNNABLE demonstration -- run with -rX "
    "to read the counterexample and check it against the tie bug's",
)
@given(rows=UNSORTED, limit=LIMITS)
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
def test_unsorted_input_fails_for_a_DIFFERENT_reason(rows, limit):
    """The trap, actually executed rather than described in a note.

    This fails too, with a tiny counterexample that looks exactly like the right
    one -- "two rows, limit=1, a row missed". It is NOT the tie bug: it is a
    precondition violation, because `page()` documents that its input arrives
    sorted by `created_at` descending.

    `strict=True` matters. The property really is run against `page()` on every
    example; xfail only decides how the RESULT is reported. A version that
    called `pytest.xfail()` in the body would short-circuit before touching the
    code under test, and would demonstrate nothing at all -- which is the exact
    failure mode topic 4 is about, arriving inside topic 5.

    The tell that separates the two failures: the tie bug's counterexample
    contains a REPEATED `created_at`; this one does not need to.
    `tests/unit/test_pagination_examples.py::test_unsorted_input_drops_rows_with_no_tie_present`
    pins that distinction deterministically, with no Hypothesis at all.
    """
    assert_walk_is_complete(rows, limit)


@given(rows=NARROW, limit=LIMITS)
@settings(max_examples=2000, deadline=None)
def test_inclusive_fix_duplicates_forever(rows, limit):
    """The property that catches the OBVIOUS fix.

    Changing `<` to `<=` stops dropping rows and starts re-serving them, and on
    an all-ties page it never terminates. `walk_pages` raises rather than
    hanging, so this reports as a readable failure instead of a stuck CI job.
    Note that a property asserting only "no row is missing" would call `<=` a
    fix; it takes the "exactly once" half to catch it.
    """
    assert_walk_is_complete(rows, limit, impl=page_inclusive)


@given(
    rows=st.lists(
        st.builds(Row, created_at=st.integers(0, 3), id=st.integers()),
        unique_by=lambda r: r.id, max_size=12,
    ).map(sorted_desc_composite),
    limit=LIMITS,
)
@settings(max_examples=2000, deadline=None)
def test_composite_cursor_is_complete_under_ties(rows, limit):
    """The real fix, held to the same property under the tie-heavy strategy.

    `(created_at, id)` is a UNIQUE sort key, and a cursor over a unique key can
    be a strict bound without skipping anything. In Postgres this is
    `WHERE (created_at, id) < (:ts, :id)` and it uses a composite index cleanly.
    """
    assert_walk_is_complete(rows, limit, impl=page_composite)
