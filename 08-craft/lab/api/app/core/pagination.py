"""Topic 5 flagship: keyset pagination, and the two rows that share a timestamp.

WHAT THIS DEMONSTRATES: `page()` mirrors the SQL every "load more" endpoint
eventually gets rewritten into --
    WHERE created_at < :cursor ORDER BY created_at DESC LIMIT :limit
-- which is the correct fix for OFFSET-based pagination on a large table, and
which silently drops rows whenever two rows share the boundary timestamp.

WHAT TO LOOK FOR: `walk_pages()` below is the property's subject. Walking the
pages until `next_cursor is None` must yield every input row exactly once. It
does not. The minimum counterexample is two rows sharing a `created_at` and
`limit=1`; topic 5's Hypothesis strategy is tuned to produce that tie and
topic 9 renames `cursor` so that a reader spots it without running anything.

Three implementations live here on purpose:
  page()            the shipped bug: `<` skips ties
  page_inclusive()  the "obvious" fix: `<=` duplicates ties, possibly forever
  page_composite()  the real fix: a (created_at, id) row-value cursor
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Row:
    """A row as the query returns it. `id` is unique; `created_at` is not."""

    created_at: int
    id: int


Cursor = int | None
Composite = tuple[int, int] | None


def page(rows: Sequence[Row], cursor: Cursor, limit: int) -> tuple[list[Row], Cursor]:
    """Return one page of `rows` plus the cursor for the next page.

    PRECONDITION: `rows` is sorted by `created_at` descending, because in
    production it arrived from `ORDER BY created_at DESC`. Passing an unsorted
    list tests a function that does not exist -- see topic 5's note about the
    `.map()` on the strategy.

    Mirrors: WHERE created_at < :cursor ORDER BY created_at DESC LIMIT :limit
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if cursor is not None:
        rows = [r for r in rows if r.created_at < cursor]
    out = list(rows[:limit])
    next_cursor = out[-1].created_at if len(out) == limit else None
    return out, next_cursor


def page_inclusive(rows: Sequence[Row], cursor: Cursor, limit: int) -> tuple[list[Row], Cursor]:
    """The `<=` "fix". Included so the duplicate-forever failure is runnable.

    Changing `<` to `<=` stops skipping tied rows and starts re-serving them:
    the boundary row satisfies its own cursor, so a walk over rows that all
    share one timestamp never terminates.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if cursor is not None:
        rows = [r for r in rows if r.created_at <= cursor]
    out = list(rows[:limit])
    next_cursor = out[-1].created_at if len(out) == limit else None
    return out, next_cursor


def page_composite(
    rows: Sequence[Row], before: Composite, limit: int
) -> tuple[list[Row], Composite]:
    """The real fix: bound on the whole sort key, not on its first column.

    `before` is the full sort key of the last row already delivered -- the
    tuple `(created_at, id)`. Tuple comparison is exactly Postgres's row-value
    comparison, `WHERE (created_at, id) < (:ts, :id)`, which uses a composite
    index on `(created_at DESC, id DESC)` cleanly.

    PRECONDITION: `rows` is sorted by `(created_at, id)` descending. That is a
    stronger precondition than `page()`'s and it is the point -- the sort key
    has to be unique before a cursor over it can be.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if before is not None:
        rows = [r for r in rows if (r.created_at, r.id) < before]
    out = list(rows[:limit])
    next_before = (out[-1].created_at, out[-1].id) if len(out) == limit else None
    return out, next_before


def walk_pages(rows: Sequence[Row], limit: int, *, impl=page, max_pages: int = 1000) -> list[Row]:
    """Walk every page until the implementation says there is no next cursor.

    This is the harness the property runs against, and two details in it are
    load-bearing:

      - An empty final page is NOT a failure. A page shorter than `limit`
        legitimately ends the walk; treating it as an error produces a
        one-row counterexample that looks like the tie bug and is not.
      - `max_pages` exists because `page_inclusive` genuinely does not
        terminate. Raising here turns "the test hangs" into a readable
        failure that names the implementation.
    """
    seen: list[Row] = []
    cursor = None
    for _ in range(max_pages):
        out, cursor = impl(rows, cursor, limit)
        seen.extend(out)
        if cursor is None:
            return seen
    raise RuntimeError(
        f"{getattr(impl, '__name__', impl)} did not terminate within {max_pages} pages "
        f"({len(seen)} rows emitted from {len(rows)} inputs) -- this is the "
        f"duplicate-forever failure, not a harness timeout"
    )


def sorted_desc(rows: Iterable[Row]) -> list[Row]:
    """Establish `page()`'s precondition. Used by the strategy's `.map()`."""
    return sorted(rows, key=lambda r: r.created_at, reverse=True)


def sorted_desc_composite(rows: Iterable[Row]) -> list[Row]:
    """Establish `page_composite()`'s stronger precondition."""
    return sorted(rows, key=lambda r: (r.created_at, r.id), reverse=True)
