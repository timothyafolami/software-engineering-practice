# Layer 5 · Topic 1 — Utilisation, Little's Law, and the latency knee

### The takeaway (read this first)

**The one idea:** queueing delay is proportional to `1/(1−ρ)`, not to load —
latency does not degrade as you approach capacity, it goes vertical, and the
last 10% of headroom is worth more than the first 50%.

**Why it matters in practice:** this is the mechanism behind nearly every "it
was fine and then it wasn't" incident, the reason capacity plans built on
average CPU are wrong, and most likely the explanation for the latency
problem you have right now.

**You'll know it landed when:** given a service's concurrency limit and its
average service time you can state its maximum throughput out loud, and say
roughly what utilisation keeps p99 sane, without running anything.

## The concept

**Little's Law:** `L = λW` — the number of items in a system equals the
arrival rate times the time each one spends there. One line, it assumes
almost nothing (no distribution, no scheduling policy, only that the system
is *stable*), and all its power is in rearranging it.

- `λ = L/W`. If concurrency is capped at `L` — pool slots, threads,
  semaphore tokens, workers — and each unit of work occupies a slot for `W`,
  then maximum throughput is fixed and computable *before you benchmark
  anything*.
- `W = L/λ`. Measure in-flight count and arrival rate and you get latency for
  free — and if your measured latency disagrees with `L/λ`, one of your three
  numbers is lying. That disagreement is a finding, not noise.

**Work the arithmetic on your own stack once and you may not need the rest of
this layer.** With SQLAlchemy's documented `QueuePool` defaults —
`pool_size=5` plus `max_overflow=10` — a worker process gets **15 concurrent
database operations**, and `pool_timeout=30` means the 16th request waits up
to thirty seconds before it runs a query at all. At a 40ms query,
`λ_max = 15 / 0.040 = 375` rps per process, no matter how much CPU you add.
More worker processes multiply that ceiling, and multiply connections into
Postgres, whose documented `max_connections` default of 100 is the next
ceiling standing behind it.

**Why the curve bends.** For a single-server queue with random arrivals,
`W = S/(1−ρ)`, where `S` is service time and `ρ` is utilisation. Evaluate the
multiplier: 2 at ρ=0.5, 2.5 at 0.6, 5 at 0.8, 10 at 0.9, 20 at 0.95, 100 at
0.99. Read that as a sequence of equal ten-point traffic increases and the
shape jumps out: **going 50%→60% costs an extra 0.5× your service time
(2.0 → 2.5); going 90%→95% costs an extra 10× (10 → 20).** Same ten points of
traffic, twenty times the damage. That is why a service that was fine last
quarter is on fire this quarter with 20% more traffic and no code change.
Nobody broke anything — you walked up the wall of a hyperbola.

Nothing about your code changes at the knee. **The arithmetic of waiting
changes.**

**The knee is sharpest when there is one server.** With `c` servers sharing a
single queue the curve is far gentler until you are very close to capacity —
that is Erlang-C, and it is why a 20-thread pool forgives what a 1-thread
pool does not. Two consequences worth carrying: your cliff sits at whatever
resource has the **smallest count** (one shared pool, one lock, one leader),
and doubling a small count buys disproportionately more than doubling a large
one.

**Utilisation is not CPU.** ρ is "fraction of the constraining resource's
capacity in use", and the constraining resource is usually a *count*, not a
processor. If latency climbs while CPU sits at 40%, you are queueing on pool
slots, threads, semaphore tokens or workers. Find the smallest count in your
architecture diagram: that is your ceiling and your cliff.

**One Kubernetes-specific trap, because it defeats the CPU intuition
completely.** CFS throttling damage is proportional to the *number of 100ms
periods that hit quota*, not to average CPU — so a service averaging 40% CPU
can have a wrecked p99 because it stalled for milliseconds at the end of many
individual periods. If your production latency problem runs in Kubernetes,
read `container_cpu_cfs_throttled_periods_total` before you touch anything
else in this layer. On this machine that counter lives only inside the Docker
Desktop Linux VM; macOS has no cgroup v2 filesystem to read it from.

## How each language actually gets there

Six languages here, and they earn it: the whole topic is about *where your
runtime hands you an `L` you did not choose*, and every one of them hands you
a different set of them.

**Python** stacks its limits and most people know only one of them. From the
outside in: the kernel accept queue (`uvicorn --backlog`, default 2048 —
connections past the TCP handshake but not yet accepted, invisible to every
application metric you have); `--limit-concurrency` (default `None`, i.e. no
limit); AnyIO's default thread limiter of **40 tokens**, which is the real cap
on `def` endpoints because Starlette runs synchronous handlers in a
threadpool; then the SQLAlchemy pool at 15. Smallest wins, and for a FastAPI
service doing real database work it is the pool. The one to internalise is
the backlog, because a request that spent four seconds queued in the kernel is
already late when your first line of Python runs, and no middleware you write
can see it.

**Node.js** has an `L` of **1** for CPU work, by construction — one thread
runs your JavaScript, so any handler that computes rather than awaits
serialises everything behind it. Its saturation signal is therefore not CPU
percent but **event loop lag** (`perf_hooks.monitorEventLoopDelay()`), which
is a direct measurement of queue wait for the single server. Underneath sit
two counts you did not set: libuv's thread pool (default 4, `UV_THREADPOOL_SIZE`)
serving `fs`, DNS and `crypto`, and `node-postgres`' pool `max` (default 10).
A Node service can be at 5% CPU and completely saturated; the number that
tells you is lag.

**Go** differs in the most instructive way. Goroutines are cheap enough that
nobody caps them, `net/http` spawns one per connection with no ceiling, and
`database/sql`'s `MaxOpenConns` default is **unlimited** (`0`). So the queue
does not disappear — it **relocates**, out of your process and into Postgres,
where waiting is replaced by `FATAL: sorry, too many clients already` and the
failure stops being a latency problem and becomes an availability one.
`SetMaxOpenConns` is the difference between a queue you can see and one you
cannot, and it is a buffered handoff underneath — the same shape as every
other pool here. Zero queue wait inside your own process is not good news.

**Rust** gives you no ambient limit at all: tokio spawns tasks with no cap, so
`L` is whatever *you* construct — a `Semaphore`, a bounded `mpsc`, or a pool
crate's explicit `max_size`. This is the clearest language for the topic
precisely because nothing is implicit; you cannot inherit a bad default you
never read. With one exception worth knowing: the blocking pool
(`spawn_blocking`) defaults to a maximum of 512 threads, which is a hidden `L`
big enough that you will hit memory or the database before you hit it.

**C++** is the only language here with nothing between you and the kernel, so
the queue stops being a metaphor: `listen(fd, backlog)` *is* the accept queue,
`SOMAXCONN` bounds what the kernel will honour, and your worker pool is a
`std::deque` you wrote and can therefore timestamp on both ends. That makes it
the best place in this layer to measure **queue wait directly** — enqueue
time minus dequeue time, with no framework in the way — and to see that the
`1/(1−ρ)` curve is a property of the queue, not of any runtime's cleverness.

**Java** stacks two counts, and most teams tune the wrong one. The servlet
container's thread pool (Tomcat's `maxThreads`, 200 by default under Spring
Boot) is the one people raise; HikariCP's `maximumPoolSize` (default 10) is
almost always the smaller number and therefore the actual ceiling. Java 21's
virtual threads make this sharper rather than softer: switch the container to
virtual threads and the thread count stops being a constraint entirely,
leaving the connection pool as the *only* bound — so the knee gets **more**
abrupt, because you removed the gentler multi-server queue in front of it.
Predict that before you run it; it is the least intuitive result in this topic.

## The experiment

Every language builds the same sweep: an **open-model** load generator
(Poisson arrivals at a fixed rate, which does *not* wait for a response before
sending the next request), a handler that holds a genuinely bounded resource
for a controlled service time `S`, and instrumentation for arrival rate `λ`,
in-flight gauge `L`, queue wait, and a latency histogram `W`.

Python does it against the real thing — FastAPI + uvicorn + SQLAlchemy + a
real Postgres pool, with `/work` running `pg_sleep()` plus a real query inside
a pooled connection, so a request that cannot get a slot really does queue.
The others isolate the same mechanism with the runtime's own bounding
primitive.

1. **Measure capacity.** Ramp until throughput plateaus. The plateau should
   equal `pool_total / service_time` — Little's Law rearranged, checked.
2. **Sweep ρ** from 0.2 to 1.1 in steps of at least 30 seconds each.
3. **Record per step:** λ achieved, p50, p99, in-flight `L`, and pool
   checkout wait.
4. **Plot p50 and p99 against ρ**, and overlay the predicted `S/(1−ρ)`.
5. **Verify Little's Law numerically:** does measured `L` equal `λ × W`?
6. **Change only `pool_size`** and rerun — the standalone programs sweep 5
   then 10. Capacity and the knee should move proportionally: the cliff
   follows the smallest count.

Output shape, so you know what you are looking at (fill in your own numbers —
the `S/(1−ρ)` column is arithmetic, not measurement):

```
pool=5  S=40ms  lambda_max = 5/0.040 = 125 rps
rho=0.20  lambda=<achieved rps>  p50=<ms>  p99=<ms>  L=<in-flight>  wait=<ms>  predicted W = 1.25*S
rho=0.90  lambda=<achieved rps>  p50=<ms>  p99=<ms>  L=<in-flight>  wait=<ms>  predicted W = 10*S
```

## How to run

Each language's sweep is self-contained:

```
python3 python/latency_knee.py
node nodejs/latency_knee.js
cd golang && go run latency_knee.go
cd rust/latency_knee && cargo run --release
c++ -O2 -std=c++17 -pthread -o /tmp/latency_knee cpp/latency_knee.cpp && /tmp/latency_knee
cd java && javac LatencyKnee.java -d /tmp/javabuild && java -cp /tmp/javabuild LatencyKnee
```

Everything here runs natively on macOS 27 / arm64, and every sweep takes two
to four minutes: twelve measured steps of twelve seconds, twice.

The Python version is the only one with dependencies — it drives the real
stack — so install them first, and give it a Postgres to talk to:

```
python3 -m pip install -r python/requirements.txt   # fastapi, uvicorn, sqlalchemy, asyncpg, psycopg
pg_isready                                          # it creates failure_lab; dropdb failure_lab when done
```

For the full stack version — real HTTP, real k6, real plots — use the shared
harness described in [`../lab/README.md`](../lab/README.md).

**Built, and executed on this machine.** The shared harness exists —
`lab/docker-compose.yml`, `lab/app/`, `lab/scripts/*.js`, `lab/tools/*.py`,
specified in [`../lab/README.md`](../lab/README.md) — and the commands below
were run against it. You do **not** need to install `k6`: it runs from the
`grafana/k6` image, which is what `docker compose run --rm k6` starts. What
you do need is Docker running (`docker info`) and host ports 8000-8003 free —
if something else on your machine holds 8000, `up` fails with `port is
already allocated`. From `05-failure/lab/`:

```
cd ../lab
docker compose up -d --build
docker compose run --rm k6 run /scripts/01_ramp.js --out csv=/out/ramp.csv
python3 tools/plot_knee.py out/ramp.csv

# step 6 — change ONLY pool_size and rerun
docker compose run --rm k6 run /scripts/01_ramp.js -e POOL_SIZE=10 \
  --out csv=/out/ramp_pool10.csv
python3 tools/plot_knee.py out/ramp_pool10.csv
```

The plotter itself runs today, against a synthetic CSV that ships with the
harness — it is a model, not a measurement, and exists only so a broken
plotting script is found by running it:

```
cd ../lab && python3 tools/make_fixtures.py
python3 tools/plot_knee.py out/fixtures/ramp.csv
```

## Predict, then record

Write these down **before** you run anything, in
[`PREDICTIONS.md`](../../PREDICTIONS.md).

- At what ρ does p99 reach 2× its ρ=0.2 value?
- What is p99/p50 at ρ=0.5, and at ρ=0.9?
- Does measured `L` match `λ × W`? If not, which of the three numbers do you
  trust least, and why that one?
- Doubling `pool_size`, does p99 at a *fixed absolute* rps improve — and what
  is the mechanism for your answer?
- Java only: does switching the container to virtual threads make the knee
  sharper or gentler?

| ρ | λ (rps) | p50 | p99 | in-flight L | λ×W | pool wait (ms) |
|---|---|---|---|---|---|---|
| 0.2 | | | | | | |
| 0.5 | | | | | | |
| 0.8 | | | | | | |
| 0.9 | | | | | | |
| 0.95 | | | | | | |
| 1.1 | | | | | | |

**What would mean the experiment is broken, not the prediction wrong:**

- **p99 rises linearly with no knee at all.** You are closed-loop. Check that
  the generator is `constant-arrival-rate`/`ramping-arrival-rate` and not
  `ramping-vus` — with fixed virtual users the generator slows itself down
  whenever the server slows, erasing exactly the effect you came to measure.
  Topic 6 is entirely about this.
- **Throughput never plateaus.** Your handler is not holding the resource you
  think it is. `asyncio.sleep()` in place of a real query measures the event
  loop, not the pool.
- **p99 is already vertical at ρ=0.2.** Capacity was measured wrong. Pool
  checkout wait should be approximately zero at 20%; if it is not, you were
  never at 20%.
- **`L` is far from `λ×W`.** Your gauge counts something your histogram does
  not time — typically it excludes time spent queued for a worker. Do not
  discard this: it is the finding. A dashboard is lying to you about where
  time goes.
- **Go only: pool wait is zero at every rate and there are connection
  errors.** Not a broken run — that is the unbounded-`MaxOpenConns` result,
  and it is the topic's second lesson. The queue moved to Postgres.

## Answer before moving on

1. Four uvicorn workers, `pool_size=5`, `max_overflow=10`, 25ms queries, and
   a `max_connections=100` shared with three other services. What is the
   maximum sustainable rps, and which number binds first? What changes if you
   put PgBouncer in transaction mode in front of it?
2. `1/(1−ρ)` says latency is infinite at ρ=1. Real systems do not return
   infinite latency. What do they do instead, and which of those outcomes do
   you actually want?
3. Little's Law holds for any *stable* system. What exactly does "stable"
   exclude, and what does that imply about every measurement you take while
   arrivals exceed capacity?
4. Why does adding a second replica sometimes cut p99 by far more than half?
   (Your answer should mention Erlang-C, not just "more capacity".)

## Next up

[Topic 2 — Timeout budgets and deadline propagation](../02-timeout-budgets-and-deadline-propagation/README.md):
what happens to all of this when the work in flight belongs to a caller who
already gave up.
