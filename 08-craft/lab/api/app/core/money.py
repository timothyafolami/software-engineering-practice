"""Topic 5 warm-up: the money split, and what a refund does to it.

WHAT THIS DEMONSTRATES: a two-clause property --
    sum(parts) == total_cents          (nothing is created or destroyed)
    max(parts) - min(parts) <= 1       ("evenly" means what it says)
-- that holds for every non-negative total and breaks on negative ones. The
interesting part is that only ONE of the two clauses fires: the spread stays
within a cent while the total quietly changes. A property test that asserted
only the "evenly" half would report success on a function that loses money.

WHAT TO LOOK FOR: nothing here prints. Run
`pytest tests/properties/test_money.py` twice -- once with `st.integers()` and
once with `st.integers(min_value=0)` -- and compare. The unrestricted run is
the one that hands you a refund.
"""
from __future__ import annotations


def split_evenly(total_cents: int, n: int) -> list[int]:
    """Split `total_cents` into `n` parts that sum back to the total.

    Contract, written down before the body -- which is the whole point of the
    exercise, because this is also the property:
      - `sum(split_evenly(t, n)) == t` for every integer `t` and every `n >= 1`
      - `max(parts) - min(parts) <= 1`
      - the parts that get the extra cent come first, so the same input always
        produces the same allocation

    Raises ValueError when `n < 1`: there is no sensible zero-part split, and
    returning `[]` would violate the sum clause silently.
    """
    if n < 1:
        raise ValueError("n must be >= 1")

    base = total_cents // n
    # `//` floors toward negative infinity, so for a negative total `base` is
    # already one step "too low" and `total_cents % n` comes back positive.
    # The author knew that, found the resulting allocation surprising, and
    # compensated with abs() -- in one of the two places it needed
    # compensating in. Every non-negative total is unaffected, which is why
    # this shipped.
    remainder = abs(total_cents) % n

    parts = [base] * n
    for i in range(remainder):
        parts[i] += 1
    return parts


def split_evenly_fixed(total_cents: int, n: int) -> list[int]:
    """The same split, correct for refunds.

    Split the magnitude, then re-apply the sign. Now there is exactly one
    place where sign is handled, instead of two places that have to agree.
    """
    if n < 1:
        raise ValueError("n must be >= 1")

    sign = -1 if total_cents < 0 else 1
    base, remainder = divmod(abs(total_cents), n)
    parts = [base] * n
    for i in range(remainder):
        parts[i] += 1
    return [sign * p for p in parts]
