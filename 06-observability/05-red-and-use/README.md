# Layer 6 · Topic 5 — RED for services, USE for resources, and the seam between them

### The takeaway (read this first)

**The one idea:** RED (rate, errors, duration) describes what *users* get from a
service; USE (utilization, saturation, errors) describes what a *resource* is doing.
Neither diagnoses alone — RED says the service is sick without saying why, USE says
a resource is strained without saying whether anyone cares. Diagnosis is the join.

**Why it matters in practice:** "which layer is lying to you" *is* this join. A
resource at low utilization and total saturation is the most common shape of a
mysterious latency incident, and utilization alone shows a healthy green number
while requests queue behind it.

**You'll know it landed when:** you stop asking "is the CPU high?" and start asking
"is anything queueing?", and you know which specific metric answers that for every
resource you run.

## The concept

**Utilization is not saturation, and the difference is where the incident lives.**
Utilization is the fraction of time a resource was busy. Saturation is the work
*waiting* for it. They are different questions with different units, and the second
one has no upper bound — which is exactly why it is the useful one.

A connection pool with 5 of 5 connections checked out is 100% utilized, and has been
since the moment load arrived. The number that changes during an incident is how
many requests are queued behind it and for how long. Once utilization pins at 100%
it stops carrying information; saturation is the only thing still moving.

The same asymmetry in time rather than in count: a container under a CFS quota can
average a modest CPU number over a minute while being throttled in a large fraction
of the individual 100ms periods inside that minute. Utilization *averages the
throttling away* — that is what an average does — while the throttled-periods ratio
counts the events directly. Step 4 of the experiment measures both on the same
container so you get your own figures rather than borrowing anyone's.

Every resource you run has both, and knowing which column to look at is most of the
skill:

| Resource | Utilization | Saturation (the one that matters) |
|---|---|---|
| Container CPU | `container_cpu_usage_seconds_total` | `cfs_throttled_periods_total / periods_total` |
| Container memory | working set / limit | page faults, OOM kills |
| DB connection pool | checked-out / size | **checkout wait time**, timeouts |
| Postgres | active backends / `max_connections` | `pg_stat_activity` wait events, lock waits |
| Uvicorn | busy workers | accept-queue depth / `listen` backlog |
| Disk | IO time | queue depth, `await` |

Read that table as one claim repeated six times: **the useful metric is almost never
the one the platform gives you by default.**

RED has a subtlety worth meeting here rather than in an incident. You can derive it
from the SDK's `http.server.request.duration` histogram, or from the Collector's
**spanmetrics connector**, which synthesises RED from the trace stream. The two
disagree the moment sampling drops below 100%, because spanmetrics counts sampled
spans. Whether that disagreement is a bug depends entirely on where spanmetrics sits
relative to the sampler in your pipeline — which is the reason this lab makes you
write the collector config instead of copying one.

## How each language actually gets there

The saturation metric in the table that you cannot buy off the shelf is **connection
checkout wait**. What your runtime hands you for free ranges from nothing at all to
a full distribution, and the four answers make a clean ladder:

**Python / SQLAlchemy — nothing, by construction.** The pool exposes `checkout` and
`checkin` events and a `status()` string; there is no timer anywhere. Worse, and
this is Topic 2's finding arriving as a metric: the wait happens *before* any
statement is issued, so it produces no span either. You instrument it by hand —
record a timestamp in the `checkout` event handler, subtract, feed a histogram, and
publish `db.client.connection.count{state="used"|"idle"}` alongside it. The metric
you need is the one metric nobody ships.

**Node.js / node-postgres — an instantaneous gauge.** `pool.waitingCount`,
`totalCount` and `idleCount` tell you the queue *depth right now*. That is a sample,
not a distribution: poll every 15 seconds and a 400ms queue between polls simply did
not happen as far as your dashboard is concerned. It is better than nothing and
strictly worse than it looks, and knowing which is the point.

**Go / `database/sql` — cumulative counters, free.** `DB.Stats()` returns `WaitCount`
and `WaitDuration` as monotonic totals, so `rate(wait_duration) / rate(wait_count)`
gives you mean wait per interval with no instrumentation at all. Go is the only one
here where saturation is a standard-library property. It is also honest about the
limit: totals give you a mean, never a percentile, and pool waits are exactly the
distribution where the mean is the least interesting statistic.

**Java / HikariCP — an actual histogram.** The Micrometer binder ships a timer for
connection acquisition, so you get percentiles of checkout wait out of the box, in
the ecosystem with the oldest production pooling culture. Java is the ceiling: this
is what the other three are approximating by hand.

**Languages: four, and the fourth is the argument.** The observable here is a
property of the pool library's stats API, not of the runtime's scheduler. Rust and
C++ are dropped because neither has a conventional pool whose defaults teach
anything — you choose a crate or write it, so "what does your pool tell you by
default" has no answer to compare. Everything else in this topic (cgroups,
spanmetrics, the collector) is language-neutral and lives in the stack.

## The experiment

**Incident replay: connection pool exhaustion.** The stack's `small_pool` defect is
already the fault; this topic is about seeing it coming.

1. Build both dashboards. RED for `api`. USE for the container, the pool and
   Postgres, with the saturation column from the table above — not the utilization
   column, which is what every default dashboard gives you.
2. Instrument the pool properly: SQLAlchemy `checkout`/`checkin` events →
   `db.client.connection.count{state="used"|"idle"}` (current semconv name) plus a
   histogram of *checkout wait time*, the metric that does not exist by default and
   is the one you need.
3. Ramp k6 from 10 to 120 VUs over 10 minutes against a pool of 5. Record three VU
   counts: where p99 turns up, where pool utilization hits 100%, and where checkout
   waits begin. They will not be the same number, and the order they arrive in is
   the finding.
4. Add `cpus: "0.5"` to `api` and repeat. Watch CPU utilization stay modest while
   the throttled-periods ratio climbs — Layer 1's scheduler material, on a
   dashboard, in a service.
5. Run spanmetrics RED alongside SDK RED at 10% head sampling and quantify the
   disagreement. Predict the factor first; the answer is more interesting than the
   obvious one.

## How to run

The language ladder has a standalone version — four programs, one per rung, no
Docker and no database. Each one runs the same ramp (2 → 120 in flight against
a pool of 5), measures every checkout, and then shows you only what that
runtime's pool would actually have told you:

```
python3 python/pool_saturation.py
node nodejs/pool_saturation.js
cd golang && go run pool_saturation.go
cd java && javac PoolSaturation.java -d /tmp/javabuild && \
  java -cp /tmp/javabuild PoolSaturation
```

Each takes six to nine seconds — the ramp is real, and the numbers come out of
your machine. Read them in that order and the ladder builds itself: Python
prints a `status()` string and the span timeline with the pool wait as a gap
with no span in it; Node prints how much of the queue a 250ms scrape missed;
Go prints `rate(WaitDuration)/rate(WaitCount)` next to the p99 the counters
cannot produce; Java prints the acquire timer's percentiles and the
interpolation error you take on when you publish buckets instead.

Then the real thing, from `lab/` — see [`../lab/README.md`](../lab/README.md):

```
docker compose up -d && docker compose run --rm k6 run /scripts/ramp.js

OTEL_TRACES_SAMPLER=parentbased_traceidratio \
OTEL_TRACES_SAMPLER_ARG=0.1 docker compose up -d api
```

Step 4's cgroup checks must run **inside** the container. The host here is macOS on
arm64 and has no cgroup filesystem at all; run these from the macOS shell and you
get "no such file", which reads like the quota failed to apply:

```
docker stats --no-stream api
docker compose exec api cat /sys/fs/cgroup/cpu.max
docker compose exec api cat /sys/fs/cgroup/cpu.stat   # nr_periods, nr_throttled
```

`cpu.max` is `QUOTA PERIOD` in microseconds, both on one line. `50000 100000` is
half a CPU; `100000 100000` is one CPU. Read it carefully — misreading the two
columns is how people conclude the period length fixed their latency.

**There is no quota unless you set one.** Run the ramp as printed above and
`cpu.max` reads `max 100000` — no quota — and `cpu.stat` reads `nr_periods 0`,
`nr_throttled 0`. Those two zeros mean the accounting never ran, which is a
different statement from "the quota was applied and never hit", and only the
second one is a result you can put in the table below. `api` takes `API_CPUS`
for this:

```
API_CPUS=0.5 docker compose up -d api
docker compose exec api cat /sys/fs/cgroup/cpu.max    # 50000 100000
docker compose exec api cat /sys/fs/cgroup/cpu.stat   # nr_periods and nr_throttled both climbing
```

Run the ramp both ways. Unthrottled, this service's CPU sits well under one core
while p99 climbs — which is step 3's answer arriving from a different direction.

## Predict, then record

Predict **(a)** the VU count where p99 starts rising; **(b)** whether pool
utilization hits 100% before or after that; **(c)** at 10% sampling, is the
spanmetrics rate ~10x low, ~1x correct, or something else — and why?

| Observation | VU count / value |
|---|---|
| p99 first exceeds 500ms | |
| Pool utilization first hits 100% | |
| Checkout wait p99 first exceeds 100ms | |
| CPU utilization at that moment | |
| CFS throttled ratio at that moment | |
| spanmetrics rate ÷ SDK rate at 10% sampling | |

And the language ladder, from the same ramp against each runtime's pool:

| Runtime | What it gives you by default | Can you get a wait *percentile*? | Extra code needed |
|---|---|---|---|
| Python / SQLAlchemy | | | |
| Node.js / node-postgres | | | |
| Go / `database/sql` | | | |
| Java / HikariCP | | | |

**What would mean the experiment is broken rather than your prediction wrong:**

- If pool utilization never reaches 100%, check `max_overflow` — a nonzero overflow
  means the pool grows and you are testing a different system.
- If throttling never appears, confirm the quota actually applied (`docker stats`,
  and `cpu.max` *inside* the container) before concluding CFS does not throttle.
- If `cpu.stat` shows `nr_throttled` at zero while p99 climbs, the bottleneck is not
  CPU — that is a result, not a broken run, and it means step 3's pool is still the
  answer.
- If spanmetrics and SDK rates agree exactly, the sampler never reached the process;
  check the env var landed on `api` and not just on the compose file.
- If p99 rises the instant the ramp starts, your pool of 5 was already saturated at
  10 VUs and you have no baseline. Start the ramp lower. Expect to need this: with
  all five defects enabled, one `/orders` request holds a connection for a ~200ms
  sequential scan plus 25 N+1 round trips, so five connections are fully committed
  somewhere below 10 VUs and `ramp.js` as shipped starts *above* the knee. The first
  two rows of the table have no honest answer until you either lower `startVUs` or
  disable a defect to buy headroom — and noticing that you have no baseline is
  worth more than filling the rows in anyway.

## Answer before moving on

1. Give a concrete example from your own service where utilization looks fine and
   saturation is the whole story. Name the two metrics you would put side by side.
2. RED has no saturation term. Omission or deliberate scoping? Defend your answer
   against the opposite one.
3. Where should spanmetrics sit relative to tail sampling, and what does each
   ordering give you? Under which ordering is the disagreement with SDK RED a bug?
4. Go gives you pool wait as two cumulative counters and Java gives you a histogram.
   Name a question the histogram answers that the counters cannot, and one where the
   counters are sufficient and the histogram is just more storage.

## Next up

[Topic 6 — SLIs, SLOs and error budgets](../06-slos-and-error-budgets/README.md): you
can now see saturation before users do. Next is deciding, in advance and in
arithmetic, how much of it you are willing to spend.
