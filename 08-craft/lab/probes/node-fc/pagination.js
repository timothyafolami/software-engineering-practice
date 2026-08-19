// A line-for-line port of app/core/pagination.py, so three shrinkers get the
// SAME bug -- and so topic 8 can run Stryker over the same algorithm mutmut ran
// over, and compare denominators.
//
// WHAT THIS DEMONSTRATES: nothing new about pagination. Everything about
// tooling. Read pagination.test.js for the property, and note that the code
// below is deliberately as close to the Python as JavaScript allows.
//
// WHAT TO LOOK FOR: `npx stryker run` reports a mutant TOTAL that differs from
// mutmut's on the same algorithm, because Stryker discards mutants that would
// not compile. That quietly changes the denominator, which means a Stryker score
// and a mutmut score are NOT directly comparable -- knowing that before you
// compare two teams' numbers saves an argument.

/** @typedef {{createdAt: number, id: number}} Row */

/**
 * One page of rows plus the cursor for the next page.
 *
 * PRECONDITION: `rows` is sorted by createdAt descending.
 * Mirrors: WHERE created_at < :cursor ORDER BY created_at DESC LIMIT :limit
 *
 * @param {Row[]} rows @param {number|null} cursor @param {number} limit
 * @returns {[Row[], number|null]}
 */
export function page(rows, cursor, limit) {
  if (limit < 1) throw new RangeError('limit must be >= 1');
  let filtered = rows;
  if (cursor !== null && cursor !== undefined) {
    filtered = rows.filter((r) => r.createdAt < cursor); // <-- strict, on a non-unique column
  }
  const out = filtered.slice(0, limit);
  const nextCursor = out.length === limit ? out[out.length - 1].createdAt : null;
  return [out, nextCursor];
}

/** The real fix: bound on the whole sort key. Mirrors (created_at, id) < (:ts, :id). */
export function pageComposite(rows, before, limit) {
  if (limit < 1) throw new RangeError('limit must be >= 1');
  let filtered = rows;
  if (before !== null && before !== undefined) {
    filtered = rows.filter(
      (r) => r.createdAt < before.createdAt
        || (r.createdAt === before.createdAt && r.id < before.id),
    );
  }
  const out = filtered.slice(0, limit);
  const next = out.length === limit ? out[out.length - 1] : null;
  return [out, next];
}

export const sortDesc = (rows) => [...rows].sort((a, b) => b.createdAt - a.createdAt);

export const sortDescComposite = (rows) =>
  [...rows].sort((a, b) => (b.createdAt - a.createdAt) || (b.id - a.id));

/** Walk every page until the implementation says there is no next cursor.
 *  An empty final page is NOT a failure; `maxPages` turns a non-terminating
 *  implementation into a readable error instead of a hung test run. */
export function walkPages(rows, limit, impl = page, maxPages = 1000) {
  const seen = [];
  let cursor = null;
  for (let i = 0; i < maxPages; i += 1) {
    const [out, next] = impl(rows, cursor, limit);
    seen.push(...out);
    if (next === null) return seen;
    cursor = next;
  }
  throw new Error(`walk did not terminate in ${maxPages} pages`);
}
