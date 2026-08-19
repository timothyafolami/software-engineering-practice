// Topic 5 cross-check: the same property, in fast-check.
//
//   npx vitest run
//
// WHAT TO LOOK FOR: fast-check prints the counterexample, the SEED and the PATH
// to replay it. Record the shrunk counterexample and compare it with
// Hypothesis's and rapid's. fast-check has real shrinking but is weaker than
// Hypothesis on nested structures -- which is exactly what a list of records is.
import { describe, expect, test } from 'vitest';
import fc from 'fast-check';
import { page, pageComposite, sortDesc, sortDescComposite, walkPages } from './pagination.js';

// The strategy, ported decision for decision:
//  - ties are LIKELY: createdAt drawn from 0..3
//  - uniqueness is on ID, not on createdAt -- making createdAt unique would
//    delete the bug from the input space rather than from the code
//  - the precondition is established by MAPPING, not filtering: filtering for
//    sortedness rejects almost every generated array, and a generator that
//    rejects too much is abandoned rather than merely slow
const rowsArb = (maxCreatedAt) =>
  fc
    .uniqueArray(
      fc.record({ createdAt: fc.integer({ min: 0, max: maxCreatedAt }), id: fc.integer() }),
      { selector: (r) => r.id, maxLength: 10 },
    )
    .map(sortDesc);

function assertExactlyOnce(rows, seen) {
  const count = (xs) => xs.reduce((m, r) => m.set(r.id, (m.get(r.id) ?? 0) + 1), new Map());
  const want = count(rows);
  const got = count(seen);
  for (const [id, n] of want) {
    expect(got.get(id) ?? 0, `row id=${id} appeared the wrong number of times`).toBe(n);
  }
  expect(seen.length).toBe(rows.length);
}

describe('keyset pagination', () => {
  test('NARROW strategy finds the tie bug', () => {
    // EXPECTED TO FAIL. Read the counterexample fast-check prints.
    expect(() =>
      fc.assert(
        fc.property(rowsArb(3), fc.integer({ min: 1, max: 5 }), (rows, limit) => {
          assertExactlyOnce(rows, walkPages(rows, limit, page));
        }),
        { numRuns: 2000 },
      ),
    ).toThrow(/Property failed/);
  });

  test('WIDE range STILL fails, because fast-check biases toward boundaries', () => {
    // The probe's most useful surprise, and the reason to cross-check instead
    // of assume.
    //
    // The expectation going in: widening the timestamp range to something
    // datetime-sized makes ties astronomically unlikely, so the property passes
    // while the bug stays. That holds under UNIFORM sampling and it is topic 5's
    // headline lesson. (Whether Hypothesis itself behaves that way is a separate
    // measurement -- see topic 5's How to run; it was not observed here.)
    // Port the SAME widening to fast-check and it still fails, because
    // fast-check's integer arbitrary
    // draws from a BIASED distribution that oversamples boundary values (0, 1,
    // -1, MAX). Go's rapid does the same thing. Two of the three tools in this
    // cross-check refuse to hide the bug.
    //
    // So the thing that hides the bug is not the declared range. It is the
    // DISTRIBUTION, and every one of these libraries picks a different default.
    //
    // SEED AND RUN COUNT ARE PINNED, and that is a finding rather than
    // housekeeping. At the 2000 runs this test originally used, the biased
    // sampler found the tie on some seeds and not others -- measured here by
    // repeating the whole check with fresh seeds: 24 of 40 checks found it at
    // numRuns=2000, 35 of 40 at 5000, 40 of 40 at 10000. So "still fails" is a
    // statement about a distribution, and asserting it on one unpinned run is a
    // coin flip. A flaky test here is not a local annoyance either: Stryker
    // aborts its whole run when the initial dry run has a failing test, so this
    // one assertion decides whether topic 8's experiment 4 produces a
    // denominator at all. Pin the seed, keep the claim, and record what the
    // unpinned rate was.
    const out = fc.check(
      fc.property(rowsArb(Number.MAX_SAFE_INTEGER), fc.integer({ min: 1, max: 5 }), (rows, limit) => {
        assertExactlyOnce(rows, walkPages(rows, limit, page));
      }),
      { numRuns: 10000, seed: 2026 },
    );
    expect(out.failed, 'a widened range did NOT hide the bug from fast-check').toBe(true);
    // eslint-disable-next-line no-console
    console.log('wide + biased sampling: failed after', out.numRuns, 'runs;',
                'counterexample', JSON.stringify(out.counterexample));
  });

  test('WIDE range + unbiased sampling is what actually hides the bug', () => {
    // `unbiased: true` turns off the boundary oversampling and draws uniformly
    // from the declared range. NOW the tie probability is the one you would
    // compute by hand -- roughly n^2 / (2 * 2^53) per example -- and the
    // property passes over every example drawn while the code is still wrong.
    //
    // This is the honest version of topic 5's "wide strategy passes" row: it is
    // true, and it is true because of the sampling policy rather than the range.
    const out = fc.check(
      fc.property(rowsArb(Number.MAX_SAFE_INTEGER), fc.integer({ min: 1, max: 5 }), (rows, limit) => {
        assertExactlyOnce(rows, walkPages(rows, limit, page));
      }),
      // Same pinned seed and run count as the biased check above, so the two
      // rows differ by ONE flag -- `unbiased` -- and nothing else.
      { numRuns: 10000, seed: 2026, unbiased: true },
    );
    expect(out.failed, 'unbiased sampling over a 2^53 range found a tie; rerun and record it').toBe(false);
  });

  test('the composite cursor is complete under ties', () => {
    fc.assert(
      fc.property(
        fc
          .uniqueArray(
            fc.record({ createdAt: fc.integer({ min: 0, max: 3 }), id: fc.integer() }),
            { selector: (r) => r.id, maxLength: 10 },
          )
          .map(sortDescComposite),
        fc.integer({ min: 1, max: 5 }),
        (rows, limit) => {
          assertExactlyOnce(rows, walkPages(rows, limit, pageComposite));
        },
      ),
      { numRuns: 2000 },
    );
  });

  test('the shrunk counterexample, printed for the record', () => {
    // Runs the failing property WITHOUT assertions swallowing it, so the
    // counterexample lands in the test output where you can copy it into
    // topic 5's shrinker table.
    const out = fc.check(
      fc.property(rowsArb(3), fc.integer({ min: 1, max: 5 }), (rows, limit) => {
        assertExactlyOnce(rows, walkPages(rows, limit, page));
      }),
      { numRuns: 2000 },
    );
    expect(out.failed).toBe(true);
    // eslint-disable-next-line no-console
    console.log('fast-check shrunk counterexample:', JSON.stringify(out.counterexample),
                '| runs before failure:', out.numRuns, '| seed:', out.seed);
  });

  test('examples, for Stryker to have something to kill', () => {
    expect(page([{ createdAt: 3, id: 1 }, { createdAt: 2, id: 2 }], null, 1))
      .toEqual([[{ createdAt: 3, id: 1 }], 3]);
    expect(page([{ createdAt: 3, id: 1 }], null, 5)).toEqual([[{ createdAt: 3, id: 1 }], null]);
    expect(() => page([], null, 0)).toThrow(RangeError);
    expect(sortDesc([{ createdAt: 1, id: 1 }, { createdAt: 2, id: 2 }])[0].createdAt).toBe(2);
    expect(sortDescComposite([{ createdAt: 1, id: 1 }, { createdAt: 1, id: 2 }])[0].id).toBe(2);
    expect(walkPages([{ createdAt: 2, id: 1 }, { createdAt: 1, id: 2 }], 1).length).toBe(2);
  });
});
