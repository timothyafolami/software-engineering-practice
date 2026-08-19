"""The regression test for topic 5's flagship bug. Subject of `make regression`.

WHAT THIS DEMONSTRATES: the shrunk counterexample, pinned as a permanent test.
Hypothesis saves failing examples to `.hypothesis/examples` and replays them
first -- but the auto-loaded `ci` profile sets `database=None`, so in CI there is
no replay and a failure found there is not reproduced on the next run. That is
the strongest argument for writing the counterexample into the source, and this
file is what that looks like.

WHAT TO LOOK FOR: this test uses NO Hypothesis at all. A regression test should
be the cheapest, most deterministic thing that can detect the bug; the generator
found the input, and its job is done.

    make regression BUG=pagination-ties
"""
from __future__ import annotations

from collections import Counter

from app.core.pagination import (
    Row, page_composite, sorted_desc, sorted_desc_composite, walk_pages,
)

# The exact counterexample Hypothesis shrank to: two rows, one shared
# timestamp, limit=1. Verified by exhaustive search to be the minimum over
# (row count, limit) that reproduces the drop.
TIED = [Row(created_at=0, id=0), Row(created_at=0, id=1)]


def test_composite_cursor_does_not_drop_rows_that_share_a_timestamp():
    """FAILS against the pre-fix source, where `page_composite` did not exist
    and the cursor bounded `created_at` alone."""
    rows = sorted_desc_composite(TIED)
    seen = walk_pages(rows, 1, impl=page_composite)
    assert Counter(r.id for r in seen) == Counter(r.id for r in rows), (
        "a row sharing the boundary timestamp was skipped: `WHERE created_at < :c` "
        "excludes every row tied with the last row of the previous page"
    )


def test_composite_cursor_does_not_serve_a_row_twice():
    """The other half, and it is a different property.

    "No row is missing" alone would accept the `<=` version, which drops nothing
    and duplicates everything. It takes "exactly once" to reject both directions
    with one assertion.
    """
    rows = sorted_desc_composite(TIED + [Row(created_at=1, id=2)])
    seen = walk_pages(rows, 1, impl=page_composite)
    assert len(seen) == len(rows)
    assert len({r.id for r in seen}) == len(seen)


def test_the_original_bug_is_still_reproducible():
    """Pins the DEFECT, not just the fix.

    `page()` is kept in the module as the pre-fix implementation, and this test
    asserts it still drops the tied row. If someone quietly "fixes" `page()`,
    this fails and tells them the demonstration they broke -- which is a
    genuinely useful thing for a teaching repository to guard, and a genuinely
    bad thing to do in production code.
    """
    from app.core.pagination import page

    rows = sorted_desc(TIED)
    seen = walk_pages(rows, 1, impl=page)
    assert len(seen) == 1, "page() no longer reproduces the tie bug this lab is about"
