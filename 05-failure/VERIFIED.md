# Layer 5 — verification record

**Date:** 2026-08-19
**Verified by:** an independent pass. Every program below was compiled and
executed on this machine, using the command printed in its own topic README.
Nothing here was taken on trust from the code's comments or from the previous
agent's report.

**What this file records, and what it does not.** It records that this code
*executes* on this machine and that each program's output is consistent with
the claim its README makes. It does **not** record that anything was learned.
The `Predict, then record` tables in every topic README are still blank, and
they stay blank — they are the reader's exercise, and filling them in from
someone else's run is the one way to get no value out of this layer at all.

## The machine

| | |
|---|---|
| OS | macOS 27.0, Darwin 27.0.0, arm64 (Apple silicon) |
| Python | 3.13.5 |
| Node.js | v24.14.0 |
| Go | go1.24.5 darwin/arm64 |
| Rust | rustc 1.97.1 / cargo 1.97.1 |
| C++ | Apple clang 21.0.0 (`c++` is clang; `-pthread` accepted) |
| Java | OpenJDK 21.0.2 LTS (virtual threads available) |
| Postgres | running, `pg_isready` → accepting connections, `max_connections=100` |
| Docker | CLI 28.1.1 installed, **daemon DOWN** (`docker info` fails) |
| k6 | **not installed** |

No toolchain was installed, no daemon started, no `brew` run.

## Every program

`RAN` = compiled/ran unmodified. `FIXED-THEN-RAN` = a defect was found, fixed,
and the program re-run to completion afterwards. Times are wall-clock for a
serial run on an otherwise idle machine.

### Topic 1 — utilisation, Little's Law and the latency knee (6 of 6 languages)

| Program | Status | Time |
|---|---|---|
| `01-.../python/latency_knee.py` | **FIXED-THEN-RAN** | 3m17s |
| `01-.../nodejs/latency_knee.js` | RAN | 2m08s |
| `01-.../golang/latency_knee.go` | RAN | 2m06s |
| `01-.../rust/latency_knee` | RAN | 1m58s |
| `01-.../cpp/latency_knee.cpp` | RAN | 1m41s |
| `01-.../java/LatencyKnee.java` | RAN | 2m55s |

The Python program was the serious one. As written it **did not demonstrate the
knee at all** — the defect the topic's own "what would mean the experiment is
broken" section warns about, shipped inside the experiment:

* the generator slept `expovariate(rate)` between dispatches, so its own
  overhead accumulated into the schedule. The busier the server got, the
  slower the client sent: at `pool=10, rho=1.1` it offered 183 rps against a
  241 rps target. It had quietly become a closed-loop generator.
* `achieved_rate` was computed after draining every request, so it was
  identically equal to `offered_rate` in every row. The plateau the header
  told you to look for could not appear in that column under any load.
* service time `S` was measured on the first pass through a lazily-filled
  SQLAlchemy pool, so it included connection setup: 68.9ms instead of 42.9ms.
  Every `rho` below was then computed against a capacity 60% too low, and the
  whole `pool=5` sweep ran under-loaded with `pool wait` pinned at 0.1ms.

Fixed by precomputing absolute arrival times, timing latency from when each
request was **due** rather than when it was dispatched, counting `achieved`
only inside the step window, discarding a warm-up pass before measuring `S`,
and replacing `httpx` with a ~25-line asyncio HTTP client so the generator
stops competing with itself for the event loop. A `gen late` column now
reports the client's own dispatch lateness, so a run where the script was the
bottleneck is visible instead of plausible-looking. Also: pool sizes 10/20 →
5/10 (the old pair asked one asyncio loop for 476 rps), the deprecated
`@app.on_event` → `lifespan`, a failing handler returns a counted 503 instead
of raising (the old path produced 93KB of uvicorn tracebacks around the
table), and a `requirements.txt` now sits beside the code.

Captured after the fix, `pool_size=5`, capacity `L/S` = 116.5 rps — a knee
where there was none:

```
  rho  offered  achieved      p50       p99  pool p50  L (gauge)   lam x W   S/(1-r)  gen late
 0.20     20.2      20.2     47.3      51.3       0.2        0.9       0.9      53.7       2.4
 0.90    103.4     101.8     65.5     204.0      19.7        6.5       6.3     429.3       2.4
 1.10    123.4     109.0   1055.6    1566.8    1008.5       80.2     114.7       inf      58.8
```

The other five were correct as written. Spot-checks that mattered: the C++
`cpu %` column really is `getrusage(2)` deltas over wall×cores (Darwin has no
`/proc`, and the file says so); Node's event-loop-delay histogram is created
and enabled per step, not shared across the sweep, and it moves — flat at
~6ms through the pooled rows, 2334ms in the CPU-bound section, which is the
column the file claims noticed; Java's `rho=1.5` overload section really does
run the same pool under platform and virtual threads and shows the bounded
container shedding load.

### Topic 2 — timeout budgets and deadline propagation (6 of 6 languages)

| Program | Status | Time |
|---|---|---|
| `02-.../python/deadline_chain.py` | RAN | 54s |
| `02-.../nodejs/deadline_chain.js` | RAN | 54s |
| `02-.../golang/deadline_chain.go` | RAN | 55s |
| `02-.../rust/deadline_chain` | RAN | 1m09s |
| `02-.../cpp/deadline_chain.cpp` | RAN | 1m00s |
| `02-.../java/DeadlineChain.java` | **FIXED-THEN-RAN** | 58s |

Java's variant 1 is the *healthy baseline*, and it was reporting 93.0%
gateway success, 3.5 zombies/s and a 454ms C p99 — against 100% / 0.0 / ~45ms
in the other five languages. Cause: it was the first variant to run, so it
paid for class loading and interpreted execution against a 500ms budget. A
reader comparing rows would have read JIT warm-up as a symptom of the thing
the program is about. Fixed with a discarded 3s warm-up pass; row 1 now reads
100.0% / 0.0 / 45ms.

The Python file's mechanism was checked rather than assumed: `asyncio.shield`
on the query task is what makes cancelling the caller *not* stop the query,
which is the entire Python-specific finding. It is really there, and the
`Database` class is honestly documented as a model, not a driver.

### Topic 3 — retries that don't become the outage (5 of 5; no C++, per the README)

| Program | Status | Time |
|---|---|---|
| `03-.../python/retry_storm.py` | **FIXED-THEN-RAN** (prose) | 1m38s |
| `03-.../nodejs/retry_storm.js` | **FIXED-THEN-RAN** (code) | 1m38s |
| `03-.../golang/retry_storm.go` | **FIXED-THEN-RAN** (prose) | 1m38s |
| `03-.../rust/retry_storm` | **FIXED-THEN-RAN** (prose) | 1m39s |
| `03-.../java/RetryStorm.java` | RAN | 2m05s |

**The headline finding of this verification pass.** Four of the five files
told the reader to compare runs across languages and attributed the
difference to the runtime. Node's header said *"expect A and B to stay
elevated long after the cause is gone. Run the Go version of the same
experiment for a runtime that snaps back. The policy is identical; the
recovery is not."* Go's said the mirror image. The policy was not identical
and the runtime was not the cause.

Node's leaf modelled a pool checkout that **could not be cancelled**: the
attempt timeout was a `Promise.race` against a timer, so a timed-out attempt
abandoned its promise while the underlying call kept its place in the waiter
queue and took a slot when its turn came. Go's leaf did the opposite —
`select { case <-l.tokens: case <-ctx.Done(): }` — and released on
cancellation. Two different simulations, presented as two runtimes. This is
also why Node took 3m20s: the orphaned waiters had to drain at 200 rps.

Fixed by giving Node the mechanism its own topic README credits it with:
`AbortSignal.timeout` combined with the caller's signal via
`AbortSignal.any`, threaded through every hop exactly as Go threads `ctx`, and
a leaf whose `acquire(signal)` removes its waiter on abort. Node's variant A
went from `mean amp 20.06x / 0.0% success after` to `2.75x / 18.8%`, and its
runtime from 3m20s to 1m38s.

I then tested the obvious next hypothesis rather than asserting it. Go's inner
hops inherit each *attempt's* 300ms deadline (`context.WithTimeout(ctx, ...)`),
while Rust/Java/Node/Python pass the flat 1500ms request deadline down. I
changed Rust to nest the deadline the way Go does and re-ran: **no effect** —
still `3.04x / 1.2%`, with slightly more retries. Hypothesis disproved, change
reverted.

What the five runs actually show is that the chain is **bistable** at these
constants (150 rps offered, 200 rps of leaf capacity): Go recovered to 0.99x /
100%, Python's A recovered but its B did not, Rust and Java did not, Node
partially did. The prose in all four files now says that, points at
`mean amp from 16s onward` and `success after` as the two numbers to read,
promises no outcome, and points out that variant C — the retry budget — is the
one thing here that is *not* bistable. Java's variant E was checked against
its own text and matches: peak stays ~22x while retries rise (32,431 → 38,920),
because the request budget, not the retry config, is what caps you.

### Topic 4 — metastable failure (1 of 6 languages)

| Program | Status | Time |
|---|---|---|
| `04-.../python/metastable.py` | **FIXED-THEN-RAN** | 3m50s |
| `04-.../nodejs/metastable.js` | **NOT WRITTEN** | — |
| `04-.../golang/metastable.go` | **NOT WRITTEN** | — |
| `04-.../rust/metastable` | **NOT WRITTEN** | — |
| `04-.../cpp/metastable.cpp` | **NOT WRITTEN** | — |
| `04-.../java/Metastable.java` | **NOT WRITTEN** | — |

Scenario (d), "restart the app", was printing **negative** gauges — `pg` down
to -5 and `inflight` down to -270. The restart zeroed `server.inflight` and
re-ran `db.__init__()` synchronously, but the cancelled request tasks run
their `finally: self.in_use -= 1` on the *next* loop turn, decrementing the
freshly zeroed counters. Fixed by rebinding to a fresh `Database` and `Server`
— which is also the more faithful model of a restart — so the dying requests
unwind against the old objects. The conclusion (a restart changes nothing,
because the amplifier is in the clients and they did not restart) is unchanged
and is now supported by gauges that are not obviously broken.

Everything else in this file checked out. The escapes are measured, not
asserted, and the data contradicts the intuitive answer in the useful
direction: (c) shedding is the only sufficient escape, recovering goodput to
~128 rps of 180 offered while still rejecting the excess; (a) drop-and-ramp
walks straight back into the same state with the 8s ramp shipped here;
(b) retry budget and (d) restart both end at 0.0 goodput.

### Topics 5, 6, 7 — no code

| Topic | Languages its README specifies | Written |
|---|---|---|
| `05-load-shedding-backpressure-and-bulkheads` | 6 | 0 |
| `06-tail-latency-fanout-and-coordinated-omission` | 4 (Python, Node, Go, Rust) | 0 |
| `07-idempotency-and-degradation-decided-in-advance` | 3 (Python, Go, Node) | 0 |

Each has empty language directories and a complete README. Their `How to run`
sections now say so rather than printing commands for files that do not exist.

### The shared harness — BLOCKED and unwritten

`lab/README.md` specifies `lab/app/`, nine `lab/scripts/*.js` k6 scripts, six
`tools/*.py` plotters and a `docker-compose.yml` with six profiles. **None of
it exists.** It is also blocked twice over on this machine:

| Blocker | Reason | Unblock |
|---|---|---|
| `docker compose ...` | Docker CLI 28.1.1 is installed but the daemon is not running (`docker info` fails) | `open -a Docker` |
| `docker compose run --rm k6 ...` | `k6` is not installed | `brew install k6` |

The `docker compose` and `tools/plot_*.py` lines in topics 1–4 have been left
in place — they are the specification for work still owed — but each block is
now labelled as not-yet-built, so nobody types one expecting it to run.

## Changes made during verification

Code:

* `01-.../python/latency_knee.py` — open-loop generator rewritten
  (absolute schedule, latency from due-time, windowed `achieved`, `gen late`
  column); warm-up pass before `S` is measured; `httpx` → asyncio client;
  pool sizes 5/10; `lifespan`; 503 instead of an uncaught raise.
* `01-.../python/requirements.txt` — added.
* `02-.../java/DeadlineChain.java` — discarded JIT warm-up pass.
* `03-.../nodejs/retry_storm.js` — cancellable leaf acquire, `AbortSignal.any`
  threaded through every hop, abortable backoff sleeps.
* `03-.../{python,nodejs,golang,rust}` — the false cross-language causal
  claims replaced with what was measured.
* `04-.../python/metastable.py` — restart rebinds instead of zeroing counters
  underneath tasks that are still cancelling.

READMEs — only `How to run` sections and one experiment-step line touched.
No teaching content added, no layer index edited, no prediction table filled.

## Still owed

* Topic 4: Node, Go, Rust, C++, Java. `python/metastable.py` is a complete
  reference to port from — same constants, same five scenarios, same columns.
* Topic 5: all six languages.
* Topic 6: all four languages.
* Topic 7: all three languages.
* The whole `lab/` harness.

---

# Fill-in pass — verification record

**Date:** 2026-08-19
**Scope:** `05-failure/lab/` — the shared harness, written by another agent
after the pass recorded above. Independent: nothing below was taken from that
agent's report, and every claim it made was re-tested from the directory.

## The headline correction to the report under review

**The harness is not blocked. It was executed here, end to end.** The report
said `docker info` fails and `k6` is absent, so nothing could run. Checked
directly:

| Claim in the report | What was found |
|---|---|
| Docker daemon is down | `docker info` **succeeds**. Docker Desktop 28.1.1 / Compose v5.1.4 running |
| `k6` is not installed, unblock with `brew install k6` | **`k6` never needed installing.** The compose file runs it from the `grafana/k6` image, which is what `docker compose run --rm k6` starts. A local k6 is not part of the design |

So the whole stack was built and run: `postgres:18` pulled, the app image
built, and nine k6 scripts executed against it. What the earlier pass recorded
as blocked was blocked only by not being tried.

One genuine environment obstacle did exist, and it is not the harness's fault:
**host port 8000 was already held by an unrelated project's container**, so
`docker compose up` fails with `port is already allocated`. Everything below
was therefore run with a verification-only override republishing app/gateway/
service-b/service-c on 18000-18003. No repo file was changed for it; the
in-container ports and the compose network are untouched, so k6 — which talks
to `app:8000` over the compose network — saw exactly the shipped
configuration. The topic READMEs now name the free-port precondition.

## The machine

Same as above, with these corrections: Docker daemon **UP**; `k6` supplied by
the `grafana/k6` image. Load average was 4.5-5.5 throughout, with another
project's containers and a second k6 harness running: **absolute numbers from
this pass are worth less than usual, shapes are not.**

## Every part of the harness

`RAN` = executed unmodified. `FIXED-THEN-RAN` = a defect was found, fixed, and
re-run afterwards.

### The service — `lab/app/` (11 modules + Dockerfile + requirements.txt)

| Item | Status |
|---|---|
| image build (`python:3.13-slim`) | RAN |
| `app`, `gateway`, `service-b`, `service-c`, `backend` roles | RAN — all five answer `/healthz` with their role |
| all 21 contract env vars from `lab/README.md` | RAN — every one present in `GET /admin/config`; none missing |
| `POST /admin/config` live mutation, pool rebuild | RAN |
| real SQLAlchemy pool + `pg_sleep` inside a checked-out connection | RAN |
| `app/db.py`, `app/deadline.py`, `app/metrics.py` | **FIXED-THEN-RAN** — see topic 2 below |

### k6 scripts — `lab/scripts/` (9 + `lib/harness.js`)

| Script | Status |
|---|---|
| `01_ramp.js` | RAN |
| `02_chain_naive.js` | RAN |
| `02_chain_deadline.js` | RAN (its result was wrong until `app/` was fixed) |
| `03_retry_storm.js` | RAN (`naive`, `budget`) |
| `04_metastable.js` | RAN (compressed window — see caveat) |
| `05_shed.js` | **FIXED-THEN-RAN** (all four modes) |
| `06_fanout.js` | **FIXED-THEN-RAN** (hedge off and on) |
| `06_closed_loop.js` | **FIXED-THEN-RAN** (prose) |
| `07_idempotency.js` | RAN (`naive`, `correct`, `chaos`) |
| `lib/harness.js` | RAN — `pollCounters` diffing verified against `/admin/counters`, which does carry `now_ms` |

### Tools — `lab/tools/` (6 + `k6csv.py` + `make_fixtures.py`)

All six run to exit 0. `plot_knee.py` was additionally run against a **real**
k6 CSV, not only against fixtures. `zombie_report.py` was run inside the
`gateway` container as the topic README instructs. `make_fixtures.py`
regenerates all 18 fixtures; every one carries `# SYNTHETIC` on line 1 and
every plotter prints the warning when it reads one. **No fixture number was
copied anywhere.**

### Compose

`docker compose config` parses. All six profiles resolve to exactly the
service sets `lab/README.md` specifies (`shed` resolving to the default set is
correct, since its row *is* the default set).

## Defects found and fixed

### 1. Topic 2 was measuring the opposite of its own claim — the serious one

The propagated variant is supposed to *reduce* zombie completions. As shipped
it **produced more of them than the naive variant** (1686 vs 841 on a 40s run)
and reported `deadline_rejected = 0` — the number its own teardown prints as
the evidence. Two independent bugs, both found by a controlled four-request
test rather than by reading:

**(a) The budget was snapshotted before the queue wait that spends it.**
`statement_timeout` and the reject check were both evaluated on arrival and
never again. `SET LOCAL statement_timeout` is a duration measured from when
the statement *runs*, so a request that queued 400ms for a pool slot was
handed its full budget a second time and overshot the real deadline by exactly
the queue wait. Under load the queue wait is the largest term — which is the
only regime topic 2 is about, so the deadline was correct precisely when it did
not matter.

Reproduced minimally (pool of 1, 300ms of work, 500ms budget, four concurrent
requests). Before the fix, three of the four returned **HTTP 200** at 651ms,
936ms and 1237ms — correct, complete answers delivered long after the caller
had given up, produced by the variant whose job is to prevent them:

```
req1 http=200 t=0.348      req3 http=200 t=0.936
req2 http=200 t=0.652      req4 http=200 t=1.237
zombies=3  deadline_rejected=0
```

Fixed in `app/db.py`: `do_work()` now takes the deadline and re-reads it
*after* checkout — no budget left means the query is never issued (counted as
a new `deadline_abandoned`), and what remains becomes the statement timeout.
Threaded through all four call sites in `app/main.py`. Same test after:

```
req1 http=200 t=0.340      req3 http=504 t=0.465
req2 http=504 t=0.467      req4 http=504 t=0.465
zombies=0  deadline_rejected=0  deadline_abandoned=2
```

**(b) Every late response was counted as a zombie, including the refusals.**
`record_completion()` counted anything returning after the deadline — so
deadline rejections and statement-timeout cancellations, which are the fix
working, were scored as the disease. That is why the fixed variant reported
*more* zombies: it gives up out loud. `record_completion(deadline, did_work=)`
now counts only requests that actually completed their work, which is what its
own docstring always said a zombie was.

With both fixed, the topic's comparison finally exists (40s, 50 rps, C at
800ms against a 500ms budget, pool of 15):

```
naive       zombies 840   C pool in use 15/15   abandoned    0
propagated  zombies  15   C pool in use  0/15   abandoned 1131
```

Zombie completions 18.28/s → 0.33/s, and C's pool goes from fully saturated to
free. That is the topic, and it was not there before.

**Not fixed, and reported rather than papered over:** gateway success is 0.0%
in *both* variants, so the README's step 5 — "gateway success rate at a load
where the naive version collapsed" — cannot show a difference at the constants
the README itself specifies in step 1. C does 800ms of work against a 500ms
budget: no request that reaches C can succeed under any propagation policy.
The zombie and pool-utilisation columns carry the finding; the success column
is arithmetically pinned at zero. Changing 800/500 would be redesigning a
specified experiment, so it is left, named here.

### 2. `05_shed.js` split its priority tiers backwards

The comment reads *"70/30 checkout/search, because a scheme that only works
when the cheap traffic is the majority is not a scheme"*. The code gave
`/checkout` **0.3** and `/search` **0.7** — the cheap traffic as the majority,
i.e. exactly the weak test the comment refuses. Swapped to match. With
tier 0 now the majority the run is a real test of the policy, and it passes:
tier 0 holds 47-83% success across the overload steps while tier 3 goes to
~0%, which is the tier scheme doing its job.

### 3. `06_fanout.js` reported a working hedge budget as a broken one

`HEDGE_BUDGET_PCT` is spent per **backend call** — hedging is decided once per
outstanding call and a request makes K of them. The teardown divided the
hedge count by the **request** count and printed it as "% of requests", so a
correctly enforced 5% budget printed as **36.3%** at K=10. That is one of the
topic README's own "the experiment is broken" criteria arriving purely as an
artefact of arithmetic. Now printed against both denominators:

```
5.01% of backend calls - this is the number HEDGE_BUDGET_PCT caps
50.1% of requests - the topic's hedge_rate column; it is ~K x the line above
```

### 4. `06_fanout.js` had no guard against the gateway being the bottleneck

Backend calls are `RATE x K`, so the shipped `RATE=50` sweeping K to 50 asks
one uvicorn process for 2500 outbound calls/s. At K=10 here the gateway
completed 33 rps of 50 offered, p99 44s — that p99 is the gateway's own queue,
and it grows with K exactly as tail amplification does. The topic README lists
this as a way the experiment breaks; nothing detected it. A teardown check now
prints offered vs completed and warns when they disagree. Verified to
discriminate: silent at `RATE=12` (12.0 completed of 12), fires at `RATE=50`.
No threshold number was invented — the comparison is the run's own two figures.

### 5. `06_closed_loop.js` told the reader to read a number that says the opposite

Its header said *"compare the ACHIEVED request counts: the closed loop sent
fewer, which is exactly the omission."* Measured: the closed run completed
**754** and the open run **354**. Once the open run saturates, its completions
collapse (still in flight, or dropped by k6) and it finishes *fewer* while
having offered several times more. The prose now points at offered-rate vs
achieved-rate, which survives saturation, and warns off the counts. The
`VUS=50` default's own stated rationale ("VUS/latency ≈ the open run's rate")
does not hold either — an unloaded `/fanout?k=10` is ~80ms here, so 50 VUs
offer ~600 rps against the open run's 50 — so `setup()` now tells the reader
to time one request and calibrate VUS instead of trusting the default.

### 6. Seven topic READMEs asserted a blocker that does not exist

Every `How to run` block said the harness "has not been executed here" because
Docker was down and `k6` absent, and told the reader to `brew install k6`.
Docker is up, and `k6` is not installed at all in this design — it runs from
the `grafana/k6` image. All seven now say the harness was executed, say that
k6 needs no install, and name the real precondition (Docker running, host
ports 8000-8003 free). Topic 6's block additionally explains when to lower
`RATE` and why the hedge figure has two denominators.

## The assignment's specific question: does topic 6 really produce two histograms?

**Yes — the topic is not broken.** Same gateway, same K=10, same 40s:

```
open   (constant-arrival-rate, 50 rps)  p50 2946ms   p99 44183ms
closed (ramping-vus, 50 VUs)            p50 2266ms   p99  6907ms
```

A 6.4x difference in p99 on one service, and the closed-loop number is the
flattering one — for the right reason: the open generator kept offering 50 rps
into something doing ~19, so the waiting is in its histogram, while the closed
generator throttled itself to capacity and never queued. The comparison works.
What did not work was the guidance around it, fixed in items 3-5 above.

## What was not established

- **Topic 4's metastable state.** `04_metastable.js` runs and the FLUSHALL
  trigger fires, but the experiment is a 10-minute run with a 5-minute
  hands-off window and it was compressed to 90s here. Goodput recovered and
  the cache hit rate climbed back; that is **not** evidence either way about
  metastability, only that the script executes. Run it at its real duration.
- **Topic 3 past t=100s.** Both variants ran at 70s, so `plot_amplification`'s
  `t=100s / t=200s / t=280s` columns are empty — correct behaviour, not a
  result. What did show, on the real stack: peak amplification 5.18x naive vs
  1.10x with the retry budget.
- **Counter bleed between back-to-back runs.** Running the two topic 2
  variants consecutively leaves the first run's backlog draining into the
  second's freshly-reset counters (C reported more `failed` than `received`).
  `zombie_report` already warns that counters are per process; the honest
  procedure is to let the backlog drain between variants.
- **Prometheus / Grafana** (`--profile observability`) were not started. Not
  needed by any topic; the README calls them optional.

## Still owed

Unchanged from the pass above except where concurrent work has landed:
topic 4 and topic 5 now have all six language directories populated (written
by another agent during this pass; **not** verified here — outside this
assignment's scope). Topic 6 has 1 of the 4 languages its README specifies,
topic 7 has 0 of 3, and both READMEs still say so.

## Changes made during this pass

* `lab/app/db.py` — `do_work()` re-reads the deadline after pool checkout;
  abandons rather than starting work it cannot finish; derives
  `statement_timeout` from the budget that is actually left.
* `lab/app/main.py` — deadline threaded into all four `do_work` call sites;
  zombie counted only for work that completed; `deadline_abandoned` exposed
  on `/admin/zombies`.
* `lab/app/deadline.py` — `record_completion(deadline, did_work=)`.
* `lab/app/metrics.py` — `deadline_abandoned` counter.
* `lab/tools/zombie_report.py` — `abandoned` column and its explanation.
* `lab/scripts/05_shed.js` — priority tier split corrected to 70/30.
* `lab/scripts/06_fanout.js` — hedge budget reported per backend call;
  gateway-saturation guard.
* `lab/scripts/06_closed_loop.js` — corrected comparison guidance, VUS
  calibration note.
* Seven topic `README.md` `How to run` blocks — stale "blocked" claim
  replaced with the real preconditions; topic 6 gained a `RATE`/hedge note.

No prediction table was touched; all seven remain blank. (Topic 6's
`predicted tail prob` column is the README's own derived `1 − 0.99^K`
arithmetic, deliberately pre-filled, and was left alone.)

---

# Fill-in pass — topics 4 and 5, the twelve standalone programs

**Date:** 2026-08-19
**Scope:** `04-metastable-failure/` and
`05-load-shedding-backpressure-and-bulkheads/` — the eleven programs written
by another agent to close the gaps recorded in the first pass above, plus the
pre-existing `04-.../python/metastable.py` they were ported from. The `lab/`
harness is a separate pass and is recorded in the section above this one.

**Independent.** Nothing below was taken from the authoring agent's report.
Every program was compiled and executed here with the command printed in its
own topic README, and every claim in this section is a thing that happened on
this machine. The report's headline — "both topics are complete and every
program has been compiled and run" — is true about compiling and running. It
was not true that every program tested the claim its README makes; three did
not, and are fixed below.

## The machine

Unchanged from the first pass, with two differences worth recording because
the topic READMEs mention both: `docker info` now **succeeds** (daemon up),
and `k6` is still **not installed** — which is not a blocker, since the topic
READMEs invoke it as `docker compose run --rm k6`, out of the `grafana/k6`
image. Nothing in this section needed either: all twelve programs are
single-process and run natively on macOS 27.0 / arm64 with no container.

## Topic 4 — metastable failure (6 of 6 languages)

| Program | Status | Time |
|---|---|---|
| `04-.../python/metastable.py` | **FIXED-THEN-RAN** | 3m51s |
| `04-.../nodejs/metastable.js` | **FIXED-THEN-RAN** | 3m52s |
| `04-.../golang/metastable.go` | **RAN** | 3m52s |
| `04-.../rust/metastable` | **RAN** | 3m52s |
| `04-.../cpp/metastable.cpp` | **FIXED-THEN-RAN** | 3m53s |
| `04-.../java/Metastable.java` | **FIXED-THEN-RAN** | 3m50s |

All six now agree on the shape the topic is about, which they did not before:
scenario 0 collapses to **0.0 rps of goodput and stays there** for the whole
24 seconds after a trigger that was over in one millisecond, and (c) load
shedding is the only escape that recovers anything at all. Every scenario's
pre-escape rows were checked against scenario 0's — they match to within
sampling noise in all six, which is how per-scenario state resetting was
verified rather than assumed.

### C++ — the timer thread was dead in four scenarios out of five

`TimerWheel::stop()` set `stopping_ = true` and joined; `start()` did not
reset it. So the second and every later `start()` launched a thread that fell
straight out of its own `while (!stopping_)` loop, and **scenarios a, b, c and
d ran with no client deadline at all** — infinitely patient callers, no
timeouts, therefore no retries and no abandoned work. The tell was in the
output and is worth naming because it is how the bug was found rather than
guessed: scenario 0 reported `thruput` ~555/s (180 rps x 3 attempts) while
scenarios a/b/d reported ~30/s, the bare pool completion rate, at the same
timestamps under identical settings; `zombie completions` read 690 in scenario
0 and **0** in every other scenario, which is impossible in a run whose
goodput is zero for forty seconds.

Fixed by clearing `stopping_` and `pending_` in `start()`. Re-run: scenarios
a, b and d now report 1279 / 1277 / 1267 zombie completions and the same
~555/s attempt rate as scenario 0. The verdict is unchanged — only (c)
recovers, to 61% of pre-trigger goodput — but it is now measured against a
client that gives up, which is the entire premise of the experiment.

### Java — a non-fair semaphore was quietly running the wrong experiment

`new Semaphore(POOL_SIZE)` is **non-fair**: a thread arriving as a permit is
released may barge ahead of threads already queued. Under sustained overload
that is accidental LIFO, the newest request is the one whose caller has not
given up, so some queries beat their deadline, some cache fills land, and the
system climbs back out. Measured, before the fix: scenario 0's `hit%` rose
1.9% → 20.5% and goodput rose 30 → 101 rps over the observation window,
finishing at **99.7 rps (55% of offered)** where the other five finish at 0.0.

That is a real finding and a broken demonstration at the same time, and the
program was printing both at once — its own summary paragraph said "the system
is still at zero goodput half a minute later" directly beneath a table showing
goodput climbing. The topic README also names this exact outcome under *what
would mean the experiment is broken*: "it recovers ... that is a slow drain,
not metastability".

Fixed by building the pool `new Semaphore(POOL_SIZE, /* fair = */ true)` —
FIFO, matching `asyncio.Semaphore`, Go's buffered channel and tokio's
`Semaphore`, which is what the other five files use. Re-run: scenario 0
finishes at **0.0 rps**, a/b/d at 0.0, (c) at 115.1 rps (61%). The finding is
kept, inverted, in the file's header and next to the constructor: deleting the
`, true` is a one-character experiment that brings the barging recovery back,
and it is now the clearest demonstration of topic 5's adaptive LIFO in the
layer — arriving as an accident of a default.

### Node.js — the comparison line printed the wrong end of the run

`const baseline = results[0][1]` reads scenario 0's **pre-trigger** goodput,
so the footer read "scenario 0 finished at 182.2 rps of goodput, for
comparison" under a table whose scenario 0 row said `0.0`. Indices are
`[title, before, after]`; corrected to `results[0][2]`. Go, Rust, C++ and Java
all already printed `after` here. Re-run: "scenario 0 finished at 0.0 rps".

### Python — a verdict block that was asserted, not computed

The topic README says "the verdict lines at the end of each run are computed
from that run, not asserted by the program". True of the five new files, which
all print an `Escapes, judged against THIS run` block from a `verdict()`
helper. Not true of the reference file, which printed a fixed paragraph
stating that (a) does not recover and (c) is sufficient — accurate on the runs
seen here, and a hardcoded claim about a result nobody had measured yet on the
reader's machine. Given the same `verdict()` helper and the same computed
block as the other five; the explanatory prose about what each escape *touches*
is kept, because that part is mechanism rather than outcome.

## Topic 5 — load shedding, backpressure and bulkheads (6 of 6 languages)

| Program | Status | Time |
|---|---|---|
| `05-.../python/shedder.py` | **RAN** | 1m31s |
| `05-.../nodejs/shedder.js` | **RAN** | 1m44s |
| `05-.../golang/shedder.go` | **RAN** | 1m32s |
| `05-.../rust/shedder` | **RAN** | 1m32s |
| `05-.../cpp/shedder.cpp` | **RAN** | 1m35s |
| `05-.../java/Shedder.java` | **RAN** | 1m32s |

All six ran unmodified, all seven scenarios each, and each was read against
the README's numbered experiment rather than against its own comments:

* **Step 3's claim** — p99 of *accepted* stays roughly flat past 100% offered
  while rejections absorb the excess — holds in all six. `none` at ρ=1.3
  reaches a p99 of 2860-4153ms with **zero** rejections; `static` at the same
  offered load holds p99 to 81-142ms, which is at or below the ρ=0.8 baseline
  in every runtime, while rejecting 22-33%.
* **Step 4's claim** — tier 0 unaffected while tier 3 absorbs the rejections —
  holds: tier-0 success is 100% under `priority` in five runtimes and 98% in
  C++, against 7-16% under `none` at the same load.
* **Step 5's claim** is the one most likely to be faked and is not: the
  gradient controller starts at `ADAPT_START = 10`, not at the hand-measured
  12, and converges to 11.7-12.5 before the perturbation — within ~4% of the
  number measured by hand. When service time triples mid-run it re-converges
  downward to 7.2-9.4, which by Little's law is the correct limit for a
  backend whose capacity just fell to 66 rps. The `adaptive` row's lower
  summary goodput is that second regime averaged in, not a worse controller.
* **Step 6's claim** holds in all six: the same eight servers split 6+2 give
  checkout 119-123 rps of goodput at a p99 of 96-262ms, against 17-64 rps at
  1697-3015ms when `/report` shares the pool.
* **Node's `lag99` column** does what the README's caveat says it should: it
  sits at 6.5-7.0ms through the ρ=1.3 collapse while `inflight` reaches 660
  and p99 reaches 3256ms. Event loop lag measuring nothing while the service
  is destroyed is the point, and it is measured here rather than asserted.

C++ is the one runtime whose `reject_ms` reads 0.0 under `static` where the
others read ~50: it rejects on the queue's measured head-of-line age instead
of joining the queue and timing out of it. Both are the CoDel rule; the C++
one is the cheaper rejection, which is what that column exists to expose. Not
a defect, and it matches what the README's per-language paragraph promises.

## Also checked, outside this assignment's scope

`06-.../python/fanout.py` — **RAN**, 3m17s. Checked specifically for the
failure mode that would make topic 6 pointless: the open-model and closed-loop
generators do produce different histograms against the same server. Open model
p50 230.9ms / p99 670.4ms at 152 achieved rps; closed loop, 15 VUs calibrated
by Little's Law from the healthy latency, p50 93.4ms / p99 531.4ms at 125
achieved rps; omission-corrected p99 6117.1ms. The generator's own lateness
p99 is 0.99ms, so the open model is not itself coordinating omission. Topic 6
is not broken. Its README still says "**Not written yet — all four**" above
the standalone block, which is now wrong for Python — left alone, because
topic 6 has concurrent work in flight and is not this pass's scope.

## Changes made during this pass

* `04-.../cpp/metastable.cpp` — `TimerWheel::start()` resets `stopping_` and
  clears `pending_`, so scenarios after the first have a working client
  deadline.
* `04-.../java/Metastable.java` — pool semaphore made fair (FIFO); header
  hazard 3, the `WHAT TO LOOK FOR` list and the `Database` javadoc rewritten
  to make the non-fair barging the reader's one-character experiment rather
  than the shipped default. Duplicate list numbering fixed.
* `04-.../nodejs/metastable.js` — comparison footer reads scenario 0's final
  goodput instead of its pre-trigger goodput.
* `04-.../python/metastable.py` — added the computed `verdict()` block the
  other five have and the README promises; removed the asserted outcome prose
  it replaces.

No README was edited: both topics' `How to run` blocks already matched the
files, and all twelve commands run exactly as printed. No `Predict, then
record` table was touched — both remain blank, and were confirmed blank after
this pass.

---

# Fill-in pass — topics 6 and 7, the seven standalone programs

**Date:** 2026-08-19
**Scope:** `06-tail-latency-fanout-and-coordinated-omission/` (four languages)
and `07-idempotency-and-degradation-decided-in-advance/` (three languages) —
the six programs written by another agent to close the gaps recorded above,
plus the pre-existing `06-.../python/fanout.py` the other three were ported
from and which that agent also amended. The `lab/` harness is a separate pass,
recorded further up this file.

**Independent.** Nothing below was taken from the authoring agent's report.
Every program was compiled and executed here with the command printed in its
own topic README, one at a time on an otherwise idle machine — serially and
not in parallel, because four of the seven are latency measurements and
concurrent runs would have measured this verification instead of the
experiment. Every number quoted below is from the run recorded here.

## The machine

Unchanged from the passes above. `docker info` succeeds (daemon up) and `k6`
is still not installed; neither mattered, because all seven programs are
single-process, need no container and no network. Local Postgres was up
(`pg_isready` → `/tmp:5432 - accepting connections`) and is what topic 7 used.

## Topic 6 — tail latency, fan-out and coordinated omission (4 of 4 languages)

The README says four languages, not six, and states its reason. Four are
present. Coverage is complete.

| Program | Command (exactly as the README prints it) | Result | Wall |
|---|---|---|---|
| `06-.../python/fanout.py` | `python3 python/fanout.py` | **RAN** | 3m18s |
| `06-.../nodejs/fanout.js` | `node nodejs/fanout.js` | **RAN** | 3m11s |
| `06-.../golang/fanout.go` | `cd golang && go run fanout.go` | **RAN** | 3m16s |
| `06-.../rust/fanout` | `cd rust/fanout && cargo run --release` | **RAN** | 3m24s |

No `go.mod` is needed for `fanout.go` (standard library only, and `go run` on
a single file outside a module is fine); `rust/fanout/Cargo.toml` and
`Cargo.lock` are present and `tokio` was already in the cargo cache. Nothing
hung, nothing needed shrinking, and no source change was required to make any
of the four run. Three minutes is what the README promises and roughly what
each took.

### The assignment's question: do the two generators produce two histograms?

Yes, in all four. Phase C runs the identical backend set (K=10, four workers
each, log-normal service) at the same nominal rate, sizing the VU pool from
Little's Law on the *healthy* latency, and measures it twice:

| | open (arrival schedule) | closed (VUs), as reported | closed, omission-corrected |
|---|---|---|---|
| Python | p50 246.0 / p99 699.5 ms, 153/s | p50 93.1 / p99 532.0 ms, 123/s (15 VUs) | p50 2144.4 / p99 6688.7 ms |
| Node | p50 241.4 / p99 618.7 ms, 154/s | p50 91.8 / p99 526.2 ms, 123/s (15 VUs) | p50 2030.9 / p99 6992.9 ms |
| Go | p50 284.1 / p99 633.4 ms, 156/s | p50 101.5 / p99 503.8 ms, 126/s (16 VUs) | p50 2355.4 / p99 5850.9 ms |
| Rust | p50 354.5 / p99 794.3 ms, 151/s | p50 111.8 / p99 583.0 ms, 119/s (17 VUs) | p50 2634.0 / p99 6213.4 ms |

The printed histograms differ in shape as well as in the summary numbers: the
open model's mode sits in the 160-320ms bucket in Python, Node and Go and in
320-640ms in Rust, while every closed-loop histogram peaks in 80-160ms. The
closed loop also completes fewer requests per second than it was nominally
configured for (119-126 against 151-156), which is the omission itself
showing up as arithmetic. The open generator is not the thing omitting:
its own lateness p99 is 0.90-2.86 ms across the four.

**Topic 6 is not broken.** This was the failure mode worth ruling out and it
is ruled out by measurement, in four runtimes.

### Phase A tests its claim

`measured` tracks `predicted` = `1 − 0.99^K` without being fitted to it, in
all four and for both distributions. Python, log-normal: 0.5 / 1.5 / 5.1 /
10.4 / 18.5 / 40.7 % against 1.0 / 2.0 / 4.9 / 9.6 / 18.2 / 39.5 %. Go:
1.1 / 2.5 / 5.6 / 10.4 / 18.3 / 39.9 %. Node: 0.9 / 1.4 / 4.6 / 10.0 / 18.6 /
39.0 %. Rust: 1.1 / 2.4 / 6.4 / 10.9 / 20.5 / 41.6 %. The bimodal rows land in
the same place while their `e2e_p50` stays near 10ms as K grows and the
log-normal one climbs to ~170ms — same p50, same p99, same tail probability,
different shape, which is what that pair of blocks exists to show.

### Phase B's `svc_ms/req` column does separate the two hedge rows

This was the authoring agent's claimed fix to the pre-existing Python file,
and it holds in every language and every cell — the cancelled/dropped/aborted
row always pays less backend service time per request than the leaked one,
while `be_rps` and `+load` are identical between them to within 0.1%:

| dist / K | cancelled or dropped | leaked or not honoured | no hedge |
|---|---|---|---|
| Python lognormal 10 | 202.9 | 240.4 | 230.8 |
| Node lognormal 10 | 202.8 | 239.0 | 230.0 |
| Go lognormal 10 | 204.6 | 241.5 | 230.8 |
| Rust lognormal 10 | 218.9 | 256.1 | 244.8 |

`+load` sits at 4.3-5.1% of backend calls in Python, Node and Go, which is the
5% bucket being enforced rather than described. Rust's bimodal cells hedge
less (1.9-3.3%) because its *measured* p95 came out at 14.3ms against 13.1ms
elsewhere and the hedge fires off the measured number — the mechanism working,
not a discrepancy.

One cell reads oddly and is worth knowing about before it is mistaken for a
finding: Node's bimodal K=10 pair shows `abort honoured` p99 212.0ms against
`abort NOT honoured` 29.7ms. Both are far below the 300.7ms no-hedge row, the
backends have 512 workers and therefore no capacity pressure, so nothing about
honouring the abort can change latency there — the two are noise around the
same value, and the file already tells the reader that `svc_ms/req` (107.8 vs
131.1 in that same pair) is the column that separates them. Not a defect.

## Topic 7 — idempotency and degradation decided in advance (3 of 3 languages)

The README says three languages, not six, and states its reason. Three are
present. Coverage is complete.

| Program | Command (exactly as the README prints it) | Result | Wall |
|---|---|---|---|
| `07-.../python/idempotency.py` | `python3 python/idempotency.py` | **RAN** | 11.7s |
| `07-.../golang/idempotency.go` | `cd golang && go run idempotency.go` | **RAN** | 11.7s |
| `07-.../nodejs/idempotency.js` | `cd nodejs && npm install && node idempotency.js` | **RAN** | 7.0s |

`npm install` was run and reported `up to date` against the committed
`node_modules`; `go.mod`/`go.sum` resolve pgx v5.7.6 from the local module
cache; the Python requirements (SQLAlchemy 2.0.39 + psycopg 3) were already
importable. All three create `failure_lab` if absent and drop/recreate their
own tables, so every count printed is that run's own.

### Every row does what the README says it does

| Row | Python | Go | Node |
|---|---|---|---|
| naive / sequential | 1 charge | 1 | 1 |
| naive / 50 concurrent | **20 charges** | **17** | **8** |
| naive / 50 concurrent / pool=1 | 1 charge | 1 | 1 |
| correct / claim + execute | 1 charge, 49× 409, loser_p99 42.8ms | 1, 49, 10.9ms | 1, 49, 39.2ms |
| correct / single transaction | 1 charge, 0× 409, loser_p99 62.6ms | 1, 0, 30.9ms | 1, 0, 40.7ms |
| correct + retries + lost responses | 20 charges / 20 keys, PASS | PASS | PASS |
| correct + crash, TTL not expired | 0 charges, 1 orphaned, 5× 409 | same | same |
| correct + crash, after TTL expiry | 1 charge, 0 orphaned | same | same |
| fingerprint: same key, other body | 422, 1 charge, PASS | PASS | PASS |

The two comparisons the README singles out both land. `naive / 50 concurrent`
against the `pool=1` row directly beneath it is a bug and its concealment by a
*smaller* limit, in all three runtimes. `claim + execute` against `single
transaction` differs exactly where it should: 49 immediate 409s versus 0, with
the single-transaction losers waiting instead — `loser_p99` is higher in the
waiting variant in all three (62.6 vs 42.8, 30.9 vs 10.9, 40.7 vs 39.2 ms).

### The per-language driver sections are real, not narrated

* **Python** reproduces the poisoned session and prints what this stack
  actually raised: `IntegrityError` wrapping `UniqueViolation` on the insert,
  then `InternalError: (psycopg.errors.InFailedSqlTransaction) current
  transaction is aborted…` on the read that follows without a rollback. That
  is the file's own caveat coming true — the message names the previous
  statement, not the problem — and it is *not* the `PendingRollbackError` the
  folklore (and this topic's own prose section) names. The file says to read
  which one you got rather than assume; it is right to.
* **Go** fires both unique constraints and tells them apart by name:
  `23505 on idempotency_keys_pkey` → replay, `23505 on
  charges_merchant_ref_uniq` → 409. Same SQLSTATE, two different correct
  answers, separated by `*pgconn.PgError.ConstraintName`.
* **Node** runs one `withRetry` wrapper over two identical inserts and gets 2
  rows written without the unique index and 1 with it. Its rollback-hygiene
  section prints three real outcomes: (a) no rollback → `next borrower FAILED:
  current transaction is aborted…`, (b) un-awaited rollback → survives, and
  node-postgres emits the `DeprecationWarning` about queueing on a busy client
  that the file predicts, (c) awaited → survives.

The degradation section behaves in all three: success 0.0% with the sick tier-2
dependency in the path, 100% after the two flags are flipped with no restart,
and goodput 15.4→345.8 /s (Python), 16.3→401.5 (Go), 16.3→387.1 (Node). The
last line in each is the baked-in constant that is in the matrix, has an owner,
and is still not a kill switch.

## Defects found

None that stop a program running or that make one fail to test its claim.
Seven of seven are `RAN`; nothing is `FIXED-THEN-RAN` and nothing is `BLOCKED`.

The one thing that was wrong was a set of stated runtimes, corrected below.
Nothing else in either topic's prose was found to disagree with what the code
does on this machine.

## Changes made during this pass

Five one-line corrections, all to durations that did not match the measured
wall-clock:

* `06-.../python/fanout.py` — header said "roughly two and a half minutes";
  it takes 3m18s here, the same as its three siblings. Now "roughly three
  minutes", matching them and the README.
* `07-.../python/idempotency.py`, `07-.../golang/idempotency.go`,
  `07-.../nodejs/idempotency.js` — headers said "roughly half a minute";
  measured 11.7s, 11.7s and 7.0s. Now "about ten seconds".
* `07-.../README.md` — the same claim in the `How to run` block, "finishes in
  about half a minute" → "finishes in about ten seconds". This is the only
  README edit in this pass and it is inside the `How to run` block.

No command in either `How to run` block needed changing: all seven run exactly
as printed. No `Predict, then record` table was touched — both remain blank,
and were confirmed blank after this pass.

---

# Fill-in pass — topics 4 and 5, second independent pass

**Date:** 2026-08-19
**Scope:** the twelve standalone programs in `04-metastable-failure/` and
`05-load-shedding-backpressure-and-bulkheads/`, re-verified from the
directory by a different agent than the one that wrote them and a different
one than the pass recorded immediately above. Nothing here was taken from
either report: every program was compiled and executed with the command its
own topic README prints, and every number below is from a run on this
machine.

**What the report under review got right, and what it did not.** "Both
topics are complete and every program has been compiled and run" is true of
compiling and running — all twelve build clean (`-Xlint:all` and `go vet`
silent, no compiler warnings) and all twelve finish. It is not true that
every program demonstrated the thing its README asks it to demonstrate. Two
defects were found by reading each program against its README's numbered
experiment and then measuring, not by reading the code:

1. **Topic 5 step 6 did not reproduce in Node** — checkout survived the
   shared pool it is supposed to die in. Root cause found and fixed in all
   six.
2. **Topic 5 step 5's re-convergence was never printed** in any of the six —
   the run ended during the dip. Fixed in all six.

A third class, asserted outcomes printed above computed tables, was found in
topic 4 and fixed in all six.

## The machine

macOS 27.0 / arm64, Apple M1. `python3` 3.13.5, `node` 24.14.0, `go`,
`rustc` 1.97.1, Apple clang 21, JDK 21.0.2. All twelve programs are
single-process and native; no container, no `k6`, no Docker involved in
anything below. Runs were serialised — one program at a time, nothing else of
mine running — because every one of them is a queueing measurement.

## Topic 4 — metastable failure (6 of 6 languages)

| Program | Status | Time |
|---|---|---|
| `04-.../python/metastable.py` | **FIXED-THEN-RAN** | 3m50s |
| `04-.../nodejs/metastable.js` | **FIXED-THEN-RAN** | 3m51s |
| `04-.../golang/metastable.go` | **FIXED-THEN-RAN** | 3m52s |
| `04-.../rust/metastable` | **FIXED-THEN-RAN** | 3m52s |
| `04-.../cpp/metastable.cpp` | **FIXED-THEN-RAN** | 3m53s |
| `04-.../java/Metastable.java` | **FIXED-THEN-RAN** | 3m51s |

The topic reproduces in all six, and the six agree. Scenario 0 — trigger
removed, offered load never touched — goes to **0.0 rps of goodput and stays
there** for the whole 24-second observation window, while `thruput` holds at
473-592 attempts/s and `hit%` sits at 0.0. Pre-trigger goodput 165-188 rps.
Escapes (a), (b) and (d) all end at 0.0 rps in all six; (c), the shedder, is
the only one that recovers anything:

```
            goodput before   after   verdict computed by the program
python           173.0       121.6   SUFFICIENT   (70%)
node             181.5       128.0   SUFFICIENT   (70%)
go               165.2       133.3   SUFFICIENT   (80%)
rust             184.3       127.7   partial      (69%)
cpp              186.9       107.9   partial      (58%)
java             187.6       121.0   partial      (65%)
```

The `partial` / `SUFFICIENT` split is the program's own 70% threshold landing
either side of the same result, not a disagreement between runtimes; the
percentage is printed next to the word, which is what makes that readable.

The per-runtime extra column matches what the topic README promises, in all
six, and C++'s is the one worth the price of the file: `qlen` climbs 2 → 690
→ 10,896 and `oldest_ms` 10 → 1,659 → 20,234 over the run, without bound,
because C++ is the only one of the six with no cancellation. Node's `lag99`
holds at 6.5-8.1ms through the entire collapse.

### Defects fixed

**1. An asserted outcome printed above a computed table — all six.** Every
file printed, unconditionally, *"and the system is still at zero goodput half
a minute later"* immediately above the `Escapes, judged against THIS run`
block. That sentence is a measurement, and the pass recorded above this one
proves it can be false: Java printed it while its own table showed goodput
climbing to 99.7 rps. Fixing Java's semaphore fixed Java; it left the
sentence able to lie on anyone else's machine. All six now print scenario 0's
own final goodput in that sentence and point the reader at the README's *what
would mean the experiment is broken* if it is not near zero.

**2. `inflight` does not climb without bound — python and node.** Both files'
`WHAT TO LOOK FOR` item 3 said to watch it do exactly that. Measured, in
scenario 0: python 4 → 277 and then flat at 245-286 for twenty-two seconds;
node 6 → 247 and then flat at 221-294. The plateau is arithmetic — offered
rate x per-request wall time, with the client giving up after `ATTEMPTS`
attempts — and nothing about the server bounds it. Both items now describe
the plateau, say what actually holds the number down, and say what to change
to make it climb. Go's and Rust's equivalents were already worded carefully
("nothing keeps it from being a queue", "the number still climbs") and were
left alone.

**3. `python/metastable.py` header, two wrong numbers.** It called the
amplification *"a 20x rise in database load from the miss rate going 10% ->
100%"*; 10% to 100% is 10x, which is what the same file's closing block
computes and prints. Corrected. It also estimated its own runtime at "two and
a half minutes" against a measured 3m50s and the README's "about four
minutes". Corrected.

**4. `nodejs/metastable.js` header** claimed its constants were identical to
`python/metastable.js`. There is no such file; `.js` → `.py`. (The constants
themselves were checked one by one across all six and do match.)

**5. `java/Metastable.java` hazard 3** told the reader the barging-semaphore
finding was a "ONE CHARACTER experiment: delete the `, true`". The code reads
`new Semaphore(POOL_SIZE, /* fair = */ true)`, so following that instruction
literally leaves `new Semaphore(POOL_SIZE, /* fair = */ )` — a syntax error.
The instruction now names both forms.

## Topic 5 — load shedding, backpressure and bulkheads (6 of 6 languages)

| Program | Status | Time |
|---|---|---|
| `05-.../python/shedder.py` | **FIXED-THEN-RAN** | 2m28s |
| `05-.../nodejs/shedder.js` | **FIXED-THEN-RAN** | 2m52s |
| `05-.../golang/shedder.go` | **FIXED-THEN-RAN** | 2m28s |
| `05-.../rust/shedder` | **FIXED-THEN-RAN** | 2m29s |
| `05-.../cpp/shedder.cpp` | **FIXED-THEN-RAN** | 2m31s |
| `05-.../java/Shedder.java` | **FIXED-THEN-RAN** | 2m28s |

After the two fixes below, all four numbered claims hold in all six:

* **Step 3** — p99 of *accepted* stays flat past 100% offered while
  rejections absorb the excess. `none` at ρ=1.3 reaches p99 4967-6700ms with
  **zero** rejections and 11-21 rps of goodput; `static` at the same offered
  load holds p99 to 80-146ms — 1.04-1.39x its own ρ=0.8 baseline in five
  runtimes and 0.58x it in C++, against 48-61x for `none` — with 175-198 rps
  of goodput while rejecting 24-32%.
* **Step 4** — tier 0 unaffected, tier 3 absorbs the rejections. Tier-0
  success under `priority` is 100% in five runtimes and 98% in C++, against
  5-9% under `none` at the same load.
* **Step 5** — the gradient controller starts at `ADAPT_START = 10`, not at
  the hand-measured 12, and converges to 10.9-12.6 before the perturbation.
* **Step 6** — the same eight servers split 6+2 give checkout 119-121 rps at
  a tier-0 p99 of 103-223ms, against 4-34 rps at 2949-5610ms sharing.

### Defect 1 — the `/report` stream was scheduled relative, not absolute, and Node's step 6 failed because of it

All six generated the slow-endpoint arrivals with `next = now + gap` inside a
loop that only turns when a *checkout* arrives. That throws away the lateness
of every detection, and the lateness grows with load — so the more overloaded
the server got, the less `/report` it was offered. Backwards, and it silently
detunes the one scenario whose whole design is a knife-edge: 120 rps x 40ms
plus 6 rps x 800ms is 9.6 servers' worth of demand on 8, so a 15% shortfall in
`/report` moves ρ from 1.2 to 1.12 and the queue stops building.

Node felt it hardest, and there the demonstration simply did not happen.
Instrumented count, shared-pool scenario: **62 report arrivals in 12s where
72 were offered** — 5.2 rps against a nominal 6. Measured result before the
fix, against the other five in the same run set:

```
                     bulk_shared goodput   tier-0 success
node                        121 rps              100%
python / go / rust /         20 / 22 / 26 /       17% / 18% / 21% /
cpp / java                    8 / 28 rps           6% / 23%
```

Checkout did not die. The README's step 6 says "watch checkout die", and the
program printed its "the boundary is worth more than the two servers it
costs" paragraph over numbers that did not show it.

Fixed in all six by accumulating an absolute schedule (`next += gap`, in a
`while` so it catches up) exactly as the checkout stream already does, with
the reason written next to it. Node's shared-pool run immediately after the
change: goodput **121 → 36 rps**, tier-0 **100% → 29%**. In the full re-run
recorded above, all six now land in the same band (4-34 rps, 3-28%).

### Defect 2 — step 5's re-convergence was never printed, in any of the six

`PERTURB_AT_S = 6`, `MIN_RTT_RESET_S = 5` and `DURATION_S = 12`: the
controller's re-baseline lands at ~10.25s and the last row printed is t=10.
So every file's `WHAT TO LOOK FOR` item 4 told the reader to watch the limit
dip while `min_rtt` is stale *and come back once it re-baselines*, and the
run ended in the middle of the dip. Measured at 12s (python): limit
12.5 → 9.5 → 7.4, still falling, run over. The reader sees only the failure
mode the reset exists to prevent, which is the opposite of the lesson.

`DURATION_S` is now 20 in all six. Same run, python:

```
   t   accepted  p99_acc  limit
 4.0     193.0      76    12.6
 6.0     193.4      74    12.3   <-- service time x3
 8.0      68.2     225     9.3
10.0      62.8     200     7.3
12.0      67.0     189    10.1
14.0      66.4     197    10.9
16.0      65.8     220    11.1
18.0      66.9     225    11.0
```

Dip to 6.9-7.3 in all six, return to 11.0-14.1 in all six, and `accepted`
holding at ~66 rps — which is 8/0.12, the new capacity — while the limit goes
back to ~12. That is the Little's-law point the same paragraph makes: what
falls is the rate, not the limit. Cost: the run goes from ~1m30s to ~2m30s,
and the topic README's timing line was updated to say so and why.

### Checked and left alone

* Each runtime uses the primitive its README paragraph promises:
  `asyncio.Semaphore` + `wait_for`, Node's hand-rolled permit queue beside a
  live `monitorEventLoopDelay()`, Go's buffered channel selected against a
  `context`, tokio's `try_acquire_owned` with an RAII ticket, C++'s CoDel
  checked at both ends of the queue, `Semaphore.tryAcquire` on virtual
  threads.
* Node's `lag99` column does what the README's caveat says: it reads
  6.5-8.5ms through the whole ρ=1.3 collapse, at t=18s reporting 6.6ms while
  `inflight` is 1210 and p99 of accepted is 4691ms.
* C++'s `reject_ms` reads 0.0 under `static` where the others read ~51,
  because it rejects on the queue's measured head-of-line age instead of
  joining the queue and timing out of it. Both are CoDel; that column exists
  to expose the difference.
* Constants are identical across all six in both topics — checked line by
  line, not assumed.
* Every `How to run` command runs exactly as printed, in both topics, under
  the layer's convention that each line starts from the topic directory.
  The only README edit in this pass is topic 5's runtime estimate.

## The assignment's topic 6 question, answered by running it

Out of this pass's scope, but asked for explicitly: **the open-model and
closed-loop generators do produce different histograms against the same
server.** `06-.../python/fanout.py`, **RAN**, 3m18s. Same server, same
nominal rate:

```
model                                   n   achieved       p50       p99        max
open  (arrival schedule)             3806      153/s    283.5ms    832.3ms    1829.1ms
closed (15 VUs), as reported         3146      125/s     91.5ms    541.4ms    1819.0ms
closed, omission-corrected           3146              2185.0ms   5760.9ms    6382.4ms
open-model generator lateness p99: 3.80 ms
```

The two histograms differ where it matters — the open model puts 1585 samples
above 320ms against the closed loop's 107 — and the generator's own lateness
p99 of 3.80ms rules out the generator being the thing coordinating omission.
Topic 6 is not broken.

## Changes made during this pass

Code:

* `05-.../{python,nodejs,golang,rust,cpp,java}` shedder — `/report` arrivals
  on an absolute schedule; `DURATION_S` 12 → 20 with the reason; header
  runtime estimate corrected.
* `04-.../{python,nodejs,golang,rust,cpp,java}` metastable — the closing
  "still at zero goodput" sentence now prints the run's own figure.
* `04-.../python/metastable.py` — 20x → 10x; runtime estimate; `inflight`
  item rewritten to describe the plateau.
* `04-.../nodejs/metastable.js` — `inflight` item rewritten; `.js` → `.py`.
* `04-.../java/Metastable.java` — hazard 3's edit instruction made runnable.

READMEs: one line, topic 5's standalone timing estimate. No teaching content
added, no layer index touched.

**Prediction tables: still blank.** Both were checked before and after; every
cell in topic 4's eight rows and topic 5's seven rows is empty.

## Still owed

* `06-tail-latency-fanout-and-coordinated-omission` now has code in all four
  language directories (concurrent work by another agent, not verified here
  beyond the Python run above), but its README still says **"Not written yet
  — all four"** above the standalone block. That line is now wrong.
* `07-idempotency-and-degradation-decided-in-advance` has code in all three
  of its language directories; its README was not read in this pass.
* Both are outside this assignment's scope and are named here so the next
  pass does not have to rediscover them.

---

# Fill-in pass — `05-failure/lab`, second independent verification

**Date:** 2026-08-19
**Scope:** `05-failure/lab/` — the shared harness. A second pass, run without
reading the first lab section above as evidence: the stack was rebuilt from
the directory, brought up, and every script driven against it here. Where this
pass reaches the same conclusion as the section above, it reached it
independently; where it does not, the difference is recorded.

## The machine, as found

| | |
|---|---|
| Docker | daemon **UP** — Server 29.5.3 (Docker Desktop), Compose **v5.1.4** |
| `k6` | not installed, and not needed — it runs from the `grafana/k6` image |
| Postgres (local) | `pg_isready` → accepting connections on 5432 |
| Host port 8000 | **occupied** by an unrelated project's container (`m1t7-harness-api-1`) |

Because of that last row, every run below used a verification-only override
republishing `app`/`gateway`/`service-b`/`service-c` on 18000-18003. No repo
file was changed for it, and the override touches published host ports only —
in-container ports and the compose network are untouched, so k6, which reaches
the services as `app:8000` over the compose network, saw exactly the shipped
configuration.

Two things about the machine that bound how much the numbers below are worth:
several other projects' containers were running throughout, and other agents
were working in this repo at the same time. **Shapes are trustworthy here;
absolute rates are not.** Where a run was shortened from its shipped duration
to fit, the duration used is stated with the result.

## Every part of the harness

`RAN` = executed unmodified. `FIXED-THEN-RAN` = a defect was found, fixed, and
re-run afterwards.

| Item | Status | What was actually done |
|---|---|---|
| image build, `python:3.13-slim` | RAN | built from `lab/app/` |
| `docker compose config` | RAN | parses; all six profiles resolve to the sets `lab/README.md` specifies |
| all 21 contract env vars | RAN | every one of the 21 rows in `lab/README.md`'s table is present in `GET /admin/config`; none missing |
| `01_ramp.js` | RAN | `-e STEP=15`, plotted |
| `02_chain_naive.js` | RAN | `-e DURATION=40` |
| `02_chain_deadline.js` | RAN | `-e DURATION=40`, after draining the naive run's backlog |
| `03_retry_storm.js` | **FIXED-THEN-RAN** | `naive` and `budget`, `-e DURATION=120 -e FAULT_AT=30 -e FAULT_FOR=20` |
| `04_metastable.js` | **FIXED-THEN-RAN** | four runs; see below — this is the serious one |
| `05_shed.js` | RAN | all four modes, `-e STEP=10`, plotted together |
| `06_fanout.js` | RAN | `K=10 HEDGE=off RATE=50 DURATION=40` |
| `06_closed_loop.js` | **FIXED-THEN-RAN** (prose) | `K=10 VUS=5 DURATION=40`, VUs calibrated by hand |
| `07_idempotency.js` | **FIXED-THEN-RAN** | all three modes |
| `tools/plot_knee.py` | RAN | against a **real** k6 CSV and against the fixture |
| `tools/plot_amplification.py` | RAN | against two real variant CSVs |
| `tools/plot_goodput.py` | RAN | against a real metastable CSV |
| `tools/plot_shed.py` | RAN | against four real mode CSVs |
| `tools/plot_tail.py` | RAN | against a real open/closed pair |
| `tools/zombie_report.py` | RAN | inside the `gateway` container, and on the host with nothing up (it explains itself and exits cleanly) |
| `tools/make_fixtures.py` | RAN | regenerates all 18 fixtures; every one carries `# SYNTHETIC` on line 1 |
| matplotlib absent | RAN | with `matplotlib` forced to `ImportError`, every plotter still prints its ASCII chart, says no PNG was written, and exits 0 |
| Prometheus / Grafana | **not started** | `--profile observability`; optional, needed by no topic |

## What the runs showed, measured here

Topic 1's knee is real and the plateau is where Little's Law puts it —
`achieved` stops rising while `p99` does not, and `L` tracks `λ×W`:

```
 rho  offered  achieved  p50 ms  p99 ms  pool p50       L  lam x W
 0.2       75      75.1    43.6    61.3       0.1    4.03     3.33
 0.9      338     316.9   262.9   628.7     213.1   87.35    82.57
 1.1      413     321.6  1780.9  6087.1    1726.5  635.74   643.61
```

Topic 2's comparison exists and points the right way (40s, 50 rps, C at 800ms
against a 500ms budget):

```
naive       zombie completions/s = 18.13   C pool in use = 15/15
propagated  zombie completions/s =  0.00   C pool in use =  0/15
```

Gateway success is 0.0% in **both**, exactly as the pass above recorded: C
does 800ms of work against a 500ms budget, so no request reaching C can
succeed under any policy. The zombie and pool columns carry the finding.

Topic 3's retry budget is the thing that works, and by a wide margin:

```
naive   offered 6000 at the gateway -> 39689 at the leaf   gateway success 28.9%
budget  offered 6001 at the gateway ->  6049 at the leaf   gateway success 59.4%
```

Topic 5's four modes, all real: `none` runs to a p99 of 6106.8ms at ρ=1.3 with
**zero** rejections, `static` holds 240.5ms at the same offered load while
rejecting 35.7%, and under `priority` tier 0 keeps 66-100% success across the
overload steps while tier 3 goes to ~0%.

Topic 6, the question this assignment asked by name — **the closed-loop and
open-loop generators do produce different histograms against the same server.**
Same gateway, same K=10, same 40s, both nominally ~50 rps (the closed run's
VUs calibrated from a hand-timed unloaded request rather than left at the
default):

```
open   (constant-arrival-rate, 50 rps)  p50  390.0ms  p99 39536.6ms  completed  669
closed (ramping-vus, 5 VUs)             p50   88.1ms  p99   569.3ms  completed 1613
```

A 69x difference in p99, and the closed-loop number is the flattering one. The
closed run also **completed more than twice as many requests** — which is the
trap the file's own header warns about, and `plot_tail.py` prints that
direction explicitly rather than assuming the other one.

## Defects found and fixed in this pass

### 1. Topic 4's amplification mechanism did not exist — the serious one

`04_metastable.js` is the layer's flagship. Run at its shipped defaults, with
`FLUSHALL` at t=120s of a 420s run and nothing touched afterwards, it produced
**no failure of any kind**:

```
received 51014   completed 51014   failed 0
retries 0        timeouts 0        cache_misses 1041 (of which ~500 after the flush)
```

51,000 requests, not one retry, not one timeout, not one failure. Two
independent causes, both found by running it rather than by reading it:

**(a) The per-attempt timeout was computed and thrown away.** `with_retries`
passes each attempt a `timeout_ms`, and `/cached`'s `attempt()` accepted the
argument and never used it — it called `do_work()` bare. So the only bound on
a cache miss was `POOL_TIMEOUT_S`, 30 seconds. Under a miss storm requests
queue quietly for half a minute, nothing times out, nothing retries, and the
loop the topic is entirely about — timeout → retry → more misses — cannot
start. Fixed in `app/main.py` with `asyncio.wait_for` around `do_work`,
counted as a `timeout` and returned as a retryable 504; cancelling the
checkout is also what returns the waiter's place in the pool queue, so the
retry arrives as a new request rather than as a second claim on the same slot.

**(b) The load and cache constants made the premise arithmetically
impossible.** Offered load was 60% of the *pool's* capacity, so the database
could serve 100% of the traffic unaided and the cache was decorative; and a
300-second TTL over 500 keys means that after one refill pass there are **no
misses left at all**, so `FLUSHALL` costs one second of work and cannot leave
the system in a regime. `LOAD_PCT` is now `LOAD_MULT`, a multiple of the
*database's* capacity — greater than 1 on purpose, because running at a load
only the cache makes serviceable is the whole premise — and `CACHE_TTL_S` is
sized so eviction gives a continuous miss stream, which is how the reference
implementation in `04-.../python/metastable.py` has always modelled it.

With both fixed, the trigger produces a real event where there was none. Stable
baseline first, verified flat over five consecutive 10s windows:

```
recv/s 225.2  goodput/s 224.6  fail/s 0.0  retry/s 0.0  to/s 0.0  hit% 88.5
recv/s 225.3  goodput/s 225.8  fail/s 0.0  retry/s 0.0  to/s 0.0  hit% 90.2
```

then `FLUSHALL`, load unchanged:

```
t+10s  recv/s 227.1  goodput/s 148.1  retry/s 142.5  to/s 181.5  hit% 45.8  inflight 448
t+20s  recv/s 225.3  goodput/s 222.3  retry/s  74.8  to/s 117.6  hit% 87.5  inflight  39
t+30s  recv/s 225.2  goodput/s 228.0  retry/s   0.0  to/s   0.0  hit% 92.4  inflight  11
```

**What this is not.** It clears in about 25 seconds, which is the topic
README's own *"recovers in twenty seconds — that is a slow drain, not
metastability, and your amplification is too weak"*. Its prescription is to
raise offered load until the database sits at 75-85% utilisation in the stable
state. That was tried, at 83%, and it fails from the other side: the baseline
never becomes flat at all — goodput 156 rps of 240 offered with a third of
requests failing **before any trigger**, which is the same README's third
broken-criterion, *"goodput was also near zero before the trigger — you were
already broken"*. The band between those two was not found on this machine
within this pass, and no constant was tuned toward a desired outcome. The
shipped defaults are the ones with a verified-flat baseline and a real
amplification event; `setup()` now prints the stable-state database
utilisation the current constants imply, so the reader aims at the band
instead of guessing at it, and the file's header states both failure
directions as measured facts.

**A sustained metastable state was therefore not reproduced on the container
harness in this pass.** That is recorded as an open item, not as a result.

### 2. `03_retry_storm.js` printed a number that is structurally always zero

Its teardown printed `leaf retries=${c.retries}`. C is the leaf: it has no
downstream, so it never retries, so that column reads 0 in every variant of
every run and can never carry information. The retries that make the
amplification are issued at the gateway and at B. Now printed there, next to
the amplification ratio the topic is about:

```
variant=naive  offered(gateway)=4500  leaf received=28952  amplification=6.43x
            retries issued: gateway=6356 + B=18263 = 24619   gateway success=31.1%
```

### 3. `06_closed_loop.js` contradicted itself between its header and its output

The header says, at length, **not** to compare completed counts, because a
saturated open run's completions collapse. `setup()` then printed *"compare
the request COUNTS too"* to the console — the one place the reader is actually
looking. Measured here, the header is right and the console line was wrong:
the closed run completed 1613 against the open run's 669. `setup()` now points
at the rates, and says why the counts do not survive saturation.

### 4. `07_idempotency.js` mislabelled its own run, and hid its headline

`teardown()` printed `mode=${r.mode}` — the **service's** `IDEMPOTENCY_MODE`,
which is `correct` for both the `correct` and the `chaos` script modes. So two
of the three runs announced themselves identically and could not be told apart
in a log. It now prints the script's mode and the service's setting.

Worse, the ambiguous-result counters — the entire point of `chaos` — were
reachable only inside the summary JSON. `handleSummary` now prints them, and
the topic's headline finally appears in the topic's own output:

```
mode=chaos (service idempotency=correct)  charge_rows=1  distinct_responses=1
  created=0  replayed=38  409=0  422=0  response lost=26
  26 clients could not tell "did not happen" from "happened, answer lost",
  retried with the same key, and still produced one charge. That is the topic.
```

against, in the same three-run sequence, `mode=naive ... charge_rows=15` and a
fingerprint probe that replays the wrong charge with a 200 where `correct`
returns 422.

The same file's header also credited `chaos` to toxiproxy. It is
`POST /admin/fault {drop_pct}`, and that is the right layer, for the reason
`app/faults.py` itself gives: the charge *committed* and only the answer was
lost, which a network fault cannot express. Header corrected to match the code.

## Claims in the report under review that did not hold

- *"the compose stack and k6 runs are blocked; `docker info` fails and `k6` is
  absent"*. Docker is up, and `k6` is supplied by the `grafana/k6` image and
  never needs installing. Nothing in the harness is blocked on this machine.
- *"the app was actually run ... zombie counted in the naive chain,
  `deadline_rejected` in the propagated one"*. Half true. Zombies are counted
  in the naive chain (18.13/s here). `deadline_rejected` was **0** in the
  propagated run — the budget is spent at the pool and at the statement
  timeout, not on arrival, so the counters that move are `deadline_abandoned`
  and the statement-timeout cancellations.
- *"all nine k6 scripts ... scenario/exec wiring confirmed"*. Parsing and
  stub-loading confirm wiring; they cannot confirm that a script measures what
  it claims. Four of the nine did not, and are fixed above.

## Coverage, and the prediction tables

Counted from the directories, not from the READMEs:

| Topic | Languages its README specifies | Present |
|---|---|---|
| 1 | 6 | 6 |
| 2 | 6 | 6 |
| 3 | 5 (no C++, stated in the README) | 5 |
| 4 | 6 | 6 |
| 5 | 6 | 6 |
| 6 | 4 | 4 |
| 7 | 3 | 3 |

Topics 6 and 7 gained their standalone programs from another agent while this
pass was running. They are **not** verified here — outside this assignment's
scope — and no claim is made about them beyond their existing.

All seven `Predict, then record` tables were re-checked after this pass and
are **blank**. (Topic 6's `predicted tail prob` column is the README's own
`1 − 0.99^K` arithmetic, deliberately pre-filled, and was left alone.)

No number anywhere in this section was copied from a fixture. `out/` was left
holding only `fixtures/`; every CSV and PNG generated during this pass was
deleted, so nobody plots a run made on a machine this loaded and records it.

## Changes made during this pass

* `lab/app/main.py` — `/cached`'s retry attempts now **apply** the per-attempt
  timeout they are handed, via `asyncio.wait_for`, counted as a timeout and
  returned as a retryable 504.
* `lab/scripts/04_metastable.js` — `LOAD_MULT` (a multiple of the database's
  capacity) replaces `LOAD_PCT`, which is kept as an alias; `SERVICE_MS`,
  `CACHE_KEYS` and `CACHE_TTL_S` give a continuous eviction-driven miss
  stream; `setup()` prints the implied equilibrium hit rate and stable-state
  database utilisation and warns at `LOAD_MULT <= 1`; the header records both
  measured failure directions.
* `lab/scripts/03_retry_storm.js` — teardown reports the amplification ratio
  and the retries issued at the gateway and B, instead of the leaf's
  always-zero counter.
* `lab/scripts/06_closed_loop.js` — `setup()` guidance no longer contradicts
  the file's own header.
* `lab/scripts/07_idempotency.js` — run labelled by the script's mode;
  ambiguous-outcome counters printed; `chaos` header corrected from toxiproxy
  to `POST /admin/fault`.
* `04-metastable-failure/README.md` — `How to run` gains the `LOAD_MULT` /
  `CACHE_TTL_S` sizing note and the "verify goodput is flat before you flush"
  precondition. No other README was touched; no prediction table was touched.

## Still owed

* A sustained metastable state on the container harness. The mechanism is now
  present and the trigger produces a real amplification event; the constants
  that hold the system down have not been found here. `04-.../python/metastable.py`
  reaches 0.0 rps of goodput and stays there, so a working reference for the
  regime exists in this layer to calibrate against.
* Topic 3 past t=100s, and all four variants: only `naive` and `budget` were
  run, at 120s, so `plot_amplification`'s `t=200s` and `t=280s` columns are
  empty — correct behaviour, not a result.
* `--profile observability` (Prometheus/Grafana) is still unexercised.
* Topics 6 and 7's standalone programs, newly landed, are unverified.


## Spot-check — topics 06 and 07, 2026-08-19

Run directly on the host (macOS 27, arm64 M1), not by an agent, to close the
"written but unverified" gap the fill-in pass reported.

| Program | Status | Wall time |
|---|---|---|
| `06-tail-latency/python/fanout.py` | RAN | 198s |
| `06-tail-latency/golang/fanout.go` | RAN | 195s |
| `06-tail-latency/nodejs/fanout.js` | RAN | 191s |
| `06-tail-latency/rust/fanout` | RAN (produces the sweep; not separately timed) | ~200s |
| `07-idempotency/python/idempotency.py` | RAN | < 45s |
| `07-idempotency/nodejs/idempotency.js` | RAN | < 45s |
| `07-idempotency/golang/idempotency.go` | RAN | < 45s |

**Correction to an earlier entry.** Topic 06's Node arm was briefly recorded as
hanging. It does not hang — it takes ~191s, which is *faster* than the Python
and Go arms. The error was a 45-second timeout applied to an experiment that
legitimately needs minutes to separate a p99 from a p50. The topic README now
carries the measured runtimes so the next person does not repeat the mistake.
No constant was changed: shortening the sweep would make it finish sooner and
measure less.

All three of topic 07's arms independently reach the same finding — that a
compile-time flag with an owner is a plan, not a kill switch — which is the
cross-language agreement the topic is built to produce.

This records that the code *executes*. It records nothing about whether
anything was learned; the `Predict, then record` tables remain unfilled.
