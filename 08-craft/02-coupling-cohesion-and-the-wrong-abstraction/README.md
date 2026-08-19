# Layer 8 · Topic 2 — Coupling and cohesion, measured; and when not to abstract

### The takeaway (read this first)

**The one idea:** coupling is not a property of code you can see in one file —
it is the probability that changing A forces you to change B. That probability
is recorded, for free, in your git history, and it very often disagrees with
your directory structure.

**Why it matters in practice:** every architecture argument you will ever have
is really an argument about coupling, conducted using pattern names as
proxies. Replacing the proxy with a measurement ends most of those arguments
in about ten minutes, and occasionally proves that the "clean" structure is
the coupled one.

**You'll know it landed when:** you can point at two files in your production
repo that change together most of the time despite living in different
packages, and say what the module boundary should have been instead.

## The concept

**Cohesion** is how much the things inside a module belong together.
**Coupling** is how much modules depend on each other. The textbook
definitions are useless in a code review because they are unmeasurable as
stated. The operational versions are not:

- **Temporal (logical) coupling.** Across your commit history, for files A and
  B: `P(B changed | A changed)`. High values across a package boundary mean
  the boundary is in the wrong place — the code is telling you where the real
  module is, and it is not where the directories say. Computable from
  `git log` in about thirty lines of Python.
- **Cohesion.** For a module: the fraction of its commits that touched *only*
  it. A module whose commits always drag in three other files is not cohesive
  no matter how tidily its methods are grouped.

Temporal coupling is strictly better evidence than static import analysis for
one specific reason: it catches coupling that has **no import at all**. Two
services that must be deployed together, a JSON contract duplicated on both
sides of a queue, a migration and the model it matches, a feature flag and the
three files that read it — none of these produce an edge in an import graph,
and all of them produce one in the commit history.

Then the sharpest line in this layer: **duplication is cheaper than the wrong
abstraction.** The mechanism is specific, not a vibe. When you deduplicate two
similar things, you create a shared module whose callers now have *different*
requirements. Each divergence gets absorbed as a parameter. Parameters that
exist only to select behaviour — Ousterhout calls them configuration
parameters, Sandi Metz's version is "the wrong abstraction" — are pure
interface surface with zero functionality behind them, and they compound: N
boolean flags gives the shared function 2^N behaviours, and your tests cover
about three of them.

The rule that actually survives contact with a codebase: **duplicate until the
third occurrence, and only abstract when the three are the same for the same
*reason*.** Two functions that look identical but change for different reasons
are not duplication — they are a coincidence, and merging them couples two
things that were correctly independent. Topic 9's naming work is the practical
test for "same reason": if you cannot name the shared thing without using
"and" or a generic noun, the three cases are not one concept.

## How each language actually gets there

Six languages, and the variable is *what the language makes easy to couple by
accident*. Every one of these is a mechanism you can point at in a diff, not a
style preference.

**Narrowing, stated plainly: this topic ships no six-language code, by design.**
The section below is a reading guide for spotting the mechanism in each
language, not a set of programs. Part 1's experiment is `git log` analysis,
which is language-agnostic and runs against *your own* repository — the
coupling it finds is real coupling in real code, which no synthetic six-language
demo could match. Part 2's two router arms are Python only because the arms
differ in *shape*, not in runtime behaviour, so five more translations would add
volume and no signal. If you want the contrast in another language, the honest
exercise is to run Part 1 against a Go or Node repo you actually work on.

**Python (your stack).** The trap is the shared Pydantic model. One
`OrderResponse` used by the public API, an internal admin endpoint and a
Celery task looks like DRY and is a hard coupling between three consumers with
three different change rates: the day the public API needs a field removed,
you cannot remove it. Duplicate the schema per boundary. This is the single
most valuable "stop doing DRY here" instruction for a FastAPI codebase and it
is nearly always resisted, because the duplicate looks like a mistake in
review and the coupling does not look like anything at all.

**Node.** The barrel file (`index.ts` re-exporting everything) creates
coupling no other module system makes this easy to create by accident: every
consumer of one symbol acquires a build-graph edge to all of them. The
observable consequence is not just rebuild time — it is that circular imports
appear between modules that never referenced each other, because the barrel
sits in the middle of both paths.

**Go.** Consumer-side interfaces make this structurally easier: each package
declares the narrow interface it needs, so two consumers of the same struct
are not coupled through a shared interface definition the way they are in
Java or C#. Worth internalising as a *design* idea rather than a Go feature —
define the interface where it is used, in any language. Go's own coupling
trap is different: a `models` or `types` package that everything imports,
which is a barrel file with a compiler.

**Java.** Coupling arrives through inheritance and through shared DTOs in a
`common` module. A protected field in a base class is a contract with every
subclass, forever, and unlike a shared struct it is not visible at the call
site. The mechanical audit worth running: for each class in `common`, count
the modules that depend on it and the number of distinct teams that own them.
`jdeps` and ArchUnit will both produce this, and ArchUnit can then *fail the
build* on a layer rule, which is the only enforcement in this list that
survives a busy quarter.

**Rust.** The wrong abstraction shows up as generic-parameter and
`where`-clause creep. Merging two similar functions means unifying their
types, which means a type parameter, which means a trait bound, which then
propagates into every caller's signature. That propagation is the coupling,
made visible by the compiler: when a change to one call site forces a
`where` clause three modules away, the type system has just printed the
coupling graph for you. Rust also has the cleanest cohesion enforcement in
this list — a module's private items are private, full stop, so "who can
depend on this" is a compiler question rather than a convention.

**C++.** The same disease with the highest bill. Configuration parameters
become template parameters and `if constexpr` branches, so N flags becomes N
instantiations of everything downstream — compile time grows, error messages
become unreadable, and the coupling is now physical: touching the header
rebuilds every consumer. C++ is where "the wrong abstraction is expensive"
stops being a metaphor and starts being a number on your CI dashboard.

## The experiment

**No per-language code in this topic, and the reason is the measurement itself.**
Part 1 reads `git log`, which is language-agnostic — the tool below runs against
a repository in any of the six, including yours — and part 2 needs two arms of
one real service to stage requirements against, which is the lab's Python. The
six mechanisms above are things to recognise in a diff, not six ports to build.

Two parts, and the first one uses **your real production repository**, not the
lab. This is deliberate: the finding you cannot dismiss is the one about code
you wrote.

**1. Temporal coupling on real history.** `tools/temporal_coupling.py` reads
`git log --name-only --pretty=format:%H --no-merges` over the last ~12 months,
builds the co-change matrix, and reports the top 30 pairs by
`co_changes / min(changes_A, changes_B)`, with a floor of ~8 changes each so
that a pair which changed twice together and never apart does not top the
list. Run it against your production service. Then classify every pair in the
top 30 as one of:

- (a) legitimately one concept in the wrong directory,
- (b) a leaky abstraction — B has to change because A's interface does not
  actually cover the case,
- (c) a file that changes with everything (lockfile, changelog, version
  header) — add to the exclusion list and re-run.

The classification is the exercise. The number alone tells you nothing about
which fix applies.

**2. The wrong abstraction, staged.** In the lab, `POST /orders` and
`POST /orders/draft` share roughly four fifths of their body. Extract a shared
`_create(...)`. Then apply three real requirements *one at a time*, in this
order:

1. drafts skip inventory reservation,
2. real orders emit an event,
3. drafts allow a null customer.

After each, record the number of parameters in `_create` that exist only to
select behaviour, and the diff size of that change. Then implement requirement
three *again* from the duplicated version and compare the two diffs directly.

The reason for staging it rather than arguing about it: the shared version
wins the first requirement almost every time, and that is exactly why teams
extract too early. The question is what the curve does by requirement three.

## How to run

Part 1 needs no container and no lab -- it reads your own repository.

```
python3 08-craft/lab/tools/temporal_coupling.py --repo ~/path/to/your/service --months 12
python3 08-craft/lab/tools/temporal_coupling.py --repo ~/path/to/your/service --months 12 \
  --exclude 'poetry.lock,pyproject.toml,CHANGELOG.md'
```

The tool prints the top pairs by `co_changes / min(changes_A, changes_B)` with a
`class` column left blank -- classifying each pair as (a), (b) or (c) is the
exercise, and a tool that guessed would be inventing the finding. It also prints
per-module cohesion (the fraction of a module's commits that touched only it).
Useful flags: `--min-changes` is the floor that stops a pair which changed twice
together from topping the list, and `--max-files` drops formatting sweeps that
would otherwise flatten the whole matrix.

Part 2 is the staged extraction, and both arms are already in the lab at
requirement 0:

```
cd 08-craft/lab && docker compose up -d api postgres
docker compose exec api pytest tests/unit -q

git switch -c t2-shared
# apply requirements 1, 2 and 3 as three separate commits, in BOTH files:
#   lab/api/app/routers/orders_duplicated.py   (arm A: POST /dup/orders, /dup/orders/draft)
#   lab/api/app/routers/orders_shared.py       (arm B: the extracted _create)
git log --stat -3
```

After each requirement, record `_create`'s parameter count and the diff size for
that one commit. The three requirements are listed in both module docstrings so
the order cannot drift between the two arms.

**Blocked on this machine:** the `docker compose` lines only -- the daemon is not
running (`docker info` fails); start Docker Desktop. `temporal_coupling.py` is
standard library only and runs here as-is.
## Predict, then record

Predict before running: which two files in your production repo do you
*expect* to top the coupling list? Write the names down before you look — the
prediction is the measurement's control, and this is the topic where being
wrong is most informative.

| Rank | file A | file B | co-change ratio | class (a/b/c) |
|---|---|---|---|---|
| 1 | | | | |
| 2 | | | | |
| 3 | | | | |

| Requirement | shared: new config params | shared: lines changed | duplicated: lines changed |
|---|---|---|---|
| drafts skip reservation | | | |
| orders emit event | | | |
| drafts allow null customer | | | |

**What would mean the experiment is broken rather than your prediction wrong:**

- **Your top pairs are all lockfiles, changelogs or version headers.** That is
  a tooling artifact, not a design finding. Fill in the exclusion list and
  re-run; the second run is the real one.
- **Nothing scores above 0.3.** Check whether your team squash-merges large
  PRs. Squashing collapses a week of unrelated work into one commit, which
  makes every file in the PR look co-changed with every other and then
  normalises the whole matrix flat. Run against pre-squash branch history, or
  compute co-change over per-PR file sets rather than per-commit.
- **Everything scores above 0.8.** The opposite artifact — usually a repo with
  a formatting or codegen commit that touched every file. Exclude commits
  above some file-count threshold and re-run.
- **The duplicated version wins all three requirements.** Be suspicious of
  yourself before concluding "never abstract". Pick a fourth requirement that
  genuinely applies to *both* paths — a change to how order totals are
  computed, say — and see whether the duplicated version now loses. If it
  does, you have found the actual boundary: the shared thing was the total
  calculation, not the endpoint body.

## Answer before moving on

1. Temporal coupling measures co-change. Name a pair of files that *should* be
   highly coupled, where that is correct design rather than a smell — and say
   what distinguishes it from a pair that should not be.
2. You found a pair at 0.85 across a package boundary. Give two different
   fixes — one that moves code, one that does not — and say which is right
   when the two files are owned by different teams.
3. "Abstract at the third occurrence, if they are the same for the same
   reason." How would you actually *check* "the same reason" rather than
   asserting it? (One good answer is in topic 9.)
4. Static import analysis and temporal coupling disagree about a pair of
   files. Construct the case where the import graph is right and the history
   is misleading.

## Next up

[Topic 3 — Errors as part of the interface](../03-errors-as-interface/README.md):
the first topic that points straight at your production latency problem, via
a `try` block that turns a dead database into a fast, successful, empty
response.
