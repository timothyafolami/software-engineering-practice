"""Topic 8, experiment 1: 100% statement coverage with zero assertions.

WHAT THIS DEMONSTRATES: every test below calls the function and checks nothing.
`coverage report` will say 100% on `core/money.py` and near it on
`core/pagination.py`. Then turn on `--branch` and watch the number move.

This is Goodhart's law with a dashboard: the cheapest way to raise a coverage
gate is to write tests with no assertions, every organisation discovers this,
and most respond by raising the threshold.

WHAT TO LOOK FOR: run
    make coverage
and read BOTH columns. Record what you actually get rather than what you
expected: on these two modules the assertion-free tests reach 100% statement
AND 100% branch, because every branch here is reachable by simply calling the
function with a couple of inputs. That is a sharper result than "branch catches
what statement missed", not a weaker one -- it means neither coverage flavour
has anything left to say, and the only measurement that can still distinguish
this file from a real suite is mutation.

So run `make mutation` with ONLY this file as the suite and record the score.
That is row 1 of topic 8's table, and the gap between 100% coverage and that
score is the number that inoculates you against a coverage gate permanently.

(If your run shows branch BELOW statement, do not "fix" it -- record it. The
difference means your modules have a branch these calls do not reach, which is
the ordinary case and the one topic 8 describes. Both outcomes are findings;
only an unrecorded one is a failure.)

These tests are deliberately excluded from the example-test run; see
tests/unit/test_pagination_examples.py.
"""
from __future__ import annotations

import pytest

from app.core.money import split_evenly, split_evenly_fixed
from app.core.pagination import (
    Row, page, page_composite, page_inclusive, sorted_desc, sorted_desc_composite, walk_pages,
)

pytestmark = pytest.mark.no_assertions


def test_money_executes():
    split_evenly(100, 3)
    split_evenly_fixed(100, 3)
    with pytest.raises(ValueError):
        split_evenly(100, 0)
    with pytest.raises(ValueError):
        split_evenly_fixed(100, 0)


def test_pagination_executes():
    rows = sorted_desc([Row(created_at=t, id=t) for t in range(5)])
    page(rows, None, 2)
    page(rows, 3, 2)
    page_inclusive(rows, None, 2)
    page_inclusive(rows, 3, 2)
    page_composite(sorted_desc_composite(rows), None, 2)
    page_composite(sorted_desc_composite(rows), (3, 3), 2)
    walk_pages(rows, 2)
    tied = sorted_desc([Row(created_at=0, id=0), Row(created_at=0, id=1)])
    with pytest.raises(RuntimeError):
        walk_pages(tied, 1, impl=page_inclusive, max_pages=3)
    for fn in (page, page_inclusive):
        with pytest.raises(ValueError):
            fn(rows, None, 0)
    with pytest.raises(ValueError):
        page_composite(rows, None, 0)
