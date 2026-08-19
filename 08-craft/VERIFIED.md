# Layer 8 · VERIFIED

**Date:** 2026-08-19
**Verified by:** an independent pass that did not write this code. Every row
below was produced by running the command in the topic README on this machine.

## What this file is, and what it is not

This records that the code in `08-craft/` **executes**: it compiles, it runs, it
does not hang, and each program's output matches what its README says the
program demonstrates. It records **nothing about what was learned.** The
`Predict, then record` tables in every topic README are still blank, and they
stay blank — they are your exercise, and a filled-in table you did not fill in
is worth less than an empty one.

Numbers appearing in this file are real captured output. They are real because
they were run here, on this machine, on this date. They are not predictions,
they are not targets, and they do not transfer: absolute numbers are a property
of an M1 laptop, not of the ideas.

## The machine

| | |
|---|---|
| OS | macOS 27.0 (build 26A5406e), Darwin, **arm64 / Apple M1** |
| Python | 3.13.5 · pytest 9.1.1 · fastapi 0.128.0 · SQLAlchemy 2.0.39 · aiosqlite 0.22.1 · coverage 7.14.1 |
| Node | v24.14.0; `npm install` was run in `probes/node-fc` only, as topics 5 and 8 instruct (`node_modules/` is gitignored). `consumer-node` needs no dependencies |
| Go | go1.24.5 darwin/arm64 |
| Rust | rustc 1.97.1 / cargo 1.97.1 |
| C++ | Apple clang 21.0.0 (clang-2100.1.1.101) — really clang; `-pthread` and `-std=c++23` both work, `<expected>` is present |
| Java | JDK 21.0.2 LTS (`javac` and `java`), virtual threads available |
| Docker | CLI 28.1.1 installed, **daemon DOWN** (`docker info` fails) |
| k6 | **not installed** |
| Postgres | client tools 17.5 (Homebrew) on PATH; a local server is listening on `:5432`. The lab pins **18** and reaches it through Docker, so the local 17.5 was not substituted |

Deviations from `lab/README.md`'s pin table, recorded rather than corrected:
Go is 1.24.5 (pinned 1.26.x), coverage.py is 7.14.1 (pinned 7.15.x). Neither
affected any run below.

Absent Python packages, each of which turns into a BLOCKED row: `hypothesis`,
`mutmut`, `schemathesis`, `testcontainers`, `pytest-repeat`.

## Every program, with status

### Topic 3 — the six-language half (`03-errors-as-interface/`)

The only topic with per-language directories. All six run natively.

| Program | Command | Status |
|---|---|---|
| `python/exception_groups.py` | `python3 python/exception_groups.py` | **RAN** — prints the broken `except Exception` around a `TaskGroup` collapsing 3 categories into one 500, then `except*` separating 2 retryable + 1 bug, then the `__cause__`/`__context__`/`__suppress_context__` table |
| `nodejs/unhandled_rejection.js` | `node nodejs/unhandled_rejection.js` | **RAN** — 201 with `audit rows written: 0`, then the awaited/translated version with a three-link cause chain |
| `golang/errors_as_values.go` | `cd golang && go run errors_as_values.go` | **RAN** — four `%w` wraps that add nothing, then one wrap that does; `errors.As` yields the retry budget as a value; ends on a deliberate nil-map panic |
| `rust/taxonomy/` | `cd rust/taxonomy && cargo run` | **RAN** — `Box<dyn Error>` collapse vs the typed enum; ends on a deliberate `panic!` (index out of bounds), which the output labels as intended |
| `cpp/four_mechanisms.cpp` | `g++ -std=c++23 -O2 -o /tmp/t3_cpp cpp/four_mechanisms.cpp && /tmp/t3_cpp` | **RAN** — compiles with exactly one warning, `-Wexceptions` on the `noexcept` function, **which is the demonstration**; `__has_include(<expected>)` guard present, so a toolchain without `<expected>` degrades to three mechanisms instead of failing |
| `java/ErrorTaxonomy.java` | `javac ErrorTaxonomy.java -d /tmp/t3java && java -cp /tmp/t3java ErrorTaxonomy` | **RAN** — the swallowed `InterruptedException` ran 1365 ms / 50 units against the restored-flag version's 109 ms / 3 units, on a virtual thread |

All six exit 0. Every claim in topic 3's mechanism table was checked against the
source, not just the output: `#[non_exhaustive]`, `new Error(msg, {cause})`,
`time.Duration` out of the error, `Thread.ofVirtual()`, the `-Wexceptions`
warning and the `<expected>` fallback are all present as described.

### The shared lab (`lab/`)

| Program | Command | Status |
|---|---|---|
| API test suite | `cd lab/api && DATABASE_URL=sqlite+aiosqlite:///:memory: python3 -m pytest -q` | **FIXED-THEN-RAN** — **46 passed, 7 skipped** in ~2 s. Was 38/7 before this pass; see *Fixes* below |
| contract tests | `pytest tests/contract -q` | **RAN** — 4 passed (topic 6's README said 5; corrected) |
| OpenAPI snapshot gate | `cd lab/api && python3 snapshot_openapi.py --check` | **RAN** — `matches the live schema`, and it is **byte-stable**: three consecutive runs in three separate processes all exit 0. The author's report of a per-process model-name race is fixed and stays fixed |
| coverage, assertion-free suite | `coverage run --branch --source=app.core.money,app.core.pagination -m pytest tests/unit/test_no_assertions.py` | **RAN** — `money.py` 19 stmts / 8 branches, `pagination.py` 46 / 16, **100 % statement and 100 % branch on both**, from tests with no assertions. Topic 8's README claims exactly this and it is correct |
| `make coverage` | `cd lab/api && make coverage` | **RAN** — whole unit suite, 753 statements / 110 branches, 39 % total |
| `tools/name_audit.py` | `python3 lab/tools/name_audit.py --path lab/api/app` | **FIXED-THEN-RAN** — 68 functions (55 public, 16 methods); four reports print in under a second |
| `name_audit.py --quiz` / `--quiz-key` | `--quiz 20` then `--quiz-key` | **FIXED-THEN-RAN** — quiz and key now draw the same functions **at any N**; they did not before |
| `tools/temporal_coupling.py` | `--repo <a git repo> --months 12` | **RAN** — exercised against a purpose-built 22-commit repo; correctly ranked the planted `a/model.py`↔`b/api.py` pair at ratio 1.00 and gave the uncoupled file cohesion 1.00, with the `class` column left blank as designed. Standard library only |
| `tools/regression.sh` | `cd lab/api && make regression BUG=pagination-ties` | **BLOCKED** — exits 2 with `not a git repository`, and prints its own unblock command. The guard works; the gate itself is untested here |
| `lab/api/seed.py` | `docker compose exec api make seed` | **BLOCKED** — needs the stack. Read but not run: it is Postgres-specific by design (`WHERE id = ANY(:ids)`) and prints the tie count topic 5 depends on |
| `lab/compose.yml` | `docker compose up -d` | **BLOCKED** — daemon down. Read against `lab/README.md`'s contract: service names, images (`postgres:18`, `ghcr.io/shopify/toxiproxy`, `grafana/k6`), published ports 8010 / 55442 / 8475 / 55443, every env var, and the named `pgdata` volume all match the spec exactly |
| `lab/load/*.js` (3 k6 scripts) | `docker compose --profile load run --rm k6 ...` | **BLOCKED** — k6 absent and daemon down. All three parse under `node --check`, and all three use `constant-arrival-rate` only: **no `constant-vus`, no `ramping-vus` anywhere**, which is the lab's stated load rule |

### Probes (`lab/probes/`) — all native, no daemon

| Program | Command | Status |
|---|---|---|
| `probes/rust` (workspace) | `cargo test` | **RAN** — `same-behaviour` 3 tests pass: shallow and deep return the same page, reject a zero limit, and reject an unknown customer. This is the Rust half of topic 1's precondition |
| `probes/cpp` | `./measure.sh` | **RAN** — shallow **89,961** preprocessed TU lines / **0.917 s** best-of-three rebuild; deep **65,871** / **0.803 s**. Both binaries run and agree before either number is printed |
| `probes/go-naming` | `go doc -all . && go test ./...` | **FIXED-THEN-RAN** — 4 tests pass, `go vet` clean |
| `probes/go-rapid` | `go test -run TestPagination -v` | **RAN, EXITS 1 BY DESIGN** — `TestPagination` fails at 4 examples with `input=[{0 0} {0 1}]`, i.e. two rows sharing a timestamp at `limit=1`; `TestPaginationComposite` passes; `TestPaginationWideRange` **also fails**, at 22 examples, which is the probe's real finding |
| `probes/go-rapid` fuzz | `go test -run XXX -fuzz FuzzPage -fuzztime 30s` | **RAN, EXITS 1 BY DESIGN** — fails on the **seed corpus** in ~1 s (`walk yielded 2 rows from 4 inputs`), because the seed is the known counterexample. It never reaches 30 s of fuzzing, and topic 5 now says so |
| `probes/node-fc` | `npm install && npx vitest run` | **RAN** — 6 tests pass. Wide+biased still finds the tie (19 runs); wide+`unbiased: true` does not; shrunk counterexample `[{createdAt:2,id:0},{createdAt:2,id:-1}], 1` |
| `probes/node-fc` mutation | `npx stryker run` | **RAN** — `pagination.js`: **91.86 %** total / 94.05 % covered, 79 killed, 5 survived, 2 no-coverage, 0 timeouts, 0 errors, in 6 s |

### Consumers (`lab/consumer-*`) — topic 6, native

| Program | Command | Status |
|---|---|---|
| `consumer-go` | `go build ./... && go test ./...` | **RAN** — 4 tests pass. Break 1 (`total` int→string) is caught by the unmarshaller; break 2 (required field omitted) **passes**, and the test says so in its own log line — that is topic 6's finding, not a gap |
| `consumer-node` | `node --test` | **RAN** — 7 tests pass, including the two halves of break 1 (with and without runtime validation) |

Both test against a stub built from the committed snapshot, never the live API.
Confirmed by reading `snapshot.go` / `server.js`.

### Blocked, with the exact one-line unblock

| What | Why | Unblock |
|---|---|---|
| every `docker compose …` line in topics 1, 3, 4, 6, 7 | Docker daemon is not running | start Docker Desktop |
| `tests/integration/test_recent_orders_real.py` (4 tests) | testcontainers needs the daemon; also `testcontainers` is not installed | start Docker Desktop, then `python3 -m pip install 'testcontainers==4.15.*'` |
| `tests/properties/` (3 modules) | Hypothesis is not installed | `python3 -m pip install 'hypothesis==6.165.*'` |
| `pytest --count=20` (topic 4's intermittency run) | `pytest-repeat` is not installed | `python3 -m pip install pytest-repeat` |
| `mutmut run` / `make mutation` | mutmut is not installed | `python3 -m pip install 'mutmut==3.7.*'` |
| `schemathesis run` / `oasdiff` | run inside the `tools` image | start Docker Desktop, or `python3 -m pip install 'schemathesis==4.24.*'` |
| every `k6 run` | k6 is not installed | `brew install k6`, or run it through compose |
| `cargo public-api -p shallow` / `-p deep` | not installed | `cargo install cargo-public-api --locked` |
| `make regression BUG=pagination-ties` | `08-craft/` is not a git repository | `git init && git add -A && git commit -m 'lab baseline'` at the repo root |

Every skip in the Python suite prints its own unblock command. Verified with
`pytest -rs`.

## What was fixed in this pass

Ten defects, each found by running or reading the thing rather than the report.

1. **An experiment that tested nothing.**
   `tests/properties/test_pagination.py::test_unsorted_input_fails_for_a_DIFFERENT_reason`
   called `pytest.xfail(...)` as the **first statement of its body**, so it
   never touched `page()`. Its docstring and topic 5's README both described it
   as "a runnable demonstration". It was not runnable; it was a label. Now the
   property actually runs against `page()` on every example and the xfail is a
   `@pytest.mark.xfail(strict=True)` decorator, which reports the same way and
   executes the code. This is the Layer 1 failure mode exactly, and it was
   sitting inside the topic about tests that prove nothing.

2. **The wide-strategy outcome was asserted but never observed.** The same file
   contained `test_wide_strategy_passes_and_that_is_the_lesson` — "PASSES, and
   the bug is still there" — and topic 5's README stated **"the test passes
   forever while the bug is still there."** Hypothesis is not installed on this
   machine, so that outcome has never been seen here; and the lab's own two
   cross-check probes, which *were* run, both **kept finding the bug at their
   widest range** because bounded integer generators oversample boundaries.
   The test is renamed, marked non-strict xfail, and documents that its outcome
   is the measurement; the README now separates the arithmetic (sound, under
   *uniform* sampling) from the library behaviour (unobserved here), and points
   at the two probes that disagree. The `n²/2⁵⁴` derivation is kept and labelled.

3. **Topic 1's precondition was assumed, not tested.** `01-…/README.md` said
   `pytest tests/integration -q   # both shapes pass`, and its broken-experiment
   note said "the integration tests fail on one shape — stop and fix that
   first." No test in `tests/integration/` touched `app/shallow/` or
   `app/deep/` at all; the directory held only topic 4's `recent_orders` tests.
   Added `tests/integration/test_shapes_are_identical.py` — 7 tests, native,
   no daemon — comparing the **raw response bytes** of `/shallow/...` and
   `/deep/...` across five limit/offset pairs, the 404 both ways, and
   requirement 0's unfiltered `total`. The README now runs that first.
   (The Rust and C++ probes already had their equivalent; Python did not.)

4. **Two hangs in the file added by (3), both found by hitting them.** Writing
   the shape check meant meeting the two ways this app can wedge a test run,
   and both are now documented at the line that prevents them.
   *First:* `app.db.engine` is built at import and aiosqlite pins its
   connection to the first event loop that uses it, so pytest-asyncio's
   per-test loop deadlocks on the **second** test rather than failing — the
   first draft ran past five minutes. Fixed with
   `pytest.mark.asyncio(loop_scope="module")` and module-scoped fixtures.
   *Second:* aiosqlite runs each connection on a **non-daemon thread**, so an
   engine left open keeps the interpreter alive after the last test reports.
   pytest printed `46 passed … in 1.9s` and the process then never exited.
   Fixed with `await engine.dispose()` in the fixture teardown. Worth naming
   the method, because pytest's own duration line hid this completely: the
   summary said 1.9 s while `time` said the command never returned. **Trust the
   wall clock, not the runner's self-report.** Full suite now: 1.2 s reported,
   2.6 s real, measured three times.

5. **`name_audit.py --quiz N` and `--quiz-key` drew different functions.**
   `--quiz-key` ignored `N` and always sampled 20, and
   `random.sample(pool, 5)` is not a prefix of `random.sample(pool, 20)` even
   at the same seed. Topic 9's README promises "the SAME seed draws the same
   twenty functions" — true at exactly N=20 and silently false everywhere else,
   which grades your answers against a different quiz. Now one permutation is
   drawn and a prefix taken, so quiz and key agree at any N.

6. **`name_audit.py` counted closures as public surface.** `ast.walk` picked up
   functions nested inside other functions, so `q(p) -> float` — a two-line
   local inside `pool_wait_stats` — appeared in the verb census and in the
   blind-name quiz. A short local name in a small scope is *correct*, which is
   the tool's own thesis. Collection is now module-level and class-level only.

7. **A source file recommended a command that exits 2.**
   `probes/go-naming/orders.go`'s package doc told the reader to run
   `go doc ./...` — twice. Topic 9's README explicitly says that spelling is
   invalid. Corrected to `go doc -all .`, with the reason inline.

8. **A dangling reference.** `app/routers/orders_shared.py` cited
   `python tools/config_params.py` for counting `_create`'s configuration
   parameters. That file does not exist and no README mentions it. Replaced
   with the measurement that does exist: read the signature.

9. **Three small factual corrections.** `app/deep/__init__.py` said the package
   "exports two names"; `__all__` has three (`OrderPage`, `customer_order_page`,
   `router`) — corrected, with the command to count them. Topic 6's README said
   "5 contract tests"; there are 4. Topic 8's table named `pagination.ts`; the
   file is `pagination.js`.

10. **An unbounded external probe inside test collection.**
   `tests/conftest.py` decided whether to skip the container tests with
   `subprocess.run(["docker", "info"], capture_output=True)` — **no timeout**.
   The suite hung once during this pass with no output at all, and that call is
   the only thing in it that talks to anything outside the process; a Docker
   Desktop that is mid-start or wedged answers slowly or never, and it blocks
   *collection*, before a single test runs. Now bounded at 10 s, with a timeout
   treated as "daemon down". Six consecutive clean runs afterwards
   (1.4–2.0 s each), so the hang is not reproducible — which is exactly why it
   is worth removing the only way it could happen.

Also documented rather than changed: `probes/go-rapid` exits **1** on the
commands topic 5 gives, because its property tests are the bug finding itself,
and `FuzzPage` fails on its seed corpus in about a second rather than fuzzing
for 30. Topic 5 now says so at the command, next to the note that the
fast-check probe wraps the same failures in assertions and therefore exits 0 —
two conventions, one defect, opposite exit codes.

## Claims checked independently, and confirmed

Not taken from the report. Re-derived here.

- **The pagination search space.** Over the 1,020 cases formed by 1–4 rows with
  unique ids, each `created_at` drawn from `{0,1,2,3}`, at limits 1–3:
  `page()` drops rows in **510**, `page_composite()` in **0**,
  `page_inclusive()` fails to terminate in **672**. The minimum counterexample
  is **two rows sharing a timestamp at `limit=1`** — which is what the
  `@example(...)` and `tests/regression/test_pagination_ties.py` pin. Matches
  the report exactly; the README now states the search space precisely enough
  to re-derive.
- **The money split.** Over `total ∈ [-60, 60] × n ∈ [1, 12]`: the sum clause
  fails **462** times, the spread clause **0** times, every failure has a
  negative total, and `split_evenly(-1, 3) = [0, -1, -1]` — sum −2 for a total
  of −1. `split_evenly_fixed` satisfies both clauses on every input tested.
  The report's claim that only one of the two clauses fires is correct.
- **Coverage.** 100 % statement *and* 100 % branch from assertion-free tests, as
  topic 8 claims — checked, not assumed.
- **Open-model load only.** Grepped: `constant-arrival-rate` in all three k6
  scripts, `constant-vus` and `ramping-vus` in none.
- **`Predict, then record` tables.** All nine are blank. Checked cell by cell.
- **No invented numbers in the topic READMEs.** The only figures presented as
  observed are topic 8's 100 %/100 % coverage (confirmed above) and topic 5's
  exhaustive-search counts (re-derived above). Topic 7's `P/S = 625 rps` is
  arithmetic worked on the page from stated inputs, not a measurement.

## Coverage gaps, honestly

Two topics implement fewer languages than their own *How each language actually
gets there* section covers, without saying so anywhere. Topics 1, 5, 6 and 9
also narrow — but each states the narrowing and its reason in its own experiment
section, so those are complete as written.

- **Topic 4** — six-language mechanism section (`jest.mock` module hoisting,
  `sqlmock` vs a hand-written fake, `mockall` needing a designed seam, gMock
  `Nice`/`Strict`, Mockito `RETURNS_DEEP_STUBS`); code exists in **Python only**.
- **Topic 8** — six-language section built on *where in the pipeline a mutant is
  introduced* (source vs bytecode vs LLVM IR); code exists for **Python**
  (mutmut, blocked) and **Node** (Stryker, ran). No PIT, no `cargo-mutants`, no
  `mull` — and those three are precisely what the section's axis is about.
- **Topic 7** — six-language section; code exists for **Python, Go and Node**
  (ladder F names exactly those three clients). Rust, C++ and Java are prose.
- **Topic 2** — six-language section; the experiment is a language-agnostic git
  tool plus two Python arms. Defensible, but the layer index's claim that
  "topics 1 through 5 and 8 use all six" is not true of it.

Nothing else is missing: every topic folder has code reachable from its README.

---

# Fill-in pass — 2026-08-19

**Verified by:** a second independent pass that did not write the code below and
did not take the author's report as evidence. Every row was produced by running
the command the topic README gives, on this machine, on this date. Same rule as
above: no number here is a prediction or a target, and the `Predict, then
record` tables are still blank — re-checked cell by cell across all nine topics.

Scope: the three compiled arms added to **topic 8**
(`08-coverage-mutation-and-the-regression-rule/{java,rust,cpp}`), the topic 8
and topic 2 README edits, and the layer index.

## The three new arms

| Program | Command (exactly as the README spells it) | Status |
|---|---|---|
| `08-…/java/run.sh` | `cd 08-craft/08-coverage-mutation-and-the-regression-rule && java/run.sh` | **RAN** — exit 0 in 0.6 s. Step 1: `javac` (JDK 21) builds `Pagination.java` + `WeakChecks.java`, the weak suite passes against `Pagination.page`, the hand mutant **survives it**, and the never-made assertion prints `3 rows [30/3 20/2 10/1]` in, `5 rows [30/3 20/2 20/2 10/1 10/1]` out of the mutant's walk. Step 2: **BLOCKED**, Maven absent |
| `08-…/rust/run.sh` | `… && rust/run.sh` | **RAN** — exit 0 in 2.4 s from a cold `target/`. Step 1: `cargo test` 3 passed, 0 failed; same mutant, same 3-in/5-out contrast. Step 2: **BLOCKED**, cargo-mutants absent |
| `08-…/cpp/run.sh` | `… && cpp/run.sh` | **RAN** — exit 0 in 1.6 s. Step 1: Apple clang `-std=c++23 -O0 -g -Wall -Wextra`, **zero warnings**, binary exits 0 (which the script labels, correctly, as "mull reads exactly this: 0 = mutant survived"); same 3-in/5-out contrast. Step 2: **BLOCKED**, mull absent |

None of the three hangs; all three are under three seconds cold. All three
print the same hand-applied mutant and none of them prints a mutation score,
which is what their headers promise.

Checked beyond the run:

- **Apple clang with `-Wpedantic -Wshadow` on top of `-Wall -Wextra`:** still
  zero warnings on both `pagination.cpp` and `weak_test.cpp`.
- **`cargo clippy --all-targets`:** clean, exit 0.
- **`java/pom.xml` is well-formed XML** (parsed with `xml.dom.minidom`). Its
  *contents* remain unverified — Maven has never resolved a single pin here,
  and the file's own header says so.
- **`PaginationWeakTest.java` was compiled and executed**, not merely read. It
  is the class PIT would run and nothing here can run JUnit, so it was compiled
  against a three-file JUnit-shaped stub (`@Test`, `@DisplayName`,
  `Assertions.assertNull`) and its three `@Test` methods invoked reflectively
  against the **real** `Pagination`: `limitRespected`, `walkTerminates`,
  `zeroLimitRejected` — 3 tests, all green. So the JUnit face of the weak suite
  is real code that passes, not a file that merely parses.

## The claim the whole arm rests on, tested rather than eyeballed

Topic 8's step 4 is a denominator cross-check, and it only means anything if
the five ports are the **same algorithm**. The README asserts "ported line for
line". That was tested differentially instead of by reading:

1,800 cases — every row multiset with `created_at ∈ {0,1,2}` for 1–4 rows with
unique ids, sorted descending, at limits 1–3, with cursors `null,0,1,2,3` —
were driven through `page()` and `walk_pages()` in all five implementations,
each printing the page, the next cursor, and the full walk in one canonical
format. Python (`lab/api/app/core/pagination.py`) is the reference.

| Port | Result |
|---|---|
| Node — `lab/probes/node-fc/pagination.js` | **1800/1800 identical** to Python |
| Java — `08-…/java/…/craft/core/Pagination.java` | **1800/1800 identical** |
| Rust — `08-…/rust/src/lib.rs` | **1800/1800 identical** |
| C++ — `08-…/cpp/pagination.cpp` | **1800/1800 identical** |

Byte-identical output, including the `ERR` cases where `limit < 1` and the
ties that make the walk drop rows. The cross-check has a real basis; five
tools would be chewing on one algorithm.

One typed difference the Rust file already documents and this pass confirms:
`limit` is `usize` there, so the negative-limit family of mutants does not
exist in that arm. That is a smaller denominator caused by a narrower type,
not by a better suite — it belongs in the qualifier column of the table, and
the file says so.

## Coverage of the weak suites, measured

The README claimed "full line coverage" for the weak suites. Measured with
`clang -fprofile-instr-generate -fcoverage-mapping` + `llvm-cov` on the C++
arm — the one arm where a coverage tool is available here with no install:

`pagination.cpp` under `weak_test`: **90.70 % lines, 92.86 % branches, 95.45 %
regions, 4/4 functions executed.** The four uncovered lines are the single
`throw std::runtime_error(...)` in `walk_pages`'s non-termination guard.
`craft::page` itself has **no zero-count lines** — the claim is true of the
mutation target and false of the file. The README now says the accurate thing
and uses the gap: "unreachable by construction" and "untested" are the two
states a coverage percentage cannot tell apart, which is the topic's own thesis
appearing inside its own fixture. The Rust and Java arms have the same shape by
construction (their `walk_pages`/`walkPages` guards are equally unreachable
from a correct `page`), which is why the corrected sentence is written about
all three.

## Re-runs of what the new arms plug into

| Program | Command | Status |
|---|---|---|
| topic 8 step 1, coverage | `cd lab/api && DATABASE_URL=sqlite+aiosqlite:///:memory: python3 -m coverage run --branch --source=app.core.money,app.core.pagination -m pytest tests/unit/test_no_assertions.py -q && python3 -m coverage report -m` | **RAN** — 2 passed; `money.py` 19 stmts / 8 branches, `pagination.py` 46 / 16, **100 % statement and 100 % branch on both**. Reproduces the first pass exactly |
| API suite | `cd lab/api && DATABASE_URL=sqlite+aiosqlite:///:memory: python3 -m pytest -q` | **RAN** — 46 passed, 7 skipped, 1.89 s reported / 3.9 s wall. Unchanged by this pass |
| Stryker cross-check | `cd lab/probes/node-fc && npx stryker run` | **RAN** — `pagination.js`: **90.70 % total / 92.86 % covered, 78 killed, 6 survived, 2 no-coverage, 0 timeouts, 0 errors**, 5 s. Note this against the first pass's **91.86 % / 79 killed / 5 survived** on the same unchanged source: the denominator held at 86, one mutant moved from killed to survived between runs. **Stryker's score is not bit-stable run to run here; its total is.** That is a live example of the README's own instruction to record the qualifier beside the number, and a reason to compare totals rather than scores |

## What was fixed in this pass

Two defects, both in the topic 8 README's new prose, both found by measuring
rather than reading.

1. **"Full line coverage" was false.** Measured above: 90.70 % of
   `pagination.cpp`'s lines under the weak suite, not 100 %. Corrected to the
   claim that is true and checkable — every line of `page()` executed — with
   the uncovered guard named and turned into the point it actually
   illustrates. A topic about coverage over-claiming its own coverage is the
   Goodhart joke landing on the wrong side of the page.

2. **Denominator and numerator were swapped in the one sentence about them.**
   The README said "every assertion you add moves the denominator you are
   trying to read." It does not. All five tools generate mutants from the
   **source**; a stronger suite raises what is killed and leaves the total
   exactly where it was. The sentence now says the score moves and the
   denominator does not — which is also *why* the cross-check table compares
   totals across tools in the first place. Every other use of "denominator" in
   the topic (Stryker discarding uncompilable mutants, cargo-mutants'
   `unviable`, Rust's `usize`) was re-read and is correct.

3. **The layer index named topic 3 as the only six-language topic.** Topic 4
   now implements the other five natively — `nodejs && node --test` (5 pass),
   `golang && go test ./...` (ok), `rust/mock_that_lies && cargo run`
   (suite A 3/3, suite B 1/3), the C++ `g++ -std=c++20` build (heap double
   1/3), and `java && javac DeepStubsLie.java` (suite A 4/4, suite B 0/4) — all
   five run here, exit 0, and topic 4's README states its narrowing explicitly
   ("Python's arm is the lab suite above and is not duplicated here"). The
   index now names topics 3 and 4, and drops topic 4 from the
   narrows-without-saying-so list. Topic 7 remains on it.

Nothing was changed in `java/`, `rust/` or `cpp/` themselves. They are correct
as written.

## Corrections to this file's own earlier sections

The **Coverage gaps, honestly** section above predates two fill-in passes and
two of its four rows are now stale. It is left standing rather than rewritten,
because it is the record of what was true on the first pass; this is the diff:

- **Topic 8** — that row says code exists for Python and Node only, "no PIT, no
  `cargo-mutants`, no `mull`". All three now exist, and step 1 of each runs
  here. Topic 8 implements five of six; Go is absent and its absence is the
  topic's stated finding.
- **Topic 4** — that row says "code exists in **Python only**". It now
  implements all six, verified by running the five non-Python arms above.
- **Topic 7** and **topic 2** rows stand. Topic 2's is now softer than it
  reads: the experiment is still language-agnostic git history plus two Python
  arms, but its README now opens `## The experiment` with an explicit narrowing
  statement and its reason, which is what the other narrowing topics do. The
  gap that remains there is the layer index's, not topic 2's, and it is fixed.

## Still open after this pass

- **Topic 7** implements Python, Go and Node against a six-language mechanism
  section, with no narrowing statement. Unchanged, and the only remaining
  unstated narrowing in the layer.
- **Every step 2 in topic 8 is blocked**, so no mutation score for the Java,
  Rust or C++ arms exists anywhere in this repository — by design, and the
  three scripts say so in their own output. The cross-check table still has two
  rows and gains its other three only when a reader installs a tool.

## Blocked, with the exact one-line unblock

New rows only; everything in the first pass's blocked table still holds.

| What | Why | Unblock |
|---|---|---|
| `08-…/java/run.sh` step 2 (PIT) | Maven is not installed, so `pom.xml` has never been parsed or resolved here | `brew install maven` |
| `08-…/rust/run.sh` step 2 (cargo-mutants) | not installed | `cargo install --locked cargo-mutants` |
| `08-…/cpp/run.sh` step 2 (mull) | not installed — no `mull-ir-frontend-*` under `/opt/homebrew/lib`, `/usr/local/lib` or `/usr/lib`, and no `mull-runner` on PATH | `brew tap mull-project/mull && brew install mull` — then expect to install Homebrew's matching `llvm` too and compile with that `clang++`, since mull's pass plugin, runner and compiler must share one LLVM major version |

---

# Fill-in pass — 2026-08-19 (topics 4 and 7)

**Verified by:** a third independent pass. It did not write the code below, and
it treated the author's report as a set of claims to test, not as evidence.
Every row was produced by running the command the topic README gives, on this
machine, on this date. Same rules as above: no number here is a prediction or a
target, and the `Predict, then record` tables are still blank — re-checked cell
by cell across all nine topics after the edits below.

Scope: the five language arms added to **topic 4**
(`04-test-levels-and-the-mock-that-lies/{nodejs,golang,rust,cpp,java}`) and the
three added to **topic 7**
(`07-fault-injection-slow-not-absent/{cpp,java,rust}`).

## Topic 4 — five arms

| Program | Command (exactly as the README spells it) | Status |
|---|---|---|
| `nodejs/quote.test.js` | `cd nodejs && node --test` | **RAN** — exit 0, 5 pass / 0 fail in 50 ms |
| `golang/` | `cd golang && go test -v ./...` | **RAN** — exit 0, 6 pass. `go vet ./...` clean |
| `rust/mock_that_lies/` | `cd rust/mock_that_lies && cargo run` | **RAN** — exit 0. Also `cargo test` (4 pass) and `cargo clippy --all-targets` (clean, 0 warnings). Cold rebuild from a deleted `target/` works with no network |
| `cpp/nice_naggy_strict.cpp` | `g++ -std=c++20 -O2 -o /tmp/t4_cpp cpp/nice_naggy_strict.cpp && /tmp/t4_cpp` | **RAN** — exit 0. Rebuilt with `-Wall -Wextra` on top: **zero warnings** |
| `java/DeepStubsLie.java` | `cd java && javac DeepStubsLie.java -d /tmp/t4java && java -cp /tmp/t4java DeepStubsLie` | **RAN** — exit 0. Recompiled with `-Xlint:all`: **zero warnings** |

Nothing was changed in any of the five. Read against their claims rather than
their output, each holds:

- **Node.** `registry_mock.js` really does write `require.cache` under a
  resolved filename, which is the mechanism `jest.mock` compiles to. The
  substitution reaching `ledger.js` is not staged: `ledger.js` is required by
  `withStub()` after the stub is installed and gets it because the stub is on
  the shared `tax_rate` module, and no test in the file mocks `ledger`. The last
  test then runs both modules against the *real* dependency and both throw —
  so the four green tests above it are demonstrably green over broken code.
  The hoisting test proves its own converse: stub installed after the require,
  0 calls recorded, real module still in force.
- **Go.** `script` and `heaptable` are registered with `sql.Register` and reached
  through a real `*sql.DB`, so `RecentOrders` runs unmodified against both. The
  driver records every SQL text it is handed; the two strings printed by
  `TestScriptedFakeNeverReadsTheQuery` differ in a `WHERE` clause and an
  `ORDER BY`, and the rows returned are identical. The hand-written fake's
  refusal is a real `error` returned from an unmodelled `ORDER BY`, not a
  printed message.
- **Rust.** `trait Rows` is the seam, `PgRows` implements it with no test hook,
  and both suites drive the same `recent_orders`. Suite A 3/3, suite B 1/3, on
  identical code.
- **C++.** `Strictness` is a real enum consulted on every unexpected call;
  Naggy writes to stderr and records no violation, Strict records one. The three
  output assertions are byte-identical across all three runs, which is the point
  — only the tolerance policy differs.
- **Java.** `DeepStub` is a `java.lang.reflect.InvocationHandler` returning
  further proxies from `children.computeIfAbsent`, so the caching the `verify`
  depends on is visible in the source, and the program prints the identity
  comparison both ways (`true` for the double, `false` for the real objects).

Coverage: topic 4's mechanism section names six languages; Python's arm is the
lab suite and the README says so explicitly. All six are now reachable.

## Topic 7 — three arms

| Program | Command (exactly as the README spells it) | Status |
|---|---|---|
| `cpp/slow_not_absent.cpp` | `g++ -std=c++20 -O2 -pthread -o /tmp/t7_cpp cpp/slow_not_absent.cpp && /tmp/t7_cpp` | **FIXED-THEN-RAN** — exit 0 in 12 s. Two fixes below. Rebuilt with `-Wall -Wextra -Wpedantic`: **zero warnings** |
| `java/SlowNotAbsent.java` | `cd java && javac SlowNotAbsent.java -d /tmp/t7java && java -cp /tmp/t7java SlowNotAbsent` | **RAN** — exit 0 in 12 s. `-Xlint:all`: zero warnings |
| `rust/slow_not_absent/` | `cd rust/slow_not_absent && cargo run --release` | **RAN** — exit 0 in 15 s of run time. Cold rebuild from a deleted `target/` takes about 7 s more and needs no network; `cargo clippy --release --all-targets` is clean |

No arm hangs; the slowest is 15 s, well inside the 60 s bar.

### The open-model claim, checked rather than taken

The README's own broken-experiment note says a closed loop hides the collapse
entirely, so that is the claim worth testing first. All three generators are
genuinely open:

- Each computes an **absolute** schedule `t0 + i*gap` and sleeps until it —
  `std::this_thread::sleep_until`, `LockSupport.parkNanos` in a re-check loop,
  `tokio::time::sleep_until` — so a slow request cannot delay the next arrival.
  Request *i* is dispatched whether or not *i-1* has returned.
- Every latency is taken from that **scheduled** time, not from when a worker
  picked the request up. In the C++ and Rust arms this is `due.elapsed()`; in
  the Java arm it is `(System.nanoTime() - dueNs)` in a `finally`, so it is
  recorded on the failure paths too.
- The result is a histogram a closed loop cannot produce: in every arm the
  slow phase's p99 is an order of magnitude above the mean service time
  (C++ 2199 ms p99 against 104 ms service; Java 3347 ms against 106 ms; Rust
  1455 ms against 102 ms) while the error rate stays at **0.0 %**. Under a
  closed loop the offered rate would have fallen with the system and p99 would
  have stayed near the service time. It did not.
- The Java arm's phase B is the sharpest single check in the topic and it
  reproduces: **pool-wait p99 = 1 ms while p99 = 3347 ms.** The queue is in
  the executor, upstream of the instrumented pool. That is a real measured
  contrast, not a narrated one.
- The Rust arm's C-vs-D pair reproduces too: identical caller-side (p99 255 vs
  254 ms, 704 deadline breaches each) and different dependency-side (peak
  in-flight 43 vs 24, connections destroyed 704 vs 0, abandoned-but-completed
  0 vs 704) — and, in both, the dependency executed 800 of 800. It also guards
  against the failure mode it documents: work abandoned by a deadline is
  drained before counters are read, so the next phase cannot report more
  executions than it was offered.

### What was fixed in this pass

Both defects are in the C++ arm, and both are the same species: a claim the
program made in prose that its own output never exercised.

1. **The C++ arm did not print a generator-lag figure.** The topic README says
   "each program prints its generator's worst lag so you can tell when the
   number is coordinated omission instead," and the Java and Rust arms do. The
   C++ arm measured nothing of the kind, so the reader had no way to tell a
   real queue from a load generator that fell behind — in the one arm that
   spawns an OS thread per arrival, which is the arm most likely to. Now
   measured at each arrival, before the thread is created so thread-creation
   cost lands in the *next* iteration's lag rather than being hidden inside it,
   and printed per phase. It reads 3–5 ms against a 10 ms inter-arrival gap on
   this machine, which is small enough that the rows stand and large enough
   that it was worth knowing.

2. **`Pool::discard` was asserted and never called.** The file's closing
   paragraph made the strongest claim in the arm — a connection abandoned
   mid-response cannot go back in the pool, so a read timeout costs a
   connection — and phase C's read deadline was 500 ms against a ~104 ms
   service time, so it never fired: **read timeouts 0, discards 0**. The
   evidence for the claim was a comment. Added a phase D that sets the read
   deadline *below* the service time (60 ms vs ~104 ms) and a discard counter
   on the pool, so the cost is now a measured column. D reads: 183 read
   timeouts, **183 connections destroyed**, 0 of 200 requests successful — a
   deadline shorter than the dependency's service time is not a bound, it
   shreds connections, succeeds at nothing, and does not stop the dependency
   doing the work anyway. The closing paragraph now prints C's and D's numbers
   side by side instead of asserting the mechanism. This is the Layer 1 failure
   mode (a program that demonstrates something other than what it says) caught
   at its mild end, and it cost about twenty lines to close.

The topic 7 README's `How to run` block was edited in one place: the run
commands were already correct and unchanged, but "each takes roughly ten to
fifteen seconds" now says *of run time* and notes that the Rust arm's first
invocation also compiles tokio. Nothing else in either README was touched, and
no `Predict, then record` cell was filled.

### Checked and left alone

- Every `cd` in both `How to run` blocks resolves against the topic folder, and
  every file each command names exists with the spelling given. The Java class
  names match their filenames in both topics.
- macOS specifics are handled rather than avoided: the C++ arm calls
  `htonl`/`ntohs` unqualified with a comment explaining that they are macros in
  `<sys/_endian.h>` and that `::htonl(...)` therefore does not compile here.
  No `epoll`, no `/proc`, no cgroup paths anywhere in the eight files.
- None of jest, vitest, go-sqlmock, mockall, gMock, Mockito, tokio-console,
  Docker or k6 is required by any of the eight, and none is used. Topic 4's
  unblock table for the five real libraries is present and its commands are
  the correct ones.
- No invented numbers. The only figures in either topic README presented as
  observed are topic 7's "seven tests pass here" for
  `tests/unit/test_resilience.py` — **re-run, 7 passed in 1.4 s** — and topic
  4's "3/3 … 1/3" for the Rust arm, which is a count of assertions and matches
  the run. Topic 7's `P/S = 625 rps` remains arithmetic worked on the page from
  stated inputs.

### Still open after this pass

- **Topic 7's compose ladder is unrun and stays unrun.** Every `docker compose`
  and `k6` line is **BLOCKED**, unchanged from the first pass. The three native
  arms are a different experiment run in-process — they are not a substitute for
  ladders A–F, and the README does not claim they are.
- The layer's coverage-gaps section above still lists **topic 7** as narrowing
  without saying so. That is now out of date: topic 7 ships Rust, C++ and Java
  arms and its README states the split (three clients in the compose stack,
  three self-contained programs here) in its mechanism section. Topic 8's
  six-language gap is closed by the previous pass. **Topic 2** remains the one
  topic whose six-language section has no six-language code.

| Blocked | Why | Unblock |
|---|---|---|
| topic 7 ladders A–F (`docker compose …`) | the Docker daemon is not running | start Docker Desktop |
| topic 7 `k6 run …` | k6 is not installed | `brew install k6`, or run it through compose |
| topic 4 `tests/integration` | the Docker daemon is not running | start Docker Desktop |
| topic 4 `pytest --count=20` | `pytest-repeat` is not installed | `python3 -m pip install pytest-repeat` |

---

# Fill-in pass — 2026-08-19 (topic 8's three compiled arms, and the layer index)

**Verified by:** a second independent pass that did not write the code under
review and did not trust the report describing it. Scope: the three compiled
arms of topic 8, the Python and Node halves of topic 8 that its README claims
run here, and every language-coverage claim in `08-craft/README.md`. Everything
below was produced by running the command the topic README gives, on this
machine, today. No `Predict, then record` cell was filled, and no mutation
score is recorded here — the Stryker run's numbers are the reader's exercise,
so this file records that the tool completed and produced a report, and stops
there.

## The machine, re-checked on this pass

Two entries in the machine table at the top of this file are **stale as of
today**, and are corrected here rather than by editing that table:

| | recorded above | observed today |
|---|---|---|
| Docker daemon | DOWN | **UP** — `docker info` succeeds, containers from unrelated projects are running |
| `testcontainers` (Python) | listed as an absent package | still absent — confirmed by import |

Re-confirmed absent, each verified by `command -v`: `mvn`, `mutmut`,
`cargo-mutants` (`cargo mutants --version` → "no such command"), `mull-runner`
and any `/opt/homebrew/lib/mull-ir-frontend-*`, `k6`, and the `hypothesis`
module. `cargo`, `javac`/`java` (JDK 21), Apple `clang++`, `node` v24.14.0,
`go` and `python3` 3.13.5 are all present. `08-craft/` is still **not** a git
repository (`git rev-parse --git-dir` fails), which is the blocker topic 8's
table gives for `make regression`, and it is the correct one.

## Every program in scope, with status

### Topic 8 — the three compiled arms (`08-coverage-mutation-and-the-regression-rule/`)

| Program | Command | Status |
|---|---|---|
| Java arm | `java/run.sh` | **RAN**, exit 0. Step 1 compiles `Pagination.java` + `WeakChecks.java` with plain `javac` and runs the offline demo; the hand mutant survives the weak suite and the missing assertion is printed. Step 2 **BLOCKED** on `mvn`. |
| Rust arm | `rust/run.sh` | **RAN**, exit 0, 3 tests pass. Step 2 **BLOCKED** on `cargo-mutants`. |
| C++ arm | `cpp/run.sh` | **FIXED-THEN-RAN**, exit 0. Builds with Apple clang `-std=c++23`. Step 2 **BLOCKED** on `mull`. Fix below. |

All three arms' step 1 is a hand-applied single mutant, not a score, and all
three say so in their own output. No mutation score for Java, Rust or C++
appears anywhere in this repository — grepped, confirmed.

The demonstration each arm rests on was checked by hand rather than eyeballed:
the mutant reads the next cursor off the *first* row of the page instead of the
last, so a walk at limit 2 over three distinct timestamps re-serves the boundary
row on every page. All three arms print the same input and the same two walks,
and the arithmetic works out the same way in all three — which is what "ported
line for line" is supposed to mean and is the thing worth checking, because
three arms agreeing by construction is exactly what a copy-paste error would
also look like.

Weak-suite coverage of `page()` re-checked by reading, in all three arms: the
three checks between them reach the `limit < 1` guard, the `cursor == null`
branch and the `cursor != null` branch, and the both-sides of the
`out.size() == limit` cursor decision. The only lines left cold are the
non-termination guards at the end of `walk_pages`, which a correct `page()`
cannot reach. That matches what the topic README claims about these files.

### Topic 8 — the Python and Node halves its README claims run here

| Program | Command | Status |
|---|---|---|
| experiment 1, assertion-free coverage | `coverage run --branch --source=app.core.money,app.core.pagination -m pytest tests/unit/test_no_assertions.py -q` then `coverage report -m` | **RAN**. 2 passed. Both modules reach 100% statement **and** 100% branch, exactly as the README's "record what you actually measure" paragraph says. |
| whole unit suite, both columns | `make coverage` | **RAN** |
| the sysmon comparison | `make coverage-sysmon` | **RAN** |
| experiment 4, Stryker cross-check | `cd lab/probes/node-fc && npx stryker run` | **FIXED-THEN-RAN**. Fix below. |
| the fast-check probe underneath it | `cd lab/probes/node-fc && npx vitest run` | **FIXED-THEN-RAN**, 8 consecutive green runs, ~1.1 s each |
| experiment 2, mutmut | `mutmut run …` / `make mutation` | **BLOCKED** — mutmut absent |
| experiment 5, the regression rule | `make regression BUG=pagination-ties` | **BLOCKED** — not a git repository. Verified the script fails *readably*: it prints the path it checked and the exact `git init && git add -A && git commit` line, and exits 2 rather than erroring somewhere inside git. |

## What was fixed in this pass

**1. The Stryker cross-check did not run at all — a flaky property test aborted
it.** This is the pass's real finding, and the report under review did not
mention it, because that report checked the three compiled arms and took the
README's "It ran here" at its word.

`npx stryker run` died before mutating anything:

```
ERROR DryRunExecutor One or more tests failed in the initial test run:
	keyset pagination WIDE range STILL fails, because fast-check biases toward boundaries
		a widened range did NOT hide the bug from fast-check: expected false to be true
ConfigError: There were failed tests in the initial test run.
```

Stryker runs the *unmutated* suite first and refuses to continue if anything
there fails — correctly, since a suite that already fails cannot tell you
whether a mutant did anything. So one nondeterministic assertion in
`lab/probes/node-fc/pagination.test.js` was enough to delete the entire output
of topic 8's experiment 4, and it failed several directories away from its
cause.

The test asserted that fast-check's boundary-biased integer sampling still finds
the tie bug at a 2^53-wide range. That is *usually* true and was being asserted
as though it were always true. Measured here by repeating the whole `fc.check`
with fresh seeds, 40 independent checks per row:

| `numRuns` | checks that found the tie |
|---|---|
| 2 000 (what the test used) | 24 of 40 |
| 5 000 | 35 of 40 |
| 10 000 | 40 of 40 |

So at the original run count the assertion was close to a coin flip. Fix: pin
both the seed and the run count (`numRuns: 10000, seed: 2026`) on that check and
on the `unbiased: true` check beside it, so the two rows differ by one flag and
nothing else, and record the table above in the test's own comment — the
flakiness rate *is* the finding, and deleting it would have been the wrong fix.
Verified: 8 consecutive `npx vitest run` invocations green, then
`npx stryker run` completed and printed a full clear-text report over
`pagination.js` with a mutant total, a killed count and a survivor list. Its
numbers are not reproduced here; they are the reader's table to fill.

A note for whoever pins seeds next: seeds 1, 2, 3, 4, 5, 42 and 2026 were each
run three times at `numRuns: 10000` and all 21 runs found the tie, so 2026 is
not a cherry-picked seed — it is an arbitrary one from a range where every seed
tried behaves the same way.

Topic 8's `How to run` block gained one paragraph for this: run `npx vitest run`
first and only start Stryker once it is green, with the exact `ConfigError`
quoted so the next person recognises it. Nothing else in that block changed —
every command in it was executed as written and every path it names resolves.

**2. The C++ hand mutant was not the one-token diff it claimed to be.**
`weak_test.cpp`'s header says "diff it against pagination.cpp and you will find
one token". It did not: the mutant had also been rewritten to use a ternary
instead of `std::min`, and had dropped the `filtered.reserve(rows.size())` call.
Neither changes behaviour, but the claim is the point — this arm exists so a
reader can hold one mutation in their head. Restored both lines (adding
`<algorithm>` to the includes), and the diff of the two function bodies is now
one token plus comments. Re-ran `cpp/run.sh`: exit 0, same output.

## Claims checked independently, and confirmed

- **The layer index's language inventory.** Topic 3 ships six language folders.
  Topic 4 ships five folders plus Python-as-the-lab-suite, and says so in the
  sentence "Python's arm is the lab suite above and is not duplicated here" —
  six. Topic 8 ships Python (`lab/api`), Node (`lab/probes/node-fc`), Java, Rust
  and C++ — five, with Go's absence stated as the finding. Topic 7 ships
  `rust/`, `cpp/` and `java/` natively plus the Python/Go/Node compose trio.
  Every count in `08-craft/README.md`'s **The language set** section is correct.
- **The index's topic 7 paragraph**, which the previous pass rewrote, is
  defensible as written: it attributes "unrun" to what `VERIFIED.md` records
  rather than to a machine state, which matters more today than it did
  yesterday, because the daemon is now up and any sentence blaming it would
  have been false within a day of being written.
- **The narrowing statements the index credits.** Topic 1 (porting a four-layer
  FastAPI app six times "would measure your patience"), topic 2 ("this topic
  ships no six-language code, by design", stated twice), topic 5 ("Two extra
  languages, not five: the variable is shrinker output"), topic 6 ("**Three
  languages here, not six**", with the reason), topic 9 ("**Two languages in the
  running experiment**"). All five present, each with its reason.
- **`Predict, then record` tables.** Topic 8's three tables are blank, including
  the cross-check table, whose only pre-filled cells are the tool and module
  labels for the two rows that already existed. Nothing was filled by this pass.
- **The three unparsed configs** each still carry the header saying their tool
  has never run here: `java/pom.xml`, `cpp/mull.yml`, `rust/mutants.toml`.

## Still open after this pass

- **The blocked tables that blame the Docker daemon are now wrong.** Topic 4's
  README, topic 7's README and this file's own earlier blocked tables all give
  "the Docker daemon is not running" as the reason. The daemon is up. Those rows
  were not edited and those experiments were not re-run: changing a reason
  without re-running would assert a result nobody observed, which is the failure
  mode this file exists to prevent. What can be said without re-running is that
  the *stated* reason is stale and at least one row has a second, still-valid
  blocker: topic 4's `tests/integration` also needs the `testcontainers` package,
  which is absent, so it stays blocked either way. Topic 7's ladders A–F need
  `k6`, which is also absent. **This needs a re-verification pass with the
  daemon up, not an edit.**
- **mutmut's command form is unverifiable here.** Topic 8's README and the lab
  `Makefile` invoke `mutmut run --paths-to-mutate …`; `pyproject.toml` also
  carries a `[tool.mutmut]` section with `paths_to_mutate` and `tests_dir`.
  mutmut 3.x moved configuration into the config file, so the flag may be stale
  — but mutmut is not installed and nothing here can settle it. Recorded rather
  than guessed at. Whoever installs mutmut should check the flag first and
  correct the README and the Makefile together.
- **Topic 5's "widening does not hide the bug from fast-check" claim** is now
  backed by a run count where it held 40 times out of 40. At the run count the
  probe previously used it held roughly three times in five. Topic 5's README
  was not edited — the claim as written is true of the test as it now stands —
  but anyone lowering `numRuns` there is lowering the confidence in a stated
  finding, not just making a test faster.

## Blocked, with the exact one-line unblock

| Blocked | Why | Unblock |
|---|---|---|
| `java/run.sh` step 2 (PIT) | Maven is not installed, so `pom.xml` has never been parsed here | `brew install maven` |
| `rust/run.sh` step 2 (cargo-mutants) | not installed (`cargo` itself is present) | `cargo install --locked cargo-mutants` |
| `cpp/run.sh` step 2 (mull) | no `mull-ir-frontend-*` plugin, no `mull-runner` on PATH | `brew tap mull-project/mull && brew install mull` |
| `mutmut run` / `make mutation` | mutmut is not installed | `python3 -m pip install 'mutmut==3.7.*'` |
| row 3 of topic 8's mutation table | Hypothesis is not installed | `python3 -m pip install 'hypothesis==6.165.*'` |
| `make regression BUG=…` | `08-craft/` is not a git repository, so there is no parent commit to check out | `git init && git add -A && git commit -m 'lab baseline'` at the repo root |
| topic 4 `tests/integration` | `testcontainers` is not installed (the daemon is now up, so this is the remaining blocker) | `python3 -m pip install 'testcontainers==4.15.*'` |
| topic 7 ladders A–F, and every `k6 run …` | k6 is not installed | `brew install k6`, or run it through compose |

# Fill-in pass — 2026-08-19 (independent re-verification of topics 4 and 7, daemon up)

Nothing in this section is taken from the pass above it. Every program was
compiled and run again from scratch, with the command its own topic README
gives. This is also the re-verification the previous pass asked for by name:
*"This needs a re-verification pass with the daemon up, not an edit."* The
daemon is up, so the compose half of topic 7 was run rather than reasoned about,
and three of its commands turned out not to work.

## The machine, re-checked on this pass

- `docker info` **exits 0**. Docker Desktop 28.1.1, compose v5.1.4. Every image
  the layer-8 stack needs is already local: `postgres:18`,
  `ghcr.io/shopify/toxiproxy` (CLI 2.12.0), `grafana/k6`, `golang:1.25`,
  `node:24-slim`, `python:3.13-slim`. No pull was needed and none was done.
- `k6` on the host: still absent. Run through compose instead.
- `testcontainers`: absent. `pytest-repeat`: absent. `sqlalchemy` + `asyncpg`:
  present on the host.
- Host ports 8080 and 8081 are held by unrelated containers
  (`interview_frontend`, `interview_admin_frontend`) — see the port fix below.
- Nothing was installed. No daemon was started; the one running was found
  running.

## Topic 4 — every program, with status

| Program | Command run | Status |
|---|---|---|
| `nodejs/quote.test.js` | `cd nodejs && node --test` | **RAN**, exit 0. 5 pass / 0 fail |
| `golang/` | `cd golang && go test -count=1 -v ./...` | **RAN**, exit 0. 6 pass |
| `rust/mock_that_lies` | `cargo run`, and `cargo test` | **RAN**, exit 0 both. Suite A 3/3, suite B 1/3; 4 tests pass |
| `cpp/nice_naggy_strict.cpp` | `g++ -std=c++20 -O2 …` | **RAN**, exit 0 |
| `java/DeepStubsLie.java` | `javac … && java -cp /tmp/t4java DeepStubsLie` | **RAN**, exit 0. Suite A 4/4, suite B 0/4 |
| `tests/unit/test_recent_orders_mocked.py`, `test_commit_mocked.py` | `DATABASE_URL=sqlite+aiosqlite:///:memory: pytest -q` | **RAN**, exit 0. 9 pass |
| `tests/integration/` | `pytest tests/integration -q` | **FIXED-THEN-RAN**, exit 0. Now 4 skips with the unblock named; it was 4 ERRORs |
| `tests/integration --count=20` | — | **BLOCKED**: `pytest-repeat` absent |

## Topic 7 — every program, with status

| Program | Command run | Status |
|---|---|---|
| `cpp/slow_not_absent.cpp` | `g++ -std=c++20 -O2 -pthread …` | **RAN**, exit 0, 11.5 s wall. Run 4x, exit 0 each time |
| `java/SlowNotAbsent.java` | `javac … && java -cp /tmp/t7java SlowNotAbsent` | **RAN**, exit 0, 12.3 s wall |
| `rust/slow_not_absent` | `cargo run --release` | **RAN**, exit 0, 15.0 s wall including the build |
| `tests/unit/test_resilience.py` | `DATABASE_URL=sqlite+aiosqlite:///:memory: pytest -q` | **RAN**, exit 0. 7 pass, as the README says |
| the compose stack | `docker compose up -d postgres toxiproxy api` | **FIXED-THEN-RAN**. Postgres exited 1 before the volume fix |
| ladders B-E's four env knobs | `POOL_TIMEOUT_S=0.5 … docker compose up -d --force-recreate api` | **FIXED-THEN-RAN**. None of them reached the container before the fix |
| toxiproxy proxy + latency toxic | `/toxiproxy-cli create …`, `toxic add …` | **FIXED-THEN-RAN**. The README's argument order is rejected by CLI 2.12 |
| the seed | `docker compose exec api python seed.py` | **FIXED-THEN-RAN**. 50 000 orders, 2 000 customers, 43 750 tied timestamps, 12 611 cancelled, 500 UPDATEd |
| `load/t7_latency_ladder.js` | `docker compose --profile load run --rm k6 run -e STEP=0 -e DURATION=10s -e RATE=50 …` | **FIXED-THEN-RAN**, exit 0. p50/p99 printed `undefined` before the fix |
| `load/t7_clients.js` | same, `-e CLIENT=go` | **FIXED-THEN-RAN**, exit 0. Same `undefined` defect |
| `consumer-go`, `consumer-node` | `docker compose --profile consumers up -d …` | **FIXED-THEN-RAN**. Both 200 with `X-Client-Queue-Ms`; port collision before the fix |
| ladders A–F in full | — | **NOT RUN, and not blocked.** Seven steps x two minutes x five ladders is the reader's exercise, not a verification step. The instrument was smoke-tested end to end instead |

## What was fixed in this pass

1. **`lab/api/tests/conftest.py` — container tests ERRORed instead of skipping.**
   Topic 4's README promises they are *"skipped with an unblock message rather
   than erroring."* They were not. The daemon probe passed (the daemon is up),
   collection therefore did not skip, and all four tests then died in fixture
   setup with a bare `ModuleNotFoundError: No module named 'testcontainers'` —
   no unblock command anywhere near it. Added an `importlib.util.find_spec`
   check at collection time, alongside the daemon probe, and factored the two
   into one `_skip_container(items, reason)` helper. `pytest tests/integration
   -q` now exits 0 with `SKIPPED [4] … the 'testcontainers' package is not
   installed … Unblock: python3 -m pip install 'testcontainers[postgres]'`.
   The 32 unit tests still pass.
2. **`lab/compose.yml` — Postgres 18 could not start at all.** The volume was
   mounted at `/var/lib/postgresql/data`. From `postgres:18` the official image
   keeps the cluster in a major-version subdirectory so `pg_upgrade --link` does
   not cross a mount boundary, and it treats a volume at the old path as an
   *"unused mount/volume"* and exits 1 rather than writing somewhere you did not
   mount. The container died before the healthcheck, so `toxiproxy` and `api`
   never started and every `docker compose` line in topic 7 failed. Moved the
   mount to `pgdata:/var/lib/postgresql`, keeping the named volume the original
   comment is about. The stack now comes up healthy.
3. **`load/t7_latency_ladder.js` and `load/t7_clients.js` — the p50/p99 columns
   printed `undefined`.** `p(50)` and `p(99)` are not among k6's default trend
   stats (`avg/min/med/max/p(90)/p(95)`), so `values['p(99)']` was undefined on
   a run that otherwise succeeded — the quietest possible way to lose an
   experiment, and it silently emptied the two columns the ladder table is built
   around. Added `summaryTrendStats: ['avg','min','med','p(50)','p(90)','p(99)','max']`
   to both. Both now print real numbers.
4. **`compose.yml` + `lab/README.md` — the consumers claimed 8080/8081.** Every
   other host port in the file is deliberately offset; these two were the
   exception, and they are the two most-contended ports on a developer laptop.
   `consumer-go` failed to start with `Bind for 0.0.0.0:8080 failed: port is
   already allocated`. Remapped to 8090/8091 (container ports unchanged, so
   `http://consumer-go:8080` inside the compose network is untouched) and added
   both rows to the lab README's port table.
5. **Topic 7's `How to run` — three commands that cannot work.**
   - `docker compose exec api make seed`: the api image is `python:3.13-slim`
     and has no `make` (`exec: "make": executable file not found in $PATH`).
     Now `docker compose exec api python seed.py`.
   - `toxic add pg -t latency …` / `toxic update pg -n lat …`: toxiproxy-cli
     2.12 parses `toxic add|update [options] <proxyName>`, so a name-first
     invocation dies with `Required argument 'type' was empty`. Proxy name moved
     to the end in both places, and in the header comment of
     `t7_latency_ladder.js`.
   - The seed ran **before** the proxy was created. The api container's
     `DATABASE_URL` points at `toxiproxy:5433`, and toxiproxy opens no listener
     until a proxy exists, so seeding first dies on `ConnectionRefusedError …
     ('172.23.0.3', 5433)` — confirmed by deleting the proxy and re-running.
     Reordered to create-proxy, add-toxic, seed, and verified in that order.
6. **`lab/compose.yml` — ladders B-E did nothing at all.** The worst of the
   five, and invisible: topic 7 runs each fix as
   `POOL_TIMEOUT_S=0.5 docker compose up -d --force-recreate api`, but a shell
   variable reaches a container only if compose names it. `POOL_TIMEOUT_S`,
   `STATEMENT_TIMEOUT_MS`, `REQUEST_DEADLINE_MS` and `BREAKER_LATENCY_MS` were
   in a comment rather than in `environment:`, and `RETRY_ATTEMPTS` /
   `RETRY_BUDGET_PCT` were hardcoded to `"0"`, which *overrides* the prefix.
   Confirmed by running the documented ladder-B command and reading the
   container's own environment: only `RETRY_ATTEMPTS=0`, `RETRY_BUDGET_PCT=0`
   and `POOL_SIZE=5` were present. A reader would have run all four ladders,
   seen four rows identical to the baseline, and concluded the fix kit does not
   work. Added all six as pass-throughs with baseline-preserving defaults
   (`"${VAR:-}"`; `config.py` already reads `""` as unset, and the two integer
   ones default to `0`). Verified in both directions from the api's own startup
   log: with no prefix, `pool_timeout_s=None … retry_attempts=0
   breaker_latency_ms=None`; with the ladder prefix, `pool_timeout_s=0.5
   request_deadline_ms=800 retry_attempts=3 retry_budget_pct=10.0
   breaker_latency_ms=300`. The lab README already documented these names and
   defaults — the contract was right and only the wiring was missing.
7. **The two blocked tables that blamed the daemon**, which the previous pass
   correctly refused to edit without re-running. Re-run, so now edited. Topic 4's
   row names `testcontainers` and the daemon's actual state; topic 7's compose
   row is gone, because those commands work.

## Claims checked against the code, not against the report

- **Topic 4 node.** The stub is installed on the shared dependency, and
  `ledger.js` — named by no test — receives it. Both diagnostics confirmed
  against the real modules: `unknown tax region: undefined` (wrong field name in
  `quote.js`) and `unknown tax region: "eu"` (lowercased key in `ledger.js`).
  The hoisting test records 0 stub calls when installed after `require`, which
  is the mechanism it claims.
- **Topic 4 go.** `TestScriptedFakeNeverReadsTheQuery` really is handed two
  different SQL texts — the driver logs both, one with `ORDER BY … LIMIT` and
  one with neither — and returns the same rows. The hand fake returns `[2 3]`
  against `[5 4 3 2]`, i.e. both bugs, and `executeish` panics on an unmodelled
  `ORDER BY`.
- **Topic 4 rust / cpp / java.** Suite A 3/3 vs suite B 1/3; Nice and Naggy
  green with the warning on stderr only, Strict red on the extra round trip,
  heap red on bugs 1 and 2; deep-stub identity `true` vs real `false`. All
  observed in the output, not inferred.
- **Topic 7, the open-model claim.** Checked in source in all three arms, since
  this is the one defect that would void the topic. C++ `sleep_until(t_start +
  gap*i)` then spawns; Java `parkNanos` to `t0 + gapNs*i` then `exec.execute`;
  Rust `sleep_until` then `tokio::spawn`. Every latency is measured from `due`,
  the scheduled time, not from when the worker started — so queueing is counted.
  Each prints a generator max-lag figure, and all three reported single-digit
  milliseconds, so none of the rows is coordinated omission. The k6 scripts use
  `constant-arrival-rate` and report `dropped_iterations`.
- **Topic 7's C++ phase D.** The README's claim that a deadline under the
  service time shreds connections is exercised, not asserted: `Pool::discard` is
  reached, and the destroyed-connection count is a measured column.
- **Topic 7's Rust C-vs-D.** Caller-side identical, dependency-side not, and
  `the dependency executed N of the M offered` equal in both — the line the
  whole arm is for. Confirmed in the output.
- **Fault injection reaches the database end to end.** With the stack up and the
  toxic at 0 ms, `GET /customers/7/orders?limit=50` returned in 0.0596 s; at
  200 ms it returned in 0.6177 s. The request is not being served from a cache
  or an identity map, which is the second entry in this topic's own
  "experiment is broken" list.
- **Both `Predict, then record` sections are still blank** — 3 tables in topic
  7, 2 in topic 4, every data cell empty.
- **Coverage.** Topic 4 states its narrowing under *The other five languages*
  (Python's arm is the lab suite, not duplicated); topic 7 states its 3-in-
  compose / 3-native split in the mechanism section and again above *The other
  three languages*. Neither topic holds fewer languages than its README covers.

## Still open after this pass

- **Ladders A–F have still never been run to completion here**, and no number
  from them exists anywhere in this layer. That is correct — they are the
  reader's exercise — but it means the compose half of topic 7 is now *verified
  to start and to instrument correctly*, not *verified to produce the knee*. The
  instrument is sound as far as a 10-second step at 50 rps can show.
- **The five images being local is a property of this machine, not of the repo.**
  A machine without them needs a pull, which is a network operation this pass
  did not perform and cannot promise.
- **The stack was left running** with the proxy created, the latency toxic at
  0 ms and the database seeded — ready for ladder A. `docker compose down -v` in
  `08-craft/lab` clears it.

## Blocked, with the exact one-line unblock

| Blocked | Why | Unblock |
|---|---|---|
| topic 4 `tests/integration` | `testcontainers` is not installed. The daemon is up; this is the only remaining blocker, and it now skips with this command in the message | `python3 -m pip install 'testcontainers[postgres]'` |
| topic 4 `pytest tests/integration -q --count=20` | `pytest-repeat` is not installed | `python3 -m pip install pytest-repeat` |
| `k6` on the **host** | not installed | `brew install k6` — or nothing at all, since `docker compose --profile load run --rm k6 …` was run successfully on this pass |

Topic 7's `docker compose` lines are **no longer blocked** and their row has been
removed from that README's table. They were run.
