# Layer 8 · Topic 4 — What belongs at each test level, and the mock that lies

### The takeaway (read this first)

**The one idea:** a mock encodes *your belief* about how the dependency
behaves. A test built on mocks verifies that your code matches your beliefs —
which is exactly the thing that was never in doubt.

**Why it matters in practice:** the highest-confidence-per-second tests in a
Python service are integration tests against a real Postgres in a container,
and they have been cheap since testcontainers matured. Most codebases are
still paying 2015's price in mock maintenance for 2015's reason — that
databases were slow to spin up — which stopped being true years ago.

**You'll know it landed when:** you can name, for any mock in your suite, the
specific production behaviour it is asserting away, and you have deleted at
least one that was asserting away something real.

## The concept

Forget the pyramid's proportions for a moment and ask the only question that
sorts tests usefully: **what would have to be true for this test to pass while
the system is broken?** Every taxonomy of test levels is downstream of that
question, and the answer is different per level:

- **Unit, no I/O.** Correct for pure logic: pagination cursors, money
  splitting, state machines, parsers, retry-policy decisions. Fast,
  deterministic, and the right home for topic 5's properties. This is where
  most of your tests *should* be, and in most codebases it is where the fewest
  are — because the logic is tangled into handlers and cannot be called
  without a request object.
- **Integration against real infrastructure.** Correct for anything that
  touches SQL, transactions, serialization, or the ORM. A session-scoped
  testcontainers Postgres plus a per-test transaction rollback gives you real
  semantics at roughly unit-test speed once the container is warm.
- **Contract, at the boundary.** Topic 6.
- **End-to-end through the real HTTP surface.** A handful, no more. They are
  slow and flaky, and they catch a category nothing else does: wiring.

The mock's failure mode is precise, and worth memorising as a sentence: **a
mock cannot fail in a way you did not anticipate.** Concretely, in Python:
`AsyncMock()` returns another `AsyncMock` for *any* attribute, so
`session.commit.assert_awaited_once()` passes whether or not the code under
test committed anything real; and `mock_session.execute.return_value` hands
back whatever you configured, in the order you wrote it — which silently
supplies the ordering guarantee that your missing `ORDER BY` did not.

That second one is the deep point. A mock does not merely fail to catch the
bug. It **actively supplies the property the bug removed**, because you wrote
the fixture with the correct behaviour in mind. This is why a mocked suite
gets *more* confident as the code gets *more* wrong: every new bug gets a new
fixture that encodes the intent rather than the reality.

The honest counter-case, because "never mock" is not the rule: mocks are
correct for third-party services you cannot run, for injecting failures you
cannot easily produce, and for asserting that a side effect *happened* (an
email was enqueued, a webhook fired). The durable version of the rule is
**don't mock what you don't own** — write a thin adapter that you *do* own,
and fake that. James Shore's "Testing Without Mocks" pushes this further into
*Nullables*: production code with an off switch, so the thing under test is
the real object in a degenerate configuration rather than a stand-in that
shares only a name.

## How each language actually gets there

Six languages, and the axis is sharp: **how much does the language let you
replace at runtime without designing a seam?** The more permissive the
mechanism, the less the design has to be honest — and the more the tests can
lie.

**Python (your stack), maximally permissive.** `unittest.mock.patch` can
replace any attribute of any module at runtime, and `AsyncMock` invents
attributes on demand, so no seam is ever required and the design never has to
admit it has a dependency. Two mitigations that cost nothing:
`autospec=True`/`create_autospec` so signatures are enforced, and
`assert_called_once_with(...)` rather than `assert_called_once()`. But the
real fix is a Postgres container.

**Node, worse in one specific way.** `jest.mock`/`vi.mock` do module-level
substitution, replacing a dependency for *every* consumer in the module graph
including transitive ones you never considered. The failure is that the test
passes while a module you did not intend to affect ran against a stub. ESM
makes this harder rather than easier, which is a feature. Prefer dependency
injection; it is the same fix as everywhere else.

**Go.** `sqlmock` exists and has all the problems above, but the idiomatic
answer is already integration-first: `dockertest` or testcontainers-go, plus a
consumer-defined interface with a small hand-written fake. Hand-written fakes
are systematically better than generated mocks for one boring reason —
writing one by hand makes you notice how much behaviour you are inventing.
You cannot fake a database's ordering semantics without first noticing you do
not know them.

**Rust, the strictest.** There is no monkey-patching at all. To substitute a
dependency you must have designed a seam — a trait, plus generics or
`dyn` dispatch — before you needed it. `mockall` generates mocks *from a
trait*, so mockability is a design decision made in the source, not a thing
tests can retrofit. The consequence is philosophically important and worth
sitting with: in Rust, "this is hard to test" is a compile-time statement
about coupling, not a complaint about tooling.

**C++.** Same requirement as Rust, less enforcement: gMock needs virtual
functions or a template parameter, so again a seam must exist. Its
contribution is `NiceMock` / `StrictMock` — an explicit knob for how much
unspecified behaviour the double tolerates. That knob is the thing Python's
`AsyncMock` is missing entirely, and reading the gMock docs on it is the
fastest way to see what "permissive" is costing you.

**Java, the most powerful and therefore the most dangerous.** Mockito can mock
statics and finals, and `RETURNS_DEEP_STUBS` will happily fabricate an entire
object graph so that `a.getB().getC().getD()` returns a live mock — the exact
Python `AsyncMock` failure with better tooling around it. Java is also where
testcontainers originated, so the same ecosystem contains both the sharpest
version of the problem and the canonical fix. If you want one argument for
integration-first testing to bring to a skeptical team, it is that the
language community with the best mocking library spent a decade building
containers instead.

## The experiment

The lab ships one repository function with **two** planted bugs, and two test
suites that both pass:

```python
async def recent_orders(session, customer_id: int, limit: int) -> list[Order]:
    stmt = select(Order).where(Order.customer_id == customer_id).limit(limit)
    result = await session.execute(stmt)          # bug 1: no ORDER BY
    orders = [o for o in result.scalars() if o.status != "cancelled"]
    return orders                                 # bug 2: filter applied after LIMIT
```

1. `tests/unit/test_recent_orders_mocked.py` mocks the session and passes. Read
   the fixture afterwards and find the line where *you* supplied the ordering.
2. `tests/integration/test_recent_orders_real.py` runs the same assertions
   against a testcontainers Postgres 18 with the seeded orders.
3. The same integration test, repeated 20 times. Bug 1 is *intermittent*
   against a real database — Postgres may return heap order, and heap order
   changes after an `UPDATE`. A test that catches a bug 3 times in 20 is a
   different and more realistic object than one that catches it always, and
   thinking about what you would do with it is half of this topic.
4. The commit test, which is the one worth showing people: a service method
   missing `await session.commit()`, with a mocked test asserting
   `session.commit.assert_awaited_once()` — and passing, because the mock's
   `commit` is called by the surrounding `async with` block, not by the code
   under test. The assertion is true. The system is broken.

## How to run

```
cd 08-craft/lab/api

# passes, proves nothing. Read the fixture and find where YOU supplied the order.
DATABASE_URL=sqlite+aiosqlite:///:memory: pytest tests/unit/test_recent_orders_mocked.py -q

# passes; the commit never happened. The assertion is true, the system is broken.
DATABASE_URL=sqlite+aiosqlite:///:memory: pytest tests/unit/test_commit_mocked.py -q

# fails, twice, against a real Postgres 18
pytest tests/integration/test_recent_orders_real.py -q

# the intermittency run: bug 1 is not caught every time, and that is the point
pytest tests/integration -q --count=20
```

The two planted bugs are in `recent_orders()` in
`lab/api/app/repositories/orders.py`; `recent_orders_fixed()` beside it is the
version that satisfies both assertions. The mocked suite is
`tests/unit/test_recent_orders_mocked.py`, the commit demonstration is
`tests/unit/test_commit_mocked.py`, and the real-database suite is
`tests/integration/test_recent_orders_real.py`.

Everything under `tests/unit` runs natively on macOS with no container -- set
`DATABASE_URL=sqlite+aiosqlite:///:memory:` as shown, or use `make unit`.
Anything under `tests/integration` is marked `container` and is **skipped with
an unblock message** rather than erroring when either prerequisite is missing --
the Docker daemon, or the `testcontainers` package the fixture imports. Both
checks are in `tests/conftest.py`, and both run at collection time, because a
prerequisite discovered inside a fixture surfaces as a bare `ModuleNotFoundError`
in four test setups with the fix named nowhere near it.

**Blocked on this machine, with the exact unblock command:**

| What | Why | Unblock |
|---|---|---|
| `tests/integration` | the `testcontainers` package is not installed, so the fixture cannot start Postgres 18. (The Docker daemon itself is up; `conftest.py` checks both and skips naming whichever is missing) | `python3 -m pip install 'testcontainers[postgres]'` |
| `--count=20` | `pytest-repeat` is not installed | `python3 -m pip install pytest-repeat` |

The mocked half runs here as-is, which is itself the topic's joke: the suite that
proves nothing is the one with no prerequisites.

### The other five languages, all of it native

The mechanism section above names a different double per runtime. Each of the
five programs below runs the **same assertions twice** -- once against that
language's idiomatic double, once against a hand-written fake that models a
table (rows in storage order; `WHERE`, then `ORDER BY`, then `LIMIT`) -- so the
contrast is in one output rather than across two files. Python's arm is the lab
suite above and is not duplicated here.

Run from `08-craft/04-test-levels-and-the-mock-that-lies/`:

```
cd nodejs && node --test
cd golang && go test -v ./...
cd rust/mock_that_lies && cargo run
g++ -std=c++20 -O2 -o /tmp/t4_cpp cpp/nice_naggy_strict.cpp && /tmp/t4_cpp
cd java && javac DeepStubsLie.java -d /tmp/t4java && java -cp /tmp/t4java DeepStubsLie
```

All five exit 0, because in every one of them **the green suite is the finding**.
Where a suite has to go red to make its point, it does so inside the program and
reports it as a printed `[FAIL]` line rather than as an exit code.

| Program | The mechanism it makes visible |
|---|---|
| `nodejs/quote.test.js` | module-registry substitution reaches **every** consumer. Two bugs, in two modules, one stub: `quote.js` reads a field that does not exist and `ledger.js` lowercases a lookup key, and neither module can execute against the real dependency. `ledger.js` is never named by any test -- it got the stub because the stub is on the shared dependency. The hoisting test shows why `jest.mock` must run before the graph is built, which is the same property that makes the substitution global |
| `golang/` | `sqlmock`'s mechanism, at the `database/sql` driver level: `TestScriptedFakeNeverReadsTheQuery` hands the driver the broken SQL and the correct SQL and gets byte-identical rows back. Beside it, the hand-written fake that catches both bugs -- and `executeish` **panics** on an `ORDER BY` it does not model, which is a fake telling you it does not know a semantics instead of inventing one |
| `rust/mock_that_lies/` | the seam is mandatory: `trait Rows` had to be written before either suite could exist, and `PgRows` has no seam that any test in any crate can reach. Then the point that survives it -- the scripted double is 3/3 on code the fake scores 1/3. `cargo test` passes too, and passing is the finding |
| `cpp/nice_naggy_strict.cpp` | the strictness knob, which no other language here exposes. A third bug (an extra round trip whose result is discarded) is silent under `NiceMock`, a stderr warning under the default `NaggyMock`, and a failure under `StrictMock` -- while the output assertions are identical in all three. Strictness and fidelity are different axes: Strict caught the call nobody expected, only the fake caught the semantics nobody modelled |
| `java/DeepStubsLie.java` | `RETURNS_DEEP_STUBS` fabricates the object graph, so `assertNotNull(session.getRepository())` can only ever pass. Four green assertions against a session that is not bound to a repository at all. Plus the property that is easy to miss: the chain is **cached**, so `verify(s.getRepository().getOrders())` relies on an identity that is an invariant of the double, not of the interface |

**None of the five mocking libraries the section names is installed on this
machine, and none is required.** Each file hand-implements the mechanism it is
about -- between twelve and sixty lines in every case -- which is the reason to
read them: `jest.mock` is a write into `require.cache`, `RETURNS_DEEP_STUBS` is
`java.lang.reflect.Proxy` returning another proxy, `NiceMock`/`StrictMock` is one
`enum` consulted on an unexpected call. Every file says so in its header and
none of them claims to be running the real library.

| Library the section names | Unblock, if you want to re-run the same demonstration through its real API |
|---|---|
| jest / vitest | `cd nodejs && npm i -D vitest` |
| `DATA-DOG/go-sqlmock` | `cd golang && go get github.com/DATA-DOG/go-sqlmock` |
| `mockall` | add `mockall = "0.13"` to `rust/mock_that_lies/Cargo.toml` and put `#[automock]` on `trait Rows` |
| googletest / gMock | `brew install googletest`, then link `-lgmock -lgtest -lgtest_main` |
| Mockito | a JUnit + `mockito-core` classpath; deep stubs are `mock(Session.class, RETURNS_DEEP_STUBS)` |

## Predict, then record

Predict: how many of the two bugs does the real-Postgres test catch on the
first run? How many across 20 runs? And how much slower is the integration
suite than the mocked one, in wall-clock seconds, once the container is warm?

| Suite | bugs caught | wall clock (cold) | wall clock (warm) |
|---|---|---|---|
| mocked | | | |
| testcontainers, 1 run | | | |
| testcontainers, 20 runs | | | |

| Bug | caught by mocked? | caught by integration, run 1 | runs out of 20 that caught it |
|---|---|---|---|
| 1 — missing `ORDER BY` | | | |
| 2 — filter after `LIMIT` | | | |
| 3 — missing `commit()` | | | |

**What would mean the experiment is broken rather than your prediction wrong:**

- **The integration test catches the ordering bug reliably on run 1.** Check
  your seed order. If you inserted rows in exactly the order you assert,
  Postgres will hand them back that way and you have accidentally rebuilt the
  mock in SQL. Shuffle the inserts and `UPDATE` a few hundred rows before
  asserting.
- **The integration test catches *nothing*.** Check that it is talking to the
  container and not to a leftover local database with different data, and that
  the fixture is not seeding fewer rows than `limit` — bug 2 cannot appear
  unless enough cancelled rows exist to fall inside the limit window.
- **The integration suite is more than roughly an order of magnitude slower
  warm.** You are starting a container per test rather than per session. Move
  the fixture to `scope="session"` and wrap each test in a transaction you
  roll back.
- **The commit test fails rather than passes.** Then your service method is
  not inside an `async with session.begin()` — which means you have removed
  the mechanism that made the assertion vacuous, and the demonstration needs
  the surrounding block to be meaningful.

## Answer before moving on

1. Both planted bugs are invisible to a mock. Construct a third bug that a
   mocked test *would* catch and an integration test would not.
2. Testcontainers gives you a real Postgres. Name three production behaviours
   it still does not reproduce, and say which of the three has bitten you.
3. Your team has 3,000 mocked tests and you cannot rewrite them. What is the
   single highest-value change you can make in one week?
4. Bug 1 was caught by some runs and not others. Write the policy you would
   actually adopt for an intermittent test that catches a real bug — and
   defend it against "flaky tests must be deleted".

## Next up

[Topic 5 — Property-based testing with Hypothesis](../05-property-based-testing-with-hypothesis/README.md):
the layer's flagship. You have now seen tests that pass while the system is
broken. Next, a machine disagrees with you about code you already reviewed
and shipped.
