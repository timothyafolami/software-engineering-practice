"""Topic 8, row 2: the example-based tests. Real assertions, chosen inputs.

WHAT THIS DEMONSTRATES: a suite a reviewer would approve. It asserts real
things about real cases -- and it never happens to pick two rows sharing a
timestamp, so `page()`'s bug survives every one of them.

WHAT TO LOOK FOR: run mutation against this file alone and then against this
file plus tests/properties. The delta between those two scores is the argument
for property-based testing that a skeptic will accept, because it is a number
about their own code.
"""
from __future__ import annotations

import pytest

from app.core.pagination import Row, page, page_composite, sorted_desc, sorted_desc_composite, walk_pages


def rows(*timestamps: int) -> list[Row]:
    return sorted_desc([Row(created_at=t, id=i) for i, t in enumerate(timestamps)])


def test_first_page_is_newest_first():
    out, cursor = page(rows(1, 2, 3), None, 2)
    assert [r.created_at for r in out] == [3, 2]
    assert cursor == 2


def test_short_page_ends_the_walk():
    out, cursor = page(rows(1, 2, 3), None, 10)
    assert len(out) == 3
    assert cursor is None


def test_cursor_excludes_the_boundary_row():
    out, _ = page(rows(1, 2, 3), 3, 2)
    assert [r.created_at for r in out] == [2, 1]


def test_walk_yields_every_row_when_timestamps_are_distinct():
    src = rows(1, 2, 3, 4, 5)
    assert sorted(r.id for r in walk_pages(src, 2)) == sorted(r.id for r in src)


def test_limit_must_be_positive():
    with pytest.raises(ValueError):
        page(rows(1), None, 0)


def test_composite_cursor_walk_is_complete_with_ties():
    src = sorted_desc_composite([Row(created_at=1, id=1), Row(created_at=1, id=2), Row(created_at=2, id=3)])
    seen = walk_pages(src, 1, impl=page_composite)
    assert sorted(r.id for r in seen) == [1, 2, 3]


def test_unsorted_input_drops_rows_with_no_tie_present():
    """The trap topic 5 warns about, pinned deterministically and with no Hypothesis.

    `page()` documents a precondition: rows arrive sorted by `created_at`
    descending. Break it and rows go missing -- with a two-row counterexample
    at `limit=1` that looks EXACTLY like the tie bug's and is a different
    defect. Note the timestamps here are DISTINCT, which is the tell: the tie
    bug needs a repeated `created_at` and this failure does not.

    Whoever reads a Hypothesis counterexample without checking for the repeat
    will confirm a bug they never reproduced, which is the single cheapest way
    to get a confident false result out of topic 5's flagship.
    """
    ascending = [Row(created_at=0, id=0), Row(created_at=1, id=1)]   # precondition violated
    assert len({r.created_at for r in ascending}) == 2, "no tie in this input"
    seen = walk_pages(ascending, 1)
    assert [r.id for r in seen] == [0], "the walk stopped early because the input was unsorted"

    assert sorted(r.id for r in walk_pages(sorted_desc(ascending), 1)) == [0, 1]
