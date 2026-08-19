# Layer 10 · Topic 3 — Little's Law, Kingman, and why independent p99s compound

### The takeaway (read this first)

**The one idea:** `L = λW` is an identity, not a model. It holds for any
stable system with no assumption whatsoever about arrival or service
distributions, so any two of {concurrency, throughput, latency} pin the
third. Most capacity questions are therefore one line of arithmetic, and
a pool size or batch cap you cannot derive from that line is a guess.

**Why it matters in practice:** this is the layer's most direct hit on
the latency problem in production right now. A FastAPI service with a
20-connection pool and a 50 ms query cannot exceed 20 ÷ 0.05 = **400
req/s** — not "probably shouldn't", *cannot* — and at 380 req/s it sits
at ρ = 380 × 0.05 / 20 = 0.95, where waiting time multiplies by roughly
1/(1−ρ) = 20x. Nothing in your code changed. You crossed a number.

**You'll know it landed when:** you can size a connection pool, a thread
pool, and a model server's max-batch from measured λ and W without
running a load test — and then use the load test only to check the
arithmetic.

**Where this topic sits.** [`05-failure` topic
1](../../05-failure/README.md) owns utilisation and the latency knee as a
subject, and [`02-network` topic
2](../../02-network/02-connection-pooling-and-pool-exhaustion/README.md)
owns pool exhaustion as a mechanism. Go there for the general treatment.
This topic teaches only the delta: what changes when the thing on the
other end is a *model server* rather than a database, which is entirely a
story about service-time variance.

## The concept

### Little's Law, applied recursively

```
L = λ W          L = items in system, λ = arrival rate, W = time in system
```

The power is that it applies to any boundary you can draw. Apply it to
the whole service, then to the DB pool alone, then to the model server's
running batch, and each application pins a different number:

```
with c servers (pool slots, workers, batch slots):
  utilisation   ρ = λ W_service / c
  max throughput  = c / W_service          (at ρ = 1, where W explodes)
```

Two worked cases, both pure arithmetic from that formula:

```
pool c=20, query W=50 ms   → max λ = 20 / 0.05  = 400 req/s
model max-batch c=16, mean generation W=4 s → max λ = 16 / 4 = 4 req/s
```

The second one is the number that surprises people. A model server that
looks idle in `nvidia-smi` can be at ρ = 0.9 on its *batch slots*, and
batch slots are the resource that runs out.

### Kingman, and why LLM traffic is different

Little's Law tells you where the wall is. **Kingman's approximation**
tells you how the approach to it feels, and it is the formula to memorise
after Little's:

```
Wq ≈ ( ρ / (1−ρ) ) × ( (c_a² + c_s²) / 2 ) × τ
```

τ is mean service time; `c_a` and `c_s` are the coefficients of variation
(σ/μ) of interarrival time and of service time. Read it as two
independent multipliers:

- **The 1/(1−ρ) wall.** At ρ = 0.5 the first factor is 1; at 0.9 it is 9;
  at 0.95 it is 19; at 0.99 it is 99. This is why "we're only at 90% CPU"
  is not the reassurance people intend.
- **Variability.** A uniform 50 ms indexed query has `c_s` ≈ 0, which
  drives the second factor toward 0.5 and makes queueing almost invisible
  until ρ is very close to 1. LLM generation where one request emits 20
  tokens and the next emits 2000 can easily have `c_s` > 1, doubling the
  queueing term at *identical* utilisation.

That is the mathematical statement of "LLM serving needs admission control
that CRUD serving didn't." It is not a vibe about AI workloads; it is one
term in a formula, and you can measure `c_s` on your own traffic in an
afternoon: log per-request service time, take σ/μ.

### Tail compounding under fan-out

If one request depends on *n* independent backends each with p99 = T,
then the probability that at least one of them is in its slow tail is

```
P(at least one slow) = 1 − (1 − 0.01)^n

n = 10   → 1 − 0.99^10  ≈ 9.6%
n = 100  → 1 − 0.99^100 ≈ 63%
```

At n = 100, *your* p99 has become *their* p63. Dean & Barroso's **The Tail
at Scale** (CACM, 2013) is still the primary source, and its fixes are
still the fixes: hedged requests issued after the p95, tied requests,
and a hard budget on how many extra requests hedging may issue.

Reality is worse than that arithmetic, and knowing why is the point.
Independence is the *optimistic* assumption: fan-out calls share queues,
share a network, share a GC pause, share a noisy neighbour, so tails
correlate. The independence math gives you a **floor**, not an estimate.
When your measured fan-out p99 is worse than the independence prediction,
that is not an error — it is the correlation, and it is quantifiable.

## How each language actually gets there

All six, and this is the topic that earns it. The mechanism *is* the
runtime: "how many requests may be in flight, and what happens to the
ones that can't be" is a data structure inside each of these, and six
runtimes made six incompatible decisions about it.

**Python.** The pool *is* the concurrency limit. SQLAlchemy async gives
you `pool_size` **plus** `max_overflow`, and the effective `c` in Little's
Law is their sum — the single most common reason a measured knee lands
somewhere other than where the arithmetic said. asyncpg has its own
`min_size`/`max_size`. The thing almost nobody instruments is time spent
*waiting to acquire* a connection, separate from query execution time,
which is exactly where the latency in your production service is hiding.
Deadlines are not ambient: `asyncio.timeout()` around the call, propagated
by hand.

**Node.js.** One pool per *process*, and process count is set by your
container CPU limit, so the real `c` is `pool_size × workers` and changing
either silently changes your capacity. `pg`'s `max` defaults to 10. The
interaction with cgroup CPU quota is where Node services surprise people:
under CFS throttling, the event loop stalls in bursts, which inflates `W`
without any query being slower — see
[`01-machine/07`](../../01-machine/07-inside-a-container/README.md).

**Go.** Genuinely different, and worth one dedicated experiment.
`database/sql` has `SetMaxOpenConns` — that is the `c` — *and*
`SetMaxIdleConns`, whose default of 2 quietly turns a high-throughput
service into a connection-churn machine: connections are opened, used,
and closed rather than returned, so you pay setup cost on most requests
and Postgres sees a connection storm. `context.Context` deadlines are in
every call signature, which is the design `asyncio.timeout` is imitating,
and cancellation actually reaches the driver.

**Rust.** `sqlx`/`deadpool` make the pool explicit and typed, and the
tokio semaphore underneath is the same object in a different costume. The
interesting difference is that a dropped future *is* a cancelled query, so
a client timeout releases the pool slot immediately and correctly — no
leak, no `CancelledError` handling. What Rust does not give you is an
ambient deadline: you compose one from `tokio::time::timeout` and pass it
down yourself.

**C++.** No standard pool, so whatever you have is one somebody wrote —
usually a `std::condition_variable` over a fixed-size free list. Written
that way, `c` is explicit and the wait is visible in the source, which
makes it the best language to *read* to understand what the other five
are doing. It is also the one with no cancellation: a request that gave
up still holds its slot until the query returns.

**Java.** HikariCP is the reference implementation of this entire idea,
and its documentation argues the Little's Law case better than most
textbooks: pools should be small, and `maximumPoolSize` is the `c` you
plug into the formula. Virtual threads (Java 21+) move the queue rather
than removing it — a million virtual threads can all block on a
20-connection pool, which turns "thread starvation" into "pool wait" and
makes the *queue depth metric* the thing you must now watch instead of
thread count. That relocation is the single best illustration in this lab
that concurrency limits are conserved, not eliminated.

## The experiment

**(a) Connection pool exhaustion, replayed then fixed.** FastAPI +
SQLAlchemy async + Postgres from [`../lab/README.md`](../lab/README.md);
one endpoint, one indexed query. Instrument **three timers separately**:
pool acquire wait, query execution, total handler time. Drive it with a
k6 arrival-rate ramp. Predict the saturation λ from `L = λW` first.

What you should see is total latency going vertical while *query time
stays flat* — the entire increase is acquire wait. That single graph is
the topic. Then fix it the way a real PR would:

- pool sized from the arithmetic rather than from a copied config,
- a per-request deadline budget propagated into the DB call
  (`asyncio.timeout` plus a Postgres `statement_timeout`, so the DB stops
  working on abandoned queries too),
- `503` with `Retry-After` instead of unbounded queueing.

Re-run. The knee moves, and the failure becomes explicit rather than
silent — which is the actual improvement, not the extra throughput.

**(b) Fan-out tail compounding, then hedging.** The gateway makes n
parallel calls to the model server (n = 1, 2, 5, 10, 20). Measure
end-to-end p99 at each n, and compare against the independence prediction
computed from the *single-call* distribution you measured at n = 1. Then
hedge: fire a second copy of any call still outstanding at the measured
p95, cancel the loser, and cap hedges at 5% of requests with a token
bucket. Measure again, and record the extra load the hedging cost you —
a hedge with no budget is a retry storm with better manners.

**(c) The delta that makes this topic Layer 10 rather than Layer 5.**
Measure `c_s` for both backends: σ/μ of service time for the Postgres
query, and for model generation. Then run both at the same ρ and compare
queue delay. Kingman predicts the ratio; you are checking whether it
does.

## How to run

**Two halves, and only one needs Docker.** The per-runtime programs and the
fan-out arithmetic run on the host with no database and no containers; the
service experiment needs the compose stack.

Each language ships one program: Part 1 is the same experiment everywhere —
open-loop Poisson arrivals against that runtime's own limiting primitive,
with acquire wait, service time and total time timed separately — and Part 2
is the trap specific to that runtime. Each takes 20-30 seconds and no
arguments, except `python/pool_queueing.py`, which takes about 90 seconds
because its Kingman arm runs each service-time distribution for 30 seconds
— the mean wait of a high-variance queue is a heavy-tailed average, and a
short window under-reads it badly enough to make Kingman look wrong:

```
python3 python/pool_queueing.py     # + the Kingman variance arm (c_s = 0 vs 1)
node    nodejs/pool_queueing.js     # one pool per process: c = pool_size x workers
cd golang && go run pool_queueing.go && cd ..     # SetMaxIdleConns, the churn knob
cargo run --release --manifest-path rust/pool_queueing/Cargo.toml   # spawn vs drop
c++ -O2 -std=c++20 -pthread -o /tmp/pool_cpp cpp/pool_queueing.cpp && /tmp/pool_cpp
cd java && javac PoolQueueing.java -d /tmp/javabuild && \
  java -cp /tmp/javabuild PoolQueueing && cd ..    # virtual threads move the queue
```

The Kingman arm lives only in `python/pool_queueing.py`: the
`(c_a² + c_s²)/2` factor is arithmetic about distributions and is not a
property of any runtime, so implementing it six times would measure the
same thing six times. What *is* a property of the runtime is Part 2 of each
file, and those are six genuinely different findings.

Fan-out tail compounding and hedging — a Monte Carlo over a service-time
distribution with a real tail, which gives you the prediction to check the
real run against:

```
python3 python/fanout_hedging.py
```

Then the service experiment, which needs the stack:

```
cd ../lab
docker compose up -d db api
curl -s localhost:8001/healthz          # pool geometry and the derived λ_max
docker compose --profile load run --rm k6 run /scripts/pool_ramp.js -e RATE=50
docker compose --profile load run --rm k6 run /scripts/pool_ramp.js -e RATE=200
docker compose --profile load run --rm k6 run /scripts/pool_ramp.js -e RATE=400 -e DIST=exp
docker compose --profile load run --rm k6 run /scripts/fanout.js -e N=1
docker compose --profile load run --rm k6 run /scripts/fanout.js -e N=10

# the three timers, from the side that can see the acquire/query split
curl -s localhost:8001/metrics | grep -E 'api_(acquire|query|request)_seconds_(sum|count)'

# what the database thinks is happening, from the other side
docker compose exec db psql -U app -c \
  "select wait_event_type, wait_event, count(*) from pg_stat_activity group by 1,2;"
docker compose exec db psql -U app -c "show max_connections;"
```

`POOL_PROFILE=default|sized|budgeted` on the `api` service selects the
before/after variants; `/healthz` prints the effective `c` and the `λ_max`
it implies, so you can check the arithmetic before generating any load.
Record the toolchain version next to any number these programs produce.

## Predict, then record

- W_query = ___ ms, pool c = ___, so saturation λ = ___ req/s.
- At 0.9x that λ, acquire wait will be ___ ms and query time will be
  ___ ms.
- Measured `c_s` for the DB query: ___. For model generation: ___.
- For n = 20 with single-call p99 = ___ ms, the independence prediction
  is ___ ms; measured will be ___% higher.
- Hedging at p95 will cut fan-out p99 by ___% at a cost of ___% extra
  requests.

| λ | acquire wait p50/p99 | query p50/p99 | total p99 | pg active conns | 5xx/429 |
|---|---|---|---|---|---|
| | | | | | |

| n | predicted p99 (independence) | measured p99 | with hedging | extra req % |
|---|---|---|---|---|
| | | | | |

**What would mean the experiment is broken rather than your prediction
wrong:**

- **Latency never goes vertical.** The pool is bigger than you think —
  `max_overflow` silently adds to `pool_size`, and Postgres
  `max_connections` may be the real ceiling — or requests are failing
  fast and you are histogramming only successes.
- **Acquire wait stays at zero while total climbs.** You are timing the
  wrong thing. Most ORMs acquire lazily at first *query*, not at session
  creation, so a timer wrapped around session creation measures nothing.
- **Fan-out p99 exactly matches the independence prediction.** Usually it
  should not, because the calls share a queue. Check they actually ran
  concurrently (`asyncio.gather`, not a loop of awaits) — sequential
  calls produce a *sum*, which can coincidentally sit near the
  independence number and look like a confirmation.
- **Hedging makes p99 worse.** Plausible and not necessarily a bug: if
  the backend is saturated, hedges add load exactly where there is none
  to spare. Check the hedge rate stayed under budget; if it did, you have
  found the real result, which is that hedging is a *latency* fix and not
  a *capacity* fix.
- **`c_s` near zero for model generation.** Your prompts all produce
  similar-length outputs, so you built a CRUD workload with a model
  attached. Vary output length deliberately before concluding LLM traffic
  is well-behaved.

## Answer before moving on

1. `L = λW` needs no distributional assumptions, but Kingman is an
   *approximation*. State exactly what it assumes, and name a real
   workload where it is badly wrong.
2. A colleague proposes fixing pool-wait latency by raising `pool_size`
   from 20 to 200. Give the arithmetic for what that does to throughput,
   and then the reason it will probably make the p99 worse anyway.
3. Java's virtual threads let a million requests block on a
   20-connection pool without exhausting threads. Which term of Little's
   Law changed, which did not, and what metric must you now alert on that
   you did not need before?
4. You measure fan-out p99 at n=20 as 2.5x the independence prediction.
   Design the follow-up experiment that distinguishes "shared queue" from
   "correlated slow node" as the cause.

## Next up

[Topic 4 — Quantization, numerical stability, and the determinism you
didn't have](../04-quantization-and-determinism/README.md). Batch size
turned out to set your latency. It also, unexpectedly, sets your
*output* — the same prompt at temperature 0 returns different tokens
depending on who else is on the server.
