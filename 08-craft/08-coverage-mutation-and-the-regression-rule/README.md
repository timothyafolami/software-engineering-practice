# Layer 8 · Topic 8 — Coverage as a diagnostic, mutation as the target, and the regression rule

### The takeaway (read this first)

**The one idea:** coverage tells you which lines *ran*, which is not remotely
the same as which lines are *checked*. Mutation testing asks the question you
actually care about — if I break this line, does anything fail? — and a
100%-covered module routinely scores far lower.

**Why it matters in practice:** coverage becomes a target the moment it
becomes a CI gate, and the cheapest way to raise it is to write tests with no
assertions. Every organisation discovers this and most respond by raising the
threshold.

**You'll know it landed when:** you look at a surviving mutant and can say
which missing *assertion* let it live, rather than which missing test.

## The concept

Coverage is a **lower bound on ignorance**: uncovered lines are definitely
untested; covered lines are merely *maybe* tested. That asymmetry is the whole
thing. It makes coverage excellent as a diagnostic — "we have no tests at all
for the refund path" is a real finding — and terrible as a target, because
`assert True` after calling the function scores identically to a real test.
This is Goodhart's law with a dashboard.

**Mutation testing** closes the gap. Introduce a small semantic change — `<`
to `<=`, `+` to `-`, a return value to `None`, a conditional to `True` — and
run the suite. If the suite still passes, that mutant **survived**, and there
is a behaviour your tests do not constrain. Mutation score is killed ÷ total
viable mutants. The cost is that it runs your suite once per mutant, which is
why it belongs on your two or three most important pure modules in a nightly
job — not on everything, and not per-PR.

Two properties of mutation testing that people discover too late:

- **A surviving mutant names a missing assertion, not a missing test.** The
  test that should have killed it usually already exists and already calls the
  right function; it just never looked at the thing the mutant changed. That
  reframing is the single most useful output of this topic.
- **Equivalent mutants exist and cannot be killed.** A mutation that produces
  semantically identical behaviour (changing a bound that a caller can never
  reach, say) will survive forever, correctly. A mutation score of 100% is
  therefore not the goal, and any policy that demands it will be gamed within
  a sprint.

Current Python tooling, honestly: **mutmut 3.7** is the practical choice —
fast, incremental (it remembers progress), with intelligent test selection and
a terminal UI, and it requires `fork()` (fine on macOS, WSL on Windows). Note
the 3.x architecture change: it mutates code **inside functions only**, so
module-level code is not mutated at all. **cosmic-ray** remains the more
thorough option: a broader operator set and a *kill matrix* mapping which test
killed which mutant, which is the more useful artifact when you are trying to
improve a suite rather than score one. Start with mutmut; reach for
cosmic-ray's kill matrix when a module scores badly and you need to know why.

Also worth knowing for 2026: **coverage.py 7.15** uses `sys.monitoring` as its
default measurement core on Python 3.14+ (opt-in via `COVERAGE_CORE=sysmon`
from 7.4 on 3.12+), substantially faster than the old trace function. Two
caveats before you flip it: plugins and dynamic contexts are not supported
under sysmon.

**The regression rule**, which the roadmap calls non-negotiable and which is
the highest-value habit in this entire layer: *every bug fix gets a test that
fails before the fix.* Not "a test" — a test you have **watched fail**. A test
written after the fix, against the fixed code, has never demonstrated that it
can detect the bug, and a distressing fraction of them cannot. This is
mechanisable, and mechanising it is the point of the last experiment.

## How each language actually gets there

Six languages, and the axis is genuinely mechanical rather than cultural:
**where in the pipeline a mutant can be introduced** — source, bytecode, or
compiler IR — determines how fast mutation testing is, how many invalid
mutants it generates, and therefore whether anyone runs it twice.

**Python (your stack).** Source-level mutation, interpreted, so a mutant costs
a full test-suite run and there is no compile step to reject nonsense.
`coverage run --branch -m pytest` (branch coverage on: statement coverage
alone cannot distinguish `if x:` taken from not-taken), `mutmut run` for
mutation, `pytest --lf`/`--ff` to iterate on the failing regression test.

**Java, the best in this list.** PIT mutates **bytecode**, so there is no
recompilation per mutant and no invalid-mutant problem; it has incremental
analysis and mutates only code reachable from changed tests. This is the one
language where per-PR mutation testing is genuinely practical, and it is worth
knowing about even if you never write Java, because it shows that the reason
mutation testing feels expensive elsewhere is an implementation detail rather
than a law.

**Rust.** `cargo-mutants` mutates source and leans on the compiler: mutants
that do not typecheck are discarded before any test runs, which removes a
whole category of noise. The cost is a compile per mutant, which is
substantial — the tradeoff is exactly inverted from Python's. `cargo-llvm-cov`
covers the coverage half, with region coverage that is finer-grained than
Python's branch coverage.

**C++.** `mull` mutates **LLVM IR** and patches mutants in at runtime, so one
compilation serves many mutants — the closest thing to PIT's speed outside the
JVM. Coverage comes from `gcov` or `llvm-cov`. The C++-specific hazard is that
a mutant can turn a well-defined program into an undefined-behaviour one, at
which point "the test suite crashed" is neither a kill nor a survival and you
have to decide what to record.

**Node.** `c8`/V8 coverage plus **Stryker** for mutation. Stryker's
distinguishing feature is a TypeScript checker that discards mutants which
would not compile — which materially cuts noise *and quietly changes the
denominator*, so a Stryker score and a mutmut score are not directly
comparable. Knowing that before you compare two teams' numbers saves an
argument.

**Go.** `go test -cover` is excellent, and since 1.20 it can collect coverage
from integration test *binaries* rather than only unit tests, which is
genuinely useful and underused. Mutation testing in Go has no equivalently
mature tool — this is a real ecosystem gap, worth knowing so you do not spend
an afternoon looking for one.

## The experiment

1. **Coverage lies, demonstrated.** Get `core/pagination.py` and
   `core/money.py` to 100% *statement* coverage using tests with **no
   assertions** — just call the function. Confirm `coverage report` says 100%.
   Then turn on `--branch` and see what it says. This takes ten minutes and
   permanently inoculates you against a coverage gate.
2. **Mutation on the same modules, three ways.** Run `mutmut run` against both
   modules with (a) the assertion-free tests, (b) only the example-based
   tests, (c) example tests plus topic 5's Hypothesis properties. Record the
   mutation score for each. The third row is the argument for property-based
   testing that a skeptic will actually accept, because it is a number about
   their own code.
3. **Read the survivors.** For each surviving mutant, write down the missing
   *assertion* — not the missing test. Then add it and re-run. Note any mutant
   you decide is equivalent, and say why.
4. **Cross-check the denominator.** Run Stryker against topic 5's Node port of
   `page()`. Its TypeScript checker discards mutants that would not compile,
   so the total differs from mutmut's on the same algorithm. Record both
   totals side by side — this is how you learn that "mutation score" is not a
   universal unit.
5. **Mechanise the regression rule.** Write `make regression BUG=<slug>` that
   checks out the parent commit's `app/` source, keeps the *new* test, runs
   it, and **fails the build if the test passes**. Then wire it as a CI job on
   any PR labelled `bugfix`. Use topic 5's pagination bug as the first
   subject, including verifying that the `@example(...)` you pinned genuinely
   fails against the pre-fix source.

## How to run

```
cd 08-craft/lab/api

# 1. coverage lies: 100% from tests with no assertions
DATABASE_URL=sqlite+aiosqlite:///:memory: \
  python3 -m coverage run --branch --source=app.core.money,app.core.pagination \
  -m pytest tests/unit/test_no_assertions.py -q && python3 -m coverage report -m
make coverage            # the whole unit suite, statement and branch
make coverage-sysmon     # 3.12+; compare the wall clock

# 2. mutation on the same two modules, three test sets
mutmut run --paths-to-mutate app/core/pagination.py,app/core/money.py
mutmut results
mutmut show <id>

# 5. the regression rule, mechanised
make regression BUG=pagination-ties
```

The three test sets for step 2 are already separated:
`tests/unit/test_no_assertions.py` (row 1, calls everything and checks nothing),
`tests/unit/test_pagination_examples.py` + `tests/unit/test_money_examples.py`
(row 2), and `tests/properties/` (row 3). Point `--tests-dir` at each in turn.

**Record what you actually measure for step 1.** On these two modules the
assertion-free tests reach 100% *statement* and 100% *branch* coverage here,
because every branch is reachable by simply calling the function. That is a
sharper result than "branch catches what statement missed", not a weaker one: it
means neither coverage flavour has anything left to say, and mutation is the only
measurement that can still tell this file apart from a real suite. `--branch` is
also set in `pyproject.toml`, so passing it on the command line is not what turns
it on -- which is topic 8's last broken-experiment note, live.

`make regression` runs `lab/tools/regression.sh`, which checks out the parent
commit's `api/app/` **only**, keeps the new test exactly as the PR wrote it, runs
it, and fails if it passes. Reverting the tests along with the source is the one
bug that makes this gate worse than nothing, so the script does not do it and
says so where the next person will look. The first subject is
`tests/regression/test_pagination_ties.py`, which pins the shrunk counterexample
with no Hypothesis at all -- the generator found the input and its job is done.

The Node cross-check runs natively and is the step to do first if the rest is
blocked:

```
cd 08-craft/lab/probes/node-fc && npm install && npx stryker run
```

It ran here and produced a full report over `pagination.js`, so the "mutants
generated" column has a real number to sit next to mutmut's. The two are *not*
the same unit: Stryker discards mutants that would not compile, which changes
the denominator, and knowing that before comparing two teams' numbers saves an
argument.

**Run `npx vitest run` first, and only start Stryker once it is green.** Stryker
begins with a *dry run* of the unmutated suite and aborts the entire run --
`ConfigError: There were failed tests in the initial test run` -- if a single
test fails there, because a suite that already fails cannot tell you whether a
mutant did anything. That makes one flaky test in `pagination.test.js` enough to
delete this whole experiment's output, which is why the two wide-range property
checks in that file pin both their seed and their run count. If you loosen
either, expect this step to start refusing to run rather than to start giving
you a different number: the failure is loud, but it lands on the *tool*, several
directories away from the test that caused it.

The three compiled arms -- **Java (PIT), Rust (cargo-mutants), C++ (mull)** --
are this topic's axis made runnable. Each is `page()` ported line for line from
`app/core/pagination.py`, with a deliberately weak suite over it, so that five
tools which insert a mutant at five different points in the pipeline can be
pointed at one algorithm and their denominators compared:

```
cd 08-craft/08-coverage-mutation-and-the-regression-rule

java/run.sh     # javac + the weak suite, then PIT            (bytecode)
rust/run.sh     # cargo test, then cargo-mutants    (source, compiler-checked)
cpp/run.sh      # clang++ + the weak suite, then mull         (LLVM IR)
```

Each script runs in two labelled steps. **Step 1 needs nothing but the
language's own toolchain and runs here**: it executes the weak suite, then
applies *one* mutant by hand -- the next cursor read off the first row of the
page instead of the last -- shows it passing that suite unchanged, and prints
the single assertion that would have killed it. One mutant chosen by a human is
not a mutation score, and none of these three arms records a score anywhere.
**Step 2 is the tool**, and on this machine all three report BLOCKED with the
install command in the table below.

The weak suites are the subject of the experiment, not scaffolding around it:
`rust/tests/weak_suite.rs`, `cpp/weak_test.cpp` and
`java/src/test/java/craft/weak/` each check that a page is no longer than the
limit, that a zero limit is rejected, and that the walk terminates -- and never
look at *which* rows or *what* cursor came back. Every line of `page()` executed,
three green tests, mutants alive. The only lines these suites leave cold are the
non-termination guards in `walk_pages`, which a correct `page()` cannot reach --
worth noticing, because "unreachable by construction" and "untested" are the two
things a coverage percentage cannot tell apart, which is the topic in one line.
Keep them weak until you have recorded the first number; every assertion you add
moves the *score*, not the denominator -- all five tools generate their mutants
from the source, so a stronger suite raises what is killed and leaves the total
where it was. That is the whole reason a denominator is comparable across
suites and a score is not.

Read the three configs before running them, because none has been parsed by its
tool here: `java/pom.xml` (version pins unresolved), `cpp/mull.yml` (mutator
group names and config-file location unconfirmed) and `rust/mutants.toml`. Each
says so in its own header rather than pretending otherwise -- treat a rejected
key as a stale pin, and correct the file rather than working around it.

The cross-check table below has rows for mutmut and Stryker. **Add one row per
tool you unblock**, and record the qualifier alongside the number: PIT's mutator
group, cargo-mutants' `unviable` count, and mull's crash-versus-kill decision
each change what the score means, so a number without its qualifier is not
comparable to the row above it.

**Blocked on this machine, with the exact unblock command:**

| What | Why | Unblock |
|---|---|---|
| `mutmut run` / `make mutation` | mutmut is not installed | `python3 -m pip install 'mutmut==3.7.*'` |
| row 3 of the mutation table | Hypothesis is not installed | `python3 -m pip install 'hypothesis==6.165.*'` |
| `make regression` | `08-craft/` is not a git repository, so there is no parent commit to check out | `git init && git add -A && git commit -m 'lab baseline'` at the repo root |
| `java/run.sh` step 2 (PIT) | Maven is not installed, so `pom.xml` has never been parsed here | `brew install maven` |
| `rust/run.sh` step 2 (cargo-mutants) | not installed | `cargo install --locked cargo-mutants` |
| `cpp/run.sh` step 2 (mull) | not installed -- no `mull-ir-frontend-*` pass plugin, no `mull-runner` on PATH | `brew tap mull-project/mull && brew install mull` |

`make coverage`, the Stryker run, and **step 1 of all three compiled arms** work
here as-is. mull is additionally pinned to one LLVM major version: the pass
plugin, `mull-runner` and the compiling clang must all agree, so on Apple
Silicon budget for Homebrew's matching `llvm` as well and compile with that
`clang++` (`CXX=$(brew --prefix llvm)/bin/clang++ ./run.sh`).

## Predict, then record

Predict, before running: what mutation score do you expect from the
assertion-free tests at 100% statement coverage? From the example tests? From
example plus property tests? Write all three down. Then predict how many
mutants you will end up calling *equivalent*.

| Test set | statement cov | branch cov | mutants killed / total | score |
|---|---|---|---|---|
| no assertions | | | | |
| example tests only | | | | |
| example + Hypothesis properties | | | | |

| Surviving mutant | file:line | the missing *assertion* | equivalent? |
|---|---|---|---|
| | | | |
| | | | |

| Tool | module | mutants generated | killed | discarded as uncompilable |
|---|---|---|---|---|
| mutmut (Python) | `pagination.py` | | | n/a |
| Stryker (Node port) | `pagination.js` | | | |

**What would mean the experiment is broken rather than your prediction wrong:**

- **A mutation score of 100% on the first try.** mutmut probably ran zero
  mutants. Check `mutmut results` for the count and check that
  `--paths-to-mutate` actually matched — a typo'd path fails quietly. Also
  remember mutmut 3.x mutates inside functions only, so a module that is
  mostly module-level constants will legitimately produce very few mutants.
- **A score near 0% with a suite you know is good.** The suite is probably not
  being found, or is erroring out entirely. Run the exact test command mutmut
  uses, by hand, and read the output.
- **Many "suspicious" or timed-out mutants.** Your baseline suite is too slow:
  mutmut times mutants out relative to baseline runtime, so a slow suite makes
  every survivor look like a timeout. Point it at the pure modules only, with
  the unit tests only.
- **`make regression` passes on the first bug you try.** Verify it is actually
  reverting the source. The most common bug in that script is reverting the
  *tests* along with the source, which trivially makes everything pass and
  makes the gate worse than nothing.
- **Branch coverage equals statement coverage exactly.** Check that `--branch`
  is really on (a `.coveragerc`/`pyproject` setting can override the flag);
  on a module with any conditional at all the two numbers should differ.

## Answer before moving on

1. Branch coverage caught things statement coverage did not. Construct a
   function where 100% *branch* coverage still misses an obvious bug.
2. Mutation testing is expensive. Design the policy you would actually put in
   CI — which modules, which trigger, what threshold, what happens on a
   regression — and justify each choice on cost rather than principle.
3. "Every bug fix gets a test that fails before the fix." Name a real bug
   class where this rule is impractical, and say what you would do instead.
4. Your property tests killed mutants the example tests did not. Name a mutant
   that a property test structurally *cannot* kill but an example test can —
   then say which number, mutmut's or Stryker's, you would put in a report
   about the same algorithm, and what you would have to write next to it for
   that number to be honest.

## Next up

[Topic 9 — Naming](../09-naming/README.md): the last topic, and the one that
turns out to be about topic 1. A name is an interface; the pagination bug you
found in topic 5 was partly a naming bug, and this topic shows you why.
