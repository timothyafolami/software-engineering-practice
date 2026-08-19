# Layer 8 · Topic 7 — Fault injection: make the dependency slow, not absent

### The takeaway (read this first)

**The one idea:** systems are designed against "up or down" and killed by
"neither." A dependency that answers correctly in 800 ms instead of 8 ms
breaks things that a dependency returning connection-refused never touches —
because every queue in the path is sized for the fast case.

**Why it matters in practice:** this is the production incident shape. Nothing
is down. Everything is late. And the mechanism that turns "300 ms slower" into
"the service is unusable" is not proportional — it is a pool, a semaphore or a
retry policy, amplifying it.

**You'll know it landed when:** you can predict, from a service's pool size,
concurrency limit and timeout configuration, roughly how much dependency
latency it takes before throughput collapses — and you notice that the
collapse point is far lower than anyone expects.

## The concept

The amplification is Little's Law and it stops being subtle the moment you do
the arithmetic. With a connection pool of size `P` and a per-request database
service time `S`, the maximum throughput through that pool is `P / S`:

- `P = 5`, `S = 8 ms` → `5 / 0.008 = 625` requests per second.
- Same pool, `S = 300 ms` → `5 / 0.3 ≈ 16.7` requests per second.

Nothing else changed. Every request beyond that second figure is queueing for
a connection, and if `pool_timeout` is unset it queues *forever* — so the
observable symptom is not errors, it is unbounded latency, then a health check
timing out, then a restart, then a thundering herd against a cold cache.

That last sequence is a **metastable failure**: the system has two stable
states, and enough load plus enough latency pushes it into the bad one, where
it stays even after the trigger is removed, because the retry backlog now
sustains it. Layer 5 covers metastability directly; this topic is the
code-level version, run against a service whose source you can edit between
ladders.

The fix kit — all of it lift-to-work, all of it code you could paste into a
PR:

- **A deadline, propagated.** One budget per request, decremented as it is
  spent, passed down. In Python: an explicit deadline on the request context
  plus `asyncio.timeout()`.
- **Bounded pool waits.** `pool_timeout` so a checkout fails fast rather than
  queueing forever, and a server-side `statement_timeout` so a slow query
  cannot hold a connection past its usefulness.
- **Retry with jitter *and a budget*.** Not `@retry(3)`. A budget caps retries
  as a fraction of total requests, so retries can never become the load. Full
  jitter, not exponential-with-a-fixed-base.
- **A circuit breaker tripped on *latency*, not only on errors** — most
  breakers count failures only, and therefore never trip during exactly this
  incident.
- **Load shedding at the edge:** when the queue exceeds the deadline budget,
  reject immediately rather than accept work you cannot finish in time.

The non-obvious thing to hold onto: several of these fixes do not improve
throughput at all. They convert unbounded latency into fast, honest failure.
That is a real and desirable outcome, and recognising it as a win is most of
the maturity this topic is trying to build.

## How each language actually gets there

Six languages in the mechanism section, because "where is the bound and who
enforces the deadline" is a property of the runtime and its ecosystem, and
that is precisely the variable. **Three of the six run inside the compose
stack** — the Python service under test, plus the Go and Node consumers the
lab already ships — because ladder F is about three clients against one
degraded API, and a fourth client *there* would add a build and no new
mechanism. The other three each own a mechanism ladder F cannot show, so they
ship instead as self-contained programs in this topic's folder that inject the
same fault in-process: Rust's drop-cancellation, Java's virtual threads moving
the bound, and C++'s total absence of either. They are under *The other three
languages* in **How to run**.

**Python (your stack).** The pool is the whole story.
`create_async_engine(..., pool_size=, max_overflow=, pool_timeout=)`, and note
that the effective concurrency limit is `pool_size + max_overflow` **per
process** — with multiple Uvicorn/Gunicorn workers you have that many pools,
which is how teams exhaust Postgres `max_connections` while every worker's
metrics look fine. `asyncio.timeout()` (3.11+) is the deadline primitive, and
it is *not* automatic: nothing in FastAPI applies one for you. httpx needs an
explicit `Timeout(connect=, read=, write=, pool=)`, and the `pool` component
is the one nobody sets — which means a client can be blocked on connection
acquisition with every configured timeout still unfired.

**Node.** The event loop hides queueing entirely, which is the specific
hazard: the process looks idle while a thousand callbacks wait, because
"waiting" is not a thread you can count. Measure `perf_hooks`
`monitorEventLoopDelay()` or you will not see the amplification at all.
`undici` has explicit `headersTimeout`/`bodyTimeout`; `pg`'s `Pool` has
`connectionTimeoutMillis` plus a server-side `statement_timeout`. Node's other
trap is that the default HTTP agent's socket pool is a second, invisible
queue in front of the database pool.

**Go.** `context.Context` deadlines propagate by convention through the entire
ecosystem, and `database/sql` has `SetMaxOpenConns` and `SetConnMaxLifetime`.
Go gets this right more often for a structural reason worth internalising
rather than envying: the deadline is a mandatory first parameter, so
forgetting to propagate it is a visible omission at every call site rather
than an invisible default. You can approximate it in Python with a
context-variable deadline plus a lint rule, and that is exactly the change
worth proposing at work.

**Java.** The most configuration surface and the best introspection. HikariCP
exposes pool wait time as a first-class metric, which is the single most
useful number in this entire topic and the one Python makes you instrument
yourself via `PoolEvents`. Virtual threads change the shape of the problem
rather than removing it: blocking calls no longer consume a scarce OS thread,
so the thread pool stops being the bound — and the *database* pool, and the
database's own `max_connections`, become the bound instead, sometimes
dramatically faster than before. "We removed the thread limit and it got worse"
is a real 2025-era Loom migration story and this is the mechanism.

**Rust.** Deadlines are values you must thread through explicitly
(`tokio::time::timeout` wrapping a future), and there is no ambient
cancellation — but dropping a future *does* cancel it, which is a genuinely
different cancellation model from every other language here and it changes
what "timeout" means: the work stops mid-way rather than continuing
unobserved. `sqlx`/`deadpool` pools expose acquire timeouts explicitly. The
lesson for Python: a timeout that abandons the caller while the work keeps
running is not a deadline, it is a lie with better latency.

**C++.** Nothing is provided. Every timeout is a syscall argument you pass
yourself — `SO_RCVTIMEO`, `poll` with a millisecond count, a `select` timeval
— and a connection pool is a data structure you wrote. This is the useful
extreme case: it shows that every fix in the kit above is *someone's code*,
not a language feature, and that the reason your framework has a default is
that somebody made a choice you have not read.

## The experiment

Toxiproxy sits between the API and Postgres. k6 drives a **fixed arrival
rate** (`constant-arrival-rate`, never `constant-vus` — VU-based load
self-throttles when the system slows down and will hide the collapse
entirely).

**Ladder A — find the knee.** Step the injected latency: 0 → 25 → 50 → 100 →
200 → 400 → 800 ms downstream, two minutes each. At each step record:
throughput, p50/p99, error rate, pool checkout wait time (SQLAlchemy
`PoolEvents` or `pool.status()`), and `pg_stat_activity` counts.

**Ladders B–E — one fix at a time.** Re-run the same ladder four more times,
adding exactly one fix per run: (1) `POOL_TIMEOUT_S=0.5`, (2) a propagated
per-request deadline via `REQUEST_DEADLINE_MS`, (3) retry with full jitter and
`RETRY_BUDGET_PCT=10`, (4) a latency-tripped circuit breaker via
`BREAKER_LATENCY_MS`. For each, record whether the knee *moves*, or whether
the failure mode merely *changes* from unbounded latency to fast 503 — and
record those as different outcomes, because they are.

**Ladder F — three clients, one fault.** Point the Go and Node consumers at
the same degraded API and drive all three at the same offered rate. Same
injected latency, three runtimes, three different collapse behaviours: where
each one queues, what it reports, and which of the three tells you it is in
trouble before it fails.

Finally, run the other toxics once each, to confirm they are genuinely
different failures rather than degrees of one: `timeout` (connection dropped),
`bandwidth` (slow bulk transfer), `slow_close`, `reset_peer`.

## How to run

```
cd 08-craft/lab && docker compose up -d

# The proxy FIRST, then the seed. The api container's DATABASE_URL points at
# toxiproxy:5433, and toxiproxy opens no listener until a proxy is created --
# so seeding first dies on `ConnectionRefusedError ... ('toxiproxy', 5433)`.
# The proxy name goes LAST on `toxic add|update`: toxiproxy-cli 2.x parses
# `[options] <proxyName>`, and name-first fails with "Required argument 'type'
# was empty."
docker compose exec toxiproxy /toxiproxy-cli create -l 0.0.0.0:5433 -u postgres:5432 pg
docker compose exec toxiproxy /toxiproxy-cli toxic add -t latency -n lat -a latency=0 pg
docker compose exec api python seed.py     # `make seed` -- the api image is
                                           # python:3.13-slim and has no make

# Ladder A
for ms in 0 25 50 100 200 400 800; do
  docker compose exec toxiproxy /toxiproxy-cli toxic update -n lat -a latency=$ms pg
  docker compose --profile load run --rm k6 run -e STEP=$ms /load/t7_latency_ladder.js
done

# Ladders B-E: one fix per run, each one environment variable
POOL_TIMEOUT_S=0.5      docker compose up -d --force-recreate api
REQUEST_DEADLINE_MS=800 docker compose up -d --force-recreate api
RETRY_ATTEMPTS=3 RETRY_BUDGET_PCT=10 docker compose up -d --force-recreate api
BREAKER_LATENCY_MS=300  docker compose up -d --force-recreate api

# Ladder F: three clients, one fault
docker compose --profile consumers up -d consumer-go consumer-node
docker compose --profile load run --rm k6 run -e CLIENT=python /load/t7_clients.js
docker compose --profile load run --rm k6 run -e CLIENT=go     /load/t7_clients.js
docker compose --profile load run --rm k6 run -e CLIENT=node   /load/t7_clients.js

docker compose exec postgres psql -U app -d craft_lab \
  -c "select state, count(*) from pg_stat_activity group by state;"
```

Two readouts exist so a recorded row is never orphaned from the state that
produced it: `GET /_pool` returns connection-checkout wait p50/p99 (SQLAlchemy
will not tell you this; `lab/api/app/db.py` instruments `PoolEvents` to produce
it, and it is the number HikariCP gives Java for free), and `GET /_stats`
returns the retry budget's actual spend fraction and the breaker's trip count.
`t7_latency_ladder.js` reads both in `teardown()` and prints them per step, and
it reports `dropped_iterations` -- non-zero means the **generator** fell behind
and every number in that step is coordinated omission.

`/healthz` deliberately does not touch the database, so a slow dependency does
not turn into a restart loop and destroy the ladder you came to measure.

The whole fix kit is in `lab/api/app/core/resilience.py` and is unit tested
without any of the above:

```
cd 08-craft/lab/api && DATABASE_URL=sqlite+aiosqlite:///:memory: \
  python3 -m pytest tests/unit/test_resilience.py -q
```

Those seven tests pass here, and the budget one is worth running before spending
an hour on a ladder: it asserts that `RETRY_BUDGET_PCT=10` really does cap
retries at 10% of requests, which is the check topic 7 tells you to make when
retries appear to improve things monotonically.

**Blocked on this machine, with the exact unblock command:**

| What | Why | Unblock |
|---|---|---|
| `k6` on the host | not installed | run it through compose as shown (the `grafana/k6` image needs no host install), or `brew install k6` |

Nothing else is blocked: the Docker daemon is up, the five images the stack
needs are local, and the whole sequence above — stack, proxy, toxic, seed, one
k6 step against `t7_latency_ladder.js`, one against `t7_clients.js` for the Go
and Node consumers — was run end to end while this line was written. What
remains is the ladder itself, which is yours to run and record.

Before recording anything, confirm `PGDATA` is on the named volume (`pgdata` in
`compose.yml`) and not a bind mount into `/Users/...`; on Docker Desktop a bind
mount routes every write through file sharing and the ladder measures that
instead of your timeout budget. Note the mount point in `compose.yml` is
`/var/lib/postgresql`, not `.../data`: from `postgres:18` the official image
keeps the cluster in a major-version subdirectory, and a volume at the old path
makes the container exit 1 at startup rather than start on the wrong disk.

### The other three languages, all of it native

Rust, Java and C++ are the three the compose ladder has no client for, and each
owns a mechanism the ladder cannot show. Each is one self-contained program that
runs its own latency ladder in-process: a real loopback TCP dependency whose
service time is a knob, a real connection pool, and load offered at a **fixed
arrival rate** — the same open-model rule as the k6 scripts, for the same
reason. Every latency is measured from a request's *scheduled* arrival, so
queueing is counted rather than erased, and each program prints its generator's
worst lag so you can tell when the number is coordinated omission instead.

No Docker, no k6, nothing to install. Run from
`08-craft/07-fault-injection-slow-not-absent/`:

```
g++ -std=c++20 -O2 -pthread -o /tmp/t7_cpp cpp/slow_not_absent.cpp && /tmp/t7_cpp
cd java && javac SlowNotAbsent.java -d /tmp/t7java && java -cp /tmp/t7java SlowNotAbsent
cd rust/slow_not_absent && cargo run --release
```

Each takes roughly ten to fifteen seconds of run time — they are load tests, and
the phases have to drain. The Rust arm's *first* invocation also compiles tokio,
so budget longer for that one run only. Each ends by working Little's Law from its own measured service
time and printing the arithmetic next to the outcome, so the prediction and the
result sit on the same screen.

| Program | The mechanism it makes visible |
|---|---|
| `cpp/slow_not_absent.cpp` | the useful extreme: the pool is a `std::vector<int>` behind a `condition_variable`, and the deadline is a `timeval` handed to `setsockopt` as `SO_RCVTIMEO`. Neither exists until someone writes it, which is the point — every entry in the fix kit above is *somebody's code*, and the reason your framework has a default is that a person chose one you have not read. Also the cost nobody budgets for: a connection abandoned mid-response cannot go back in the pool, because the reply is still in flight |
| `java/SlowNotAbsent.java` | pool wait as a first-class number, and the Loom migration. Four phases that differ only in where the bound is: platform threads, then virtual threads against the same pool, then a pool sized under the dependency's own connection ceiling. The finding is the middle step — removing the thread limit does not remove the queue, it moves it onto the database, which answers with an error instead of a wait. Phase B is the companion warning: a quiet pool-wait metric can mean the queue is upstream of the thing you instrumented |
| `rust/slow_not_absent/` | the cancellation model. `tokio::time::timeout` wrapping the work *drops* the future, so the await stops where it stands and the permit's `Drop` returns it — free, structural, and one `tokio::spawn` away from being opted out of. Phases C and D are that one line apart and are identical on every caller-side metric. Then the line neither of them improves: the dependency executed the same number of requests in both. Cancelling a future is a client-side event, which is why the fix kit lists a client deadline **and** a server-side `statement_timeout` as separate items |

The three arms are deliberately not identical experiments — each is tuned to the
mechanism it is about, so their absolute numbers are not comparable with each
other and are not meant to be. What transfers between them, and to the compose
ladder, is the shape: a dependency that got slower and never failed, an error
rate that stayed at zero while p99 left the axis, and a bound that bought no
throughput and was still the right change.

## Predict, then record

Predict **before** running: at what injected latency does throughput fall
below half of baseline? Compute the Little's Law estimate from your configured
`POOL_SIZE + MAX_OVERFLOW` and your measured baseline database service time,
and write that number down next to your gut number. Two predictions, one
derived and one intuitive, is the point — the gap between them is what this
topic is teaching.

| Injected latency (ms) | rps | p50 | p99 | error % | pool wait p99 | active PG conns |
|---|---|---|---|---|---|---|
| 0 | | | | | | |
| 25 | | | | | | |
| 50 | | | | | | |
| 100 | | | | | | |
| 200 | | | | | | |
| 400 | | | | | | |
| 800 | | | | | | |

| Fix applied | knee moves to | failure mode becomes |
|---|---|---|
| baseline | | |
| + `POOL_TIMEOUT_S` | | |
| + propagated deadline | | |
| + retry with budget | | |
| + latency circuit breaker | | |

| Client | first symptom observed | what it reported | collapsed at |
|---|---|---|---|
| Python (in-process) | | | |
| Go consumer | | | |
| Node consumer | | | |

**What would mean the experiment is broken rather than your prediction wrong:**

- **Throughput stays flat as you raise latency.** You are almost certainly
  running k6 with `constant-vus`. VUs wait for responses, so the offered load
  falls with the system: you built a closed loop that cannot show collapse.
  Switch to `constant-arrival-rate`.
- **p99 never exceeds your injected latency.** Your requests probably are not
  reaching the database — check for a cache, a response served out of
  SQLAlchemy's identity map, or a `DATABASE_URL` that points at `postgres`
  directly instead of at `toxiproxy:5433`.
- **Adding retries improves things monotonically at every step.** The budget
  is not engaged. Verify by counting retries as a fraction of requests: the
  entire point is that unbudgeted retries make the 400 ms and 800 ms steps
  *worse*, and if you do not see that, you have not reproduced the incident.
- **The knee arrives far earlier than Little's Law predicts and pool wait is
  near zero.** The bound is somewhere else — Uvicorn's concurrency limit, the
  event loop, or `max_connections` on Postgres. Find which count is smallest
  before concluding the model is wrong; the model is about whichever count
  binds first.
- **Latency measurements are dominated by disk.** Check that `PGDATA` is on a
  named volume and not a bind mount into `/Users/...`; on Docker Desktop that
  routes every write through file sharing and you will be measuring the wrong
  thing entirely.

## Answer before moving on

1. Little's Law predicted the knee; your measurement was somewhere else. Name
   two mechanisms that push the real knee *earlier* than the prediction, and
   one that pushes it later.
2. The circuit breaker trips on latency rather than errors. What new failure
   mode does that introduce, and how would you bound it?
3. `pool_timeout` converted unbounded latency into fast 503s without improving
   throughput at all. Argue that this is a good change, in terms a product
   manager would accept.
4. The Go client and the Python service failed differently under the same
   fault. Say which of the two differences came from the runtime and which
   came from a configuration choice someone made — and how you would tell.

## Next up

[Topic 8 — Coverage as a diagnostic, mutation as the target](../08-coverage-mutation-and-the-regression-rule/README.md):
you have now built four fixes. The last topic asks the question that decides
whether they stay fixed.
