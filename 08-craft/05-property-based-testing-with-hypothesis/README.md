# Layer 8 · Topic 5 — Property-based testing with Hypothesis (the flagship)

### The takeaway (read this first)

**The one idea:** stop writing examples and start writing the *invariant*. You
state a property that must hold for all inputs; the tool searches the input
space for a counterexample, then shrinks it to the smallest one that still
fails — and the shrunk counterexample is usually so small that it explains the
bug by itself.

**Why it matters in practice:** the roadmap calls property-based testing
criminally underused and it is right. Nearly everyone who adopts it has the
same experience in the first week: the tool finds a real bug in code they had
already tested, reviewed and shipped. This topic is engineered so that happens
to you on purpose, with a bug that is genuinely common in production Python.

**You'll know it landed when:** your instinct on seeing a new function is to
ask "what must be true for every input" before "what should I pass it," and
you can tell *in advance* whether a given strategy is capable of finding a
given bug.

This is the topic that builds the layer's ownership test: state a property for
a function you wrote, then watch Hypothesis produce a two-element
counterexample for it.

## The concept

An example test asserts `f(3) == 6`. A property asserts something structural
about `f` for all inputs. Four shapes cover most real code:

1. **Round-trip.** `decode(encode(x)) == x`. Serializers, cursors, tokens,
   anything with a "parse" and a "format".
2. **Invariant.** Something is always true of the output: sums balance, no
   duplicates, sorted stays sorted, the state machine never reaches an illegal
   state.
3. **Oracle / differential.** The fast implementation agrees with the obvious
   slow one; the new query agrees with the old one. The most under-used
   property in performance work, and it applies directly to a latency project:
   "the optimized version returns the same rows as the original, for all
   inputs."
4. **Metamorphic.** Changing the input in a known way changes the output in a
   known way: adding an element never decreases the count; filtering then
   sorting equals sorting then filtering.

Then the part everyone skips: **shrinking is why this works.** Random inputs
alone find bugs you cannot debug. Hypothesis reduces the failing example
toward a minimum — fewer list elements, smaller integers, simpler strings — so
what lands in your terminal is two rows and `limit=1` rather than four hundred
rows of noise. It also saves the failing example to `.hypothesis/examples` and
replays it first on the next run, which quietly gives you topic 8's "every bug
fix gets a test that fails before the fix" rule for free.

Four Hypothesis defaults are load-bearing in this topic, and all four are
documented in the library's own settings module rather than folklore:

- `max_examples` is **100** — far too few to hit a rare tie; the flagship
  raises it.
- `deadline` is **200 ms per example**; anything touching a container flakes
  against it, so set `deadline=None` there, and only there.
- Hypothesis loads a **`ci` profile automatically when it detects CI**, which
  sets `deadline=None`, `derandomize=True`, `print_blob=True` **and
  `database=None`**. That last one is the surprise: in CI there is no
  saved-example replay, so a failure found there is not automatically
  reproduced on the next run — the strongest argument for pinning shrunk
  counterexamples with `@example(...)` in the source.
- The example database lives in `.hypothesis/examples` and falls back to
  in-memory when that path is not writable, which is what a fresh container
  gives you every run.

The skill in all of this is **strategy design**, and the characteristic
failure is a strategy whose distribution cannot express the bug. That is the
most important sentence in this topic, so it gets its own experiment.

Worth knowing without reaching for it first: Hypothesis supports alternative
backends via `@settings(backend="crosshair")`, swapping random generation for
CrossHair's SMT-based symbolic execution. Explicitly experimental and slow,
and it finds needle-in-a-haystack conditions (`if x == 123456789`) that random
generation essentially never will.

## How each language actually gets there

Six languages, and the variable is **shrinking**: whether the tool reduces a
failure to something a human can read, and what it costs to get there. Every
one of these ecosystems can generate random inputs; they differ almost
entirely in what they hand you *after* the failure.

**Python (your stack).** Hypothesis, and it is not close — the best
property-based testing library in any mainstream language. `@given` plus
`strategies`, `@settings(...)`, `@example(...)` to pin a regression
permanently, `.map()`/`.filter()`/`st.builds()` to shape a strategy, and
`RuleBasedStateMachine` for sequences of operations. Shrinking is integrated
with generation — the internal representation is a choice sequence, so
shrinking the *bytes* shrinks the *structure* — which is why its
counterexamples come out minimal rather than merely smaller.

**Node.** `fast-check` is the closest analogue — arbitraries instead of
strategies, real shrinking, works under Jest and Vitest, and `fc.assert`
prints a counterexample plus the seed and path to replay it. Weaker shrinking
than Hypothesis on nested structures, which the cross-check below shows you.

**Go.** `testing/quick` in the standard library is frozen and has **no
shrinking** — do not use it. Go's real answer is native coverage-guided
fuzzing (`go test -fuzz`, since 1.18), a genuinely different technique: it
mutates a seed corpus and keeps inputs that reach new code paths, rather than
sampling from a declared distribution. Better than Hypothesis at finding
parser crashes on bytes, worse at expressing a structured invariant over
typed records. `pgregory.net/rapid` fills the Hypothesis-shaped gap and does
shrink.

**Rust.** Two traditions, and the split is instructive. `proptest` is the
Hypothesis descendant (strategies, integrated shrinking); `quickcheck` is the
Haskell descendant (type-directed `Arbitrary`, a separate `shrink` method you
can get wrong). Separately, `cargo-fuzz` + `arbitrary` gives you libFuzzer
with structured input decoding — Go's coverage-guided idea plus a derive macro
that turns raw bytes into your types. Rust is the only language here where
both traditions are first-class and comparable on one bug.

**Java.** `jqwik` is the mature property library (JUnit 5, `@Property`,
`@ForAll`, shrinking that reports the shrunk sample beside the original).
Java's more interesting contribution is elsewhere: it has the strongest
*stateful* testing culture, because its concurrency tooling (jcstress, Loom's
cheap threads) makes generating interleavings practical — which is exactly
what the idempotency experiment below needs.

**C++.** `RapidCheck` is the QuickCheck port with shrinking; libFuzzer and AFL
cover the coverage-guided half. The reason to care is that property tests and
sanitizers (`-fsanitize=address,undefined`) compose: the property finds the
input, the sanitizer explains why it was fatal, and together they catch memory
bugs no assertion you wrote would have caught. No managed language here has an
equivalent pairing.

## The experiment

### Warm-up: the money split

`core/money.py` has `split_evenly(total_cents: int, n: int) -> list[int]`,
implemented with `//` and a remainder loop. The property is two clauses:
`sum(parts) == total_cents`, and `max(parts) - min(parts) <= 1`. Run it twice,
once with `st.integers()` and once with `st.integers(min_value=0)`. The
unrestricted version hands you a negative total — a refund — and Python's
floor division does not do what the author assumed for negative numbers. Ten
minutes, and it makes the flagship legible.

### Flagship: keyset pagination

`core/pagination.py`:

```python
def page(rows, cursor, limit):
    """rows sorted by created_at DESC. Mirrors:
       WHERE created_at < :cursor ORDER BY created_at DESC LIMIT :limit"""
    if cursor is not None:
        rows = [r for r in rows if r.created_at < cursor]
    out = rows[:limit]
    next_cursor = out[-1].created_at if len(out) == limit else None
    return out, next_cursor
```

This is the shape of every "load more" endpoint you have ever worked on, and
it is the *correct* fix for `OFFSET`-based pagination on a large table — which
is why it is probably already on a latency project's list somewhere.

The property: **walking the pages until `next_cursor is None` yields every row
exactly once.**

```python
rows_strategy = (
    st.lists(
        st.builds(Row, created_at=st.integers(0, 3), id=st.integers()),
        unique_by=lambda r: r.id,
    )
    # page() documents "rows sorted by created_at DESC" as a precondition.
    # Without this the strategy violates the precondition before it can
    # produce a tie, and the failure you get is not the bug you came for.
    .map(lambda rs: sorted(rs, key=lambda r: r.created_at, reverse=True))
)

@given(rows=rows_strategy, limit=st.integers(min_value=1, max_value=5))
@settings(max_examples=2000)
def test_pagination_yields_every_row_once(rows, limit):
    ...
```

Two decisions in that strategy carry the whole topic:

**The `.map(...)` that sorts.** `page()`'s docstring states a precondition —
rows arrive sorted by `created_at` descending, because in production they came
out of `ORDER BY created_at DESC`. A strategy that generates arbitrary order
tests a function that does not exist. It will fail on a two-row *unsorted*
list long before it ever produces a tie, and the failure will look like the
bug: two rows, `limit=1`, a row missed. You will "confirm" the tie bug without
having reproduced it. `.map()` is the right tool rather than `.filter()`
because filtering for sortedness rejects almost every generated list and
Hypothesis will abandon the run for filtering too much; mapping keeps every
example and preserves shrinking, because Hypothesis shrinks the underlying
list and re-applies the map.

**`st.integers(0, 3)` for the timestamp.** Deliberate, and it is the lesson.
Widen the range and the tie probability collapses: sampled *uniformly* from a
2^53-wide range, two of twelve rows collide with probability on the order of
n²/2⁵⁴ per example, so 2,000 examples find nothing and **the test is green
while the bug is still there.** That arithmetic is sound. What it does not tell
you is what your library actually does, because a bounded integer strategy
rarely samples uniformly — it oversamples zero, one and the bounds, on purpose,
because that is where bugs live.

Which means the wide row of the table is a **measurement, not a foregone
conclusion**, and this lab has evidence on both sides of it: the two
cross-check probes below were run on this machine and *neither* was fooled by a
widened range. fast-check only stops finding the tie once you also pass
`unbiased: true`; rapid keeps finding it regardless. Hypothesis's own behaviour
here was **not** observed — it is not installed on this machine, so the wide
test is marked non-strict `xfail` and reports whichever way it lands. Predict
it, run it with `-rX`, and record what you get.

The generalisation survives either outcome, and is sharper for it: a property
is only as good as the probability that its strategy produces the shape the bug
needs — and that probability is set by the *distribution*, which you did not
write down, rather than by the *range*, which you did.

The bug itself: rows sharing the boundary timestamp are **skipped** by `<`,
and **duplicated — possibly forever** — if you "fix" it to `<=`. The real fix
is a composite cursor comparing `(created_at, id)` as a row value, which in
Postgres is `WHERE (created_at, id) < (:ts, :id)` and uses a composite index
cleanly. Property first, watch it fail, fix, then pin the shrunk example.

### Cross-check: give three shrinkers the same bug

Port the *same* property to Go (`pgregory.net/rapid`) and Node
(`fast-check`), against a line-for-line port of `page()`. Two extra languages,
not five: the variable is shrinker output, and three samples show the spread
without building the same forty lines six times. Record what each one prints.
It is the cheapest way to learn what "integrated shrinking" buys, and it
inoculates you against assuming every language's tool behaves like Hypothesis.

### Stateful: idempotency keys

`RuleBasedStateMachine` over the real API through testcontainers, with
`@rule`s for `create_order(key)`, `retry_create(key)` and `cancel(order)`, and
an `@invariant` that charge rows never outnumber the distinct idempotency keys
used. The planted bug: the idempotency record is written *after* the charge,
in a separate transaction, so a retry inside that window double-charges.
Hypothesis finds the interleaving; an example test almost certainly will not.

## How to run

```
cd 08-craft/lab/api

# warm-up: same property, two strategies. The unrestricted one hands you a refund.
DATABASE_URL=sqlite+aiosqlite:///:memory: pytest tests/properties/test_money.py -q

# the flagship. narrow: fails. Read the counterexample before doing anything else.
DATABASE_URL=sqlite+aiosqlite:///:memory: pytest tests/properties/test_pagination.py -q
# wide: the outcome IS the measurement -- -rX prints it either way
PAGINATION_STRATEGY=wide \
  DATABASE_URL=sqlite+aiosqlite:///:memory: pytest tests/properties/test_pagination.py -rX
DATABASE_URL=sqlite+aiosqlite:///:memory: \
  pytest tests/properties/test_pagination.py -q --hypothesis-show-statistics

# stateful: the idempotency key written one transaction too late
docker compose up -d postgres
TEST_DATABASE_URL=postgresql+asyncpg://app:app@localhost:55442/craft_lab \
  pytest tests/properties/test_idempotency_stateful.py -q --hypothesis-show-statistics
```

`tests/properties/test_pagination.py` contains all three strategies as separate
tests -- narrow, wide, and the **unsorted** one that fails for a different reason
and is kept as a runnable demonstration rather than a warning in prose. It also
holds the property that catches the `<=` "fix" (which duplicates forever;
`walk_pages` raises rather than hanging) and the property the composite-cursor
fix satisfies. The code under test is `lab/api/app/core/pagination.py` and
`lab/api/app/core/money.py`.

The cross-check runs natively, and it is the part worth doing even if the rest is
blocked:

```
cd 08-craft/lab/probes/go-rapid && go test -run TestPagination -v
cd 08-craft/lab/probes/go-rapid && go test -run XXX -fuzz FuzzPage -fuzztime 30s
cd 08-craft/lab/probes/node-fc  && npm install && npx vitest run
```

**The first two exit non-zero on purpose, and that is the demonstration.**
`TestPagination` and `TestPaginationWideRange` are the property finding the bug;
`FuzzPage` fails on its seed corpus in about a second, because the seed IS the
known counterexample and coverage-guided fuzzing never has to search for it.
Read the printed counterexample -- an exit code of 1 here is the result, not a
broken build. (The fast-check probe wraps the same failures in assertions so its
suite stays green; the two conventions are worth noticing, because a CI job that
gates on exit status would report these two probes very differently while they
found exactly the same defect.)

Both cross-checks ran on this machine, and both produced a result the topic did
not predict: **rapid and fast-check each bias integer generation toward boundary
values, so widening the timestamp range does NOT hide the bug from them the way
it does from Hypothesis.** fast-check only stops finding it once you also pass
`unbiased: true`. Both probes now contain a test for each half, so what you
record is the measurement rather than the expectation. Same declared range, three
different distributions -- which is a sharper version of this topic's thesis than
the one it set out to make.

**Blocked on this machine, with the exact unblock command:**

| What | Why | Unblock |
|---|---|---|
| every `pytest tests/properties/...` line | Hypothesis is not installed | `python3 -m pip install 'hypothesis==6.165.*'` |
| the stateful test | needs Postgres 18; the Docker daemon is not running | start Docker Desktop |

The bug itself was verified here without Hypothesis, by exhaustive search over
the 1,020 cases formed by 1-4 rows with unique ids, each row's `created_at` drawn
from `{0,1,2,3}`, at limits 1-3: `page()` drops rows in **510** of them, the
minimum is **two rows sharing a timestamp at `limit=1`** (which is what the
`@example(...)` in the test pins), `page_composite()` fails in **none**, and
`page_inclusive()` fails to terminate in **672**. Re-derive it rather than
trusting it -- it is thirty lines of `itertools.product` over
`app.core.pagination`.
## Predict, then record

Before running, write down: (a) how many examples do you think Hypothesis
needs to find the pagination bug with the narrow strategy? (b) how many rows
will the shrunk counterexample contain? (c) with `st.datetimes()`, what is
your rough estimate of the probability of a tie appearing at all in 2000
examples — and can you justify it from the range and the number of draws?

| Test | strategy | examples to first failure | shrunk counterexample | found the bug? |
|---|---|---|---|---|
| money split | `st.integers()` | | | |
| money split | `st.integers(min_value=0)` | | | |
| pagination | narrow (`0..3`), sorted | | | |
| pagination | wide (`st.integers(0, 2**53)`), sorted | | | |
| pagination | narrow, **without** the `.map` sort | | | |
| idempotency stateful | — | | | |

| Shrinker | tool | shrunk counterexample |
|---|---|---|
| Python | Hypothesis | |
| Go | rapid | |
| Node | fast-check | |

**What would mean the experiment is broken rather than your prediction wrong:**

- **The counterexample has no repeated `created_at`.** You found a
  **precondition violation, not the tie bug** — the strategy handed `page()`
  an unsorted list, which its docstring forbids. Check that the `.map(lambda
  rs: sorted(rs, key=lambda r: r.created_at, reverse=True))` is on the
  strategy and not accidentally applied inside the test body after the pages
  have already been walked. This is the single easiest way to get a confident
  false confirmation out of this experiment, which is why it is first on the
  list.
- **It passes with the narrow strategy too.** Check `unique_by`. If you made
  `created_at` unique rather than `id`, you deleted the bug from the input
  space rather than from the code. Also check that `max_examples` took effect
  — a `@settings` on the wrong decorator line is silently ignored.
- **It fails immediately with a one-row counterexample.** The tie bug needs
  two rows, so a one-row failure is an off-by-one in the *test harness's*
  page-walking loop, not in `page()`. The usual culprit is treating an empty
  final page as a failure.
- **It fails with `Flaky` or `DeadlineExceeded` rather than your assertion.**
  That is the 200 ms default deadline, not a bug in the code. Set
  `deadline=None` for anything touching a container.
- **The second run passes.** Either Hypothesis replayed the saved example from
  `.hypothesis/examples` against code you have since changed, or you are in a
  fresh container / under the auto-loaded `ci` profile, both of which mean no
  database and therefore no replay. Not flakiness — an argument for
  `@example(...)`.
- **The stateful test finds nothing.** Check that the rules can actually
  interleave. If `create_order` awaits its own commit before returning, the
  state machine is executing serially and can never produce the racing window;
  the two writes must be in separate transactions to reproduce it.

## Answer before moving on

1. You found the pagination bug with a narrow timestamp strategy. Describe, in
   general, how you would choose a strategy's distribution for a function you
   have never seen — what are you actually trying to make likely?
2. The `<=` "fix" causes duplicates and can loop forever. Write the property
   that would have caught *that* fix, and say why it is different from the one
   you already wrote.
3. The `.map()` sort encodes a precondition. Argue instead that the
   precondition should be *removed* — that `page()` should sort its own input,
   or reject unsorted input — and price each choice at the call site.
4. Your differential property ("the optimized query returns the same rows as
   the original") is the one that applies to a latency project. What would you
   generate, and what makes generating realistic database state hard in a way
   that generating integers is not?

## Next up

[Topic 6 — Contract tests at service boundaries](../06-contract-tests-at-service-boundaries/README.md):
the same generative machinery pointed at an HTTP surface instead of a
function, and the reason that proves less than it looks like it does.
