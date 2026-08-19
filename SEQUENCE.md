# The running order

**Layers 1 through 10, in order, as the roadmap wrote them.**

An earlier version of this file reordered everything around a production
latency incident. That was the wrong instrument for the wrong goal, and it has
been replaced. The purpose here is understanding and design mechanism — the
things that stay true after the frameworks change — not triage. If a live
problem shows up, the material is there to reach for; it does not get to
reorganise the syllabus around itself.

## Order, not schedule

There are no week numbers in this file, deliberately. Layer 1 took one day.
Pacing prescriptions written by someone who cannot see how fast you actually
move are noise, and the roadmap's own "five focused hours a week" is a floor
for someone else.

What the order *does* encode is **dependency** — which layers are harder to
understand if you skip what comes before. That is real and worth respecting.
Speed is yours to set.

| Layer | Depends on | Why it sits here |
|---|---|---|
| **1 · The machine** + `07-inside-a-container` | — | Everything above bottoms out here. The container extension belongs with it: same subject, one level up. |
| **2 · The network** | 1 | Sockets, epoll and file descriptors are Layer 1 vocabulary. Connection pools and timeouts are Layer 5's mechanism seen early. |
| **3 · Data** | 1, 2 | The highest-return layer on the page. Isolation levels need transactions; pool sizing needs Layer 2; MVCC needs Layer 1's memory model. |
| **4 · Distributed** | 3 | Partial failure is only interesting once single-node consistency is solid. Raft is the flagship — it is a Go project, which suits you. |
| **5 · Failure** | 3, 4 | Timeout budgets, retry storms and metastability are distributed-systems problems wearing operational clothes. |
| **6 · Observability** | 5 | You cannot alert on symptoms you have no vocabulary for. Error budgets mean little before you have watched something fall over. |
| **7 · Security** | 3, 6 | IDOR is an authorization bug in a query; SQL injection is a string-building bug. Both read better with Layer 3 in hand. |
| **8 · Craft** | all of 1-7 | Judging an abstraction requires having been burned by one. Property-based testing is the flagship and it lands hardest last. |
| **9 · Writing** | 5, 6 | The design doc and the postmortem both need something real to be about. |
| **10 · The edge** | 1, 3, 5 | Inference is bandwidth-bound (Layer 1), serving is queueing (Layer 5), and the maths pays off across all of it. |

## Two things that genuinely want to run continuously

Not a reordering — both stay at their numbered positions. But both compound
if practised throughout rather than saved for their turn:

- **Layer 9 (writing).** One design doc and one technical post a month, from
  now, about whatever layer you are actually in. The roadmap is right that
  this is the most underrated multiplier; it is also the only layer whose
  output other people ever see.
- **Layer 10 (the edge).** One paper a week read properly. It costs an hour
  and it is the half of your stated goal that the other nine layers do not
  touch.

Do these alongside. Do not let them reorder anything.

## Layers are units of authorship, not units of study

Some layers are worth taking in pieces — not resequenced, just not swallowed
whole:

- **Layer 6** has three natures inside it. Topics 1-3 (the three signals, the
  real p99, trace propagation) are diagnosis and are useful the moment you
  read them. Topics 4-5 (cardinality, RED/USE) assume failure vocabulary from
  Layer 5. Topics 6-7 (SLOs, error budgets, symptom alerting, postmortems)
  are organisational and are worth far more once you have real incidents to
  point at.
- **Layer 7** splits similarly. Topics 1-2 (broken access control / IDOR, SQL
  injection) are largely a read-and-grep against code you already run. The
  rest (XSS, JWT, SSRF, supply chain, crypto) is study.
- **Layer 10** splits by dependency — some rides along with the SWE material,
  some needs its own contiguous run.

## The one rule that outranks the order

Predict before you run. Every topic, every time, written into
`PREDICTIONS.md` before the first command.

Layer 1 took a day, and the four rows already seeded in that file are all
from Layer 1 — a fabricated benchmark table, a race result that was really
optimizer hoisting, and two topics whose code had never executed on this
machine. Speed is not the risk. Reading something, recognising it, and
mistaking that for knowing it is the risk, and a logged prediction is the
only thing that reliably tells the two apart.

If you can already state the answer before running the experiment, and you
are right, skip it and move on. That is what the log is for.

## Build once, reuse: who owns which shared experiment

Six layers independently specify a connection-pool-exhaustion experiment.
Five specify a retry storm. Four specify Toxiproxy. Building each of those
six times is the single biggest time sink available in this repo, so:

**Build `lab-harness/` in Block A** — one compose stack: FastAPI + Postgres +
Toxiproxy + k6 (open-loop executors) + `grafana/otel-lgtm`. Every later layer
extends it rather than starting over.

| Shared experiment | Canonical owner | Reused (extend, don't rebuild) |
|---|---|---|
| Open-loop load harness | Block A (`lab-harness/`) | every layer |
| Toxiproxy fault injection | `02-network` t3 | 4.1, 5.3, 8.7 |
| Connection pool exhaustion | **`05-failure` t1** (Little's Law framing) | 2.2 (HTTP-client variant), 3.7 (DB sizing + PgBouncer), 6.5 (instrumenting it), 8.7 (slow-not-absent), 10.3 (async variant) |
| Little's Law / the knee | **`05-failure` t1** | 3.7, 10.3 |
| Retry storm, metastable collapse | **`05-failure` t3, t4** | 2.3, 3.7, 4.x — reference, do not rebuild |
| Retry budget + full jitter | **`05-failure` t3** | everywhere retries appear |
| Timeout budget / deadline propagation | **`05-failure` t2** | 2.3, 8.7 |
| Fan-out tail latency + hedging | **`05-failure` t6** | 2.1, 10.3 |
| Idempotency keys | **`04-distributed` t2** (atomicity) | 5.7 (as retry precondition), 8.5 (as a Hypothesis state machine) |
| Replica lag / read-your-writes | **`03-data` t8** (mechanism + three fixes) | 4.4 (as a consistency model), 6.7 (as an alert scenario) |
| CFS throttling / cgroup quota | **`01-machine/07` t2** | 3.7, 5.x, 6.5 |
| Coordinated omission | stated once in root `README.md` | flagged in 2, 5, 6, 10 |

Where a layer is listed as a reuser, its README should link to the owner and
teach only the *delta* — a different resource, a different failure signature,
a different fix. Not a second compose file.

## Version pins for the whole lab

**The pins live in [`mise.toml`](mise.toml), not in this table.** A markdown
table pins nothing; `mise install` pins everything. Run that first.

Every version below is what the ~530 programs in this repo were actually
compiled and run against, not what would be nice to have:

| | Pin | Note |
|---|---|---|
| Python | 3.13.5 | free-threading needs a separate `3.14t` — add it when you reach `01-machine/07/07`, and assert `sys._is_gil_enabled() is False` before believing any number |
| Node | 24.14.0 | |
| Go | **1.24.5** | **not** container-aware for `GOMAXPROCS` — that arrived in 1.25. `automaxprocs` is still required on this pin |
| Rust | 1.97.1 | |
| Java | temurin 21.0.2 | virtual threads available |
| C++ | Apple clang 21 | any C++17 compiler; two topics carry epoll/kqueue guards |
| k6 | 2.2.0 | |
| Postgres | 18.6 | |

### The drift is real, and it is in the Docker images

This section previously warned about version drift between two READMEs. The
warning was right and understated. Actual state of the compose files:

- **Postgres**: `17`, `18`, `18.6`, and `18.6-alpine` all in use.
- **k6**: `1.4.0`, `2.2.0`, and `latest` all in use.
- **`:latest`** on Grafana, Prometheus, otel-lgtm, and Toxiproxy.

Toolchains are now pinned; images are not. For a repo other people will
clone, `:latest` is the worst of the three states — it works today, breaks
silently later, and gives the person following your path a failure that has
nothing to do with the lesson. Pin every image to a digest or an exact tag
before publishing.

The lesson is worth keeping either way: two READMEs written the same week,
from the same research, disagreed about the major version of the tool every
experiment depends on. Check the releases API, not the blog posts.
