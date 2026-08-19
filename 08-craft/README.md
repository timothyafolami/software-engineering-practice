# Layer 8 · Craft: code and testing

Nine topics, each with its own `README.md` carrying the concept, the
per-language mechanism, the experiment, and a blank results table to fill in.

The roadmap's one-liner is that this layer decides whether the system is still
workable in two years, "which is where most of a system's cost actually sits."
Every other layer here is about making the machine do the right thing; this
one is about making the *next change* cheap — and it is not a soft interlude:
[topic 3](03-errors-as-interface/README.md) turns a dead database into a fast,
wrong `200 OK`, and [topic 7](07-fault-injection-slow-not-absent/README.md)
makes a dependency *slow* rather than absent, the only setup in this lab that
reproduces a real latency incident's shape.

| # | Topic | The thing it teaches |
|---|---|---|
| 1 | [Deep modules, shallow modules, change amplification](01-deep-and-shallow-modules/README.md) | Layers are not free; measure before arguing |
| 2 | [Coupling and cohesion, measured](02-coupling-cohesion-and-the-wrong-abstraction/README.md) | Your git history disagrees with your directories |
| 3 | [Errors as part of the interface](03-errors-as-interface/README.md) | The silent failure that makes dashboards green |
| 4 | [Test levels, and the mock that lies](04-test-levels-and-the-mock-that-lies/README.md) | A mock supplies the property the bug removed |
| 5 | [Property-based testing with Hypothesis](05-property-based-testing-with-hypothesis/README.md) | **The flagship.** A machine disagrees with you |
| 6 | [Contract tests at service boundaries](06-contract-tests-at-service-boundaries/README.md) | Why testing a generated schema proves almost nothing |
| 7 | [Fault injection: slow, not absent](07-fault-injection-slow-not-absent/README.md) | Little's Law, at the code level |
| 8 | [Coverage as diagnostic, mutation as target](08-coverage-mutation-and-the-regression-rule/README.md) | If I break this line, does anything fail? |
| 9 | [Naming](09-naming/README.md) | A name you cannot write is a design you have not finished |

Do 1, 2 and 9 together — one argument from three angles — then 3 through 8 in
order; 5 is the flagship and 8 depends on it. Topic 9 covers **naming**, a
roadmap design bullet with no coverage here before now; it is numbered last so
topics 1–8 keep the numbers `SEQUENCE.md` cites.

## The shared lab

Most topics need a real service, so they share one Docker Compose stack —
`api` (FastAPI + SQLAlchemy 2.0 async), `postgres:18`, `toxiproxy`, `k6`, and
two generated consumers in Go and Node. Service names, ports, env vars, file
paths, k6 script names and tool pins are specified once in
[`lab/README.md`](lab/README.md); the topics reference it rather than restate
it. Topics 2 and 9 and most of topic 5 need no container — git history and
pure functions, native on macOS 27 / arm64. The rest runs Linux inside Docker
Desktop's VM, where **shapes transfer and absolute numbers do not.**

## The language set

Six — Python, Node.js, Go, Rust, C++, Java — used where they make the
mechanism visible. **Every topic's *How each language actually gets there*
section covers all six. The running code is narrower, deliberately and
unevenly, and this paragraph is the honest inventory.**

[Topic 3](03-errors-as-interface/README.md) and
[topic 4](04-test-levels-and-the-mock-that-lies/README.md) implement all six —
errors in a type versus an exception versus a return code, and a mock library
versus a hand-written fake, are both differences you cannot see without writing
all six.
[Topic 8](08-coverage-mutation-and-the-regression-rule/README.md) implements
five — Python, Node, Java, Rust, C++ — and the missing one is its finding: Go
has no mature mutation tool, so there is nothing to write.
[Topic 7](07-fault-injection-slow-not-absent/README.md) also ships all six, but
split across two venues and only half of it has been run here: Python, Go and
Node are the compose stack's own service and consumers, reached through ladders
A-F, which `VERIFIED.md` records as **unrun**; Rust, C++ and Java are
self-contained programs in the topic folder, and those have run. Its mechanism
section states that split and why a fourth compose client would add a build and
no new mechanism.

Every other topic narrows further, either because the mechanism lives *outside*
the language (topic 2's git history, topic 6's JSON contract) or because porting
a four-layer FastAPI app six times would measure your patience rather than the
idea. Topics 1, 2, 5, 6 and 9 each state their narrowing and its reason in their
own README, and `VERIFIED.md`'s coverage-gaps section records exactly what each
topic implements — including where that section is itself out of date, which it
says in its own **Corrections** heading rather than by quiet rewriting.

## The "you own this when" test

Layer 8 is one of two roadmap layers with **no** "you own this when" block
(Layer 10 is the other). Rather than pretend one was supplied, here is the
test this lab holds you to, built from the roadmap section's two halves:

> You can look at a pull request that adds a layer of indirection and say
> whether it *reduced* total complexity or merely moved it, with a reason that
> isn't a pattern name. And you can state, for any function you have written
> this month, one property that must hold for every input — then watch
> Hypothesis produce a two-element counterexample for it.

The second clause is the sharp one, and topic 5 builds it directly: the
minimum counterexample for its pagination bug is two rows sharing a timestamp.

## A rule this layer enforces

**No result numbers anywhere in this layer.** Every topic ends with a
*Predict, then record* block: prediction first, table after, then check the
outcome against a stated list of ways the *experiment* can be broken rather
than your prediction wrong. Every number in prose is either derived on the
page or carries its source. That is the subject, not overhead — generating
plausible code and plausible tests is the cheap half, and topics 4, 5 and 8
attack the other one: *does this passing test imply anything at all?*

## Resources

**A Philosophy of Software Design** (Ousterhout), 2nd edition, is the spine of
topics 1, 2, 3 and 9; read **`github.com/johnousterhout/aposd-vs-clean-code`**
after it, a written debate between Ousterhout and Robert Martin on exactly
this layer's questions. For the testing half: James Shore's **"Testing Without
Mocks: A Pattern Language"** is the argument topic 4 tests empirically, and the
Hypothesis docs' **"What you can generate and how"** is where topic 5 sends you.

## Next up

**Layer 9 — Writing.** Topics 2 and 7 both produce findings, and a finding
nobody acts on may as well not exist.
