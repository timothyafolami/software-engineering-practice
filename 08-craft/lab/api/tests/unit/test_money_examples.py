"""Topic 8, row 2 for the money module. Chosen inputs, real assertions.

Every case here is non-negative, because that is what an author writes when the
function is called from a checkout flow. The refund path is the one nobody
thought of, and it is exactly where the bug is.
"""
from __future__ import annotations

import pytest

from app.core.money import split_evenly, split_evenly_fixed


@pytest.mark.parametrize("total,n,expected", [
    (100, 4, [25, 25, 25, 25]),
    (10, 3, [4, 3, 3]),
    (0, 3, [0, 0, 0]),
    (1, 3, [1, 0, 0]),
    (7, 2, [4, 3]),
])
def test_split_examples(total, n, expected):
    assert split_evenly(total, n) == expected
    assert sum(split_evenly(total, n)) == total


def test_rejects_zero_parts():
    with pytest.raises(ValueError):
        split_evenly(100, 0)


def test_fixed_agrees_with_original_on_non_negative_totals():
    """A differential property, written as examples. It holds -- which is why
    nobody noticed the two implementations disagree about refunds."""
    for total in range(0, 50):
        for n in range(1, 7):
            assert sorted(split_evenly(total, n)) == sorted(split_evenly_fixed(total, n))
