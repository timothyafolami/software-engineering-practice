# Layer 2 · Topic 2 — Connection pooling and pool exhaustion

### The takeaway (read this first)

**The one idea:** a connection pool is a queue with a hard capacity, and when
demand exceeds capacity your requests do not fail — they *wait*, in a place
your application metrics do not look. Pool exhaustion is how a service stops
responding while every dependency it talks to reports itself perfectly
healthy.

**Why it matters in practice:** you have two pools stacked on top of each
other (HTTP client → upstream, SQLAlchemy → Postgres) plus a third you never
configured (uvicorn workers). The narrowest one sets your real concurrency,
and by Little's Law that limit divided by your latency sets your throughput
ceiling. This is a live suspect in the latency problem that
[`SEQUENCE.md`](../../SEQUENCE.md) is built around.

**You'll know it landed when:** given pool sizes and p50 upstream latency you
can compute the service's ceiling in requests per second on the back of a
napkin, and say which pool saturates first.

**Where this topic sits.** [`05-failure`](../../05-failure/README.md) topic 1
is the canonical owner of pool exhaustion and the Little's Law framing for the
whole lab — go there for the general model and for the admission-control
treatment. This topic teaches the *delta*: the HTTP-client variant, where the
same condition produces three incompatible behaviours in three libraries you
have all three of installed right now. Do not rebuild the compose stack for
this; extend [`../lab/`](../lab/README.md).

## The concept

**Little's Law**: `L = λ × W`. Rearranged for a pool: a pool of `N`
connections, each held for `W` seconds, sustains `N / W` requests per second
and not one more.

Derive it for the SQLAlchemy defaults, because the arithmetic is the whole
point:

```
pool_size = 5, mean query time W = 50 ms
    ceiling = 5 / 0.050 = 100 queries/sec

add max_overflow = 10  →  15 connections available
    ceiling = 15 / 0.050 = 300 queries/sec
```

Past that, requests do not error. They queue for `pool_timeout` — 30 seconds
by default — and only then raise
`TimeoutError: QueuePool limit of size 5 overflow 10 reached`. Thirty seconds
is roughly forever from the caller's point of view, so the observable symptom
is a hang, not an error, which is why this incident is always reported as
"the service is slow" and never as "the pool is full".

The stacking is what makes it hard to see:

```
k6 (open model, 500 rps)
  └─ uvicorn workers          ← limit #1
       └─ httpx pool          ← limit #2 (max_connections=100)
            └─ SQLAlchemy     ← limit #3 (pool_size=5 + overflow=10)
                 └─ Postgres  ← limit #4 (max_connections, shared with everything else)
```

Latency added at the bottom propagates upward as *queueing* at every layer
above. Run the arithmetic again with `W` doubled to 100 ms and the ceiling
halves to 150 rps — so "the database got a bit slower" and "the service is
hanging" are the same event, and the second one arrives without warning
because a queue is smooth right up to the point where it is not.

The other half of the concept is what happens *at* the ceiling. A service
that queues has an unbounded p99 and looks healthy on every dashboard until it
falls over. A service that **sheds** — refuses work with a 503 the moment
in-flight requests exceed a threshold it knows it can serve — has a bounded
p99 and a visible error rate. The error rate is the feature. It is the only
signal that says "we are at capacity" while there is still time to act.

## How each language actually gets there

Six runtimes, six different decisions about what "the pool is full" means.
That disagreement is the lesson, so all six are here.

**Python — memorise this table, because the three clients disagree in ways
that change the shape of your incident:**

| | default pool | per-host | when full |
|---|---|---|---|
| `requests` (urllib3 `HTTPAdapter`) | `pool_maxsize=10` | 10 pools cached | **silently discards** the extra connection, warning `Connection pool is full, discarding connection` — no backpressure, unbounded new sockets |
| `httpx` | `max_connections=100`, `max_keepalive_connections=20`, `keepalive_expiry=5.0` | shared | **blocks**, then raises `PoolTimeout` |
| `aiohttp` (`TCPConnector`) | `limit=100` | `limit_per_host=0` — **unlimited** | waits on a per-host futures queue |

These are the documented defaults; verify them for your installed versions
before recording a number against them —
`python -c "import httpx; print(httpx.Limits())"` takes two seconds.

Three libraries, three failure modes for one condition. `requests` fails
*open*: it will cheerfully create thousands of sockets and hit your file
descriptor limit, which is Layer 1 Topic 6's "too many open files" arrived at
by a completely different road. `httpx` fails *closed*, with a clean typed
error. `aiohttp` has no per-host bound unless you set one, so a single slow
host can consume the entire global limit of 100 and starve every other
dependency you have.

SQLAlchemy's defaults — `pool_size=5, max_overflow=10, pool_timeout=30,
pool_recycle=-1, pool_pre_ping=False` — are sized for a desktop script. And
**each worker process gets its own pool**: four uvicorn workers × 15 = 60
Postgres connections from one container, against a server whose default
`max_connections` is 100 and is shared with your migrations, your admin
sessions and every other service. That arithmetic is what produces
`FATAL: sorry, too many clients already` at 3am.

**Node.js.** `pg.Pool` defaults to `max: 10` with `connectionTimeoutMillis: 0`
— which means *wait forever*, the same unbounded wait as SQLAlchemy's 30
seconds but with no upper bound at all. Node's second pool is invisible: an
undici `Pool` queues requests per origin, and that queue depth is not a metric
anyone exports by default, so a saturated HTTP client in Node looks exactly
like a slow upstream.

**Go.** `sql.DB.SetMaxOpenConns` defaults to **unlimited**. Go fails open at
the database layer where Python fails closed, and unlimited is not a kindness
— it relocates the failure from your process, where you could shed, into
Postgres, where you cannot. `http.Transport.MaxConnsPerHost` is likewise 0
(unlimited) by default; the only bounded thing is idle connections. The Go
lesson is that "no configured limit" is itself a configuration choice, and it
chooses which component dies.

**Rust.** There is no ambient pool. `bb8` and `deadpool` give you one, and
both make you name a size and a wait behaviour at construction, so the
question "what happens when it is full" cannot be left unanswered. More
interesting for teaching: model the pool as a `tokio::sync::Semaphore` and the
checked-out connection as an owned permit. Now "who is holding a connection"
is a value with a lifetime the compiler tracks, and the classic leak — an
early `return` on an error path that never releases the connection — becomes
structurally impossible rather than a code-review item. That is the
compile-time-enforcement contrast this lab keeps coming back to, applied to a
resource that is not memory.

**C++.** There is no pool, there is no queue, and there is no timeout unless
you wrote one. Hand-rolling it — a `std::condition_variable`, a bounded deque,
a `wait_for` with a deadline — takes about forty lines and forces you to make,
explicitly, every decision the other five runtimes made for you: bounded or
unbounded, block or fail, timeout or wait forever, LIFO or FIFO handout. Do it
once and every other library's defaults stop being arbitrary.

**Java.** HikariCP is the pool most of the JVM world runs, and its defaults are
the sanest in this list: `maximumPoolSize=10`, `connectionTimeout=30000` ms,
and — the part that matters — it exports pool metrics (active, idle, pending)
as first-class instrumentation, so the queue this whole topic is about is
actually visible. The 2026 wrinkle is virtual threads: on a platform-thread
executor the thread pool was an accidental second limiter that partly hid pool
exhaustion, and with `Executors.newVirtualThreadPerTaskExecutor()` that
limiter disappears. Every request now reaches the connection pool at once, so
the pool becomes the *only* ceiling and exhaustion arrives faster and cleaner
than it used to. Cheap threads do not create capacity; they just stop
concealing where the capacity actually ends.

## The experiment

`lab/topic2/` — `api` exposes `/checkout`, which makes one call to `upstream`
and two queries against `db`. Toxiproxy adds 100 ms to the database path so
`W` is a known constant. k6 ramps arrival rate from 50 to 600 rps over five
minutes, open model.

Export, per second: in-flight requests, pool checkouts, pool waits and wait
duration. From inside `db`, `SELECT count(*), state FROM pg_stat_activity
GROUP BY state`. From inside `api`, `ss -tan` counts.

Four configurations, selected with `POOL_PROFILE`:

| `POOL_PROFILE` | What it is |
|---|---|
| `default` | SQLAlchemy and httpx defaults, untouched |
| `wide` | `pool_size=20, max_overflow=10` |
| `fast_timeout` | defaults, but `pool_timeout=2` |
| `shed` | defaults, plus a bounded queue that returns 503 immediately once in-flight requests exceed a threshold |

The fourth is the point of the topic. The first three move the knee around;
only the fourth changes the *shape* of the failure.

The single-file programs make the same points without the stack:
[`python/pool_full_behaviour.py`](python/) puts the three clients under
identical overload and prints what each does;
[`python/littles_law_and_shedding.py`](python/) is the arithmetic, executable;
[`golang/fails_open_by_default.go`](golang/) shows the unlimited default
reaching Postgres; [`nodejs/agent_queue_is_invisible.js`](nodejs/) shows a
queue with no metric on it; [`rust/pool_permit/`](rust/) is the semaphore
permit version; [`cpp/hand_rolled_pool.cpp`](cpp/) is the forty-line pool with
every one of those decisions made explicitly, run under one overload at four
policies; [`java/PoolCeilingVsThreads.java`](java/) runs the same pool behind
platform threads and virtual threads.

## How to run

```
cd 02-network/lab
POOL_PROFILE=default docker compose up -d api db toxi
docker compose run --rm load run /scripts/topic2.js
docker compose exec db psql -U app -c "select state, count(*) from pg_stat_activity group by 1;"
```

Repeat with `POOL_PROFILE=wide`, `POOL_PROFILE=fast_timeout`,
`POOL_PROFILE=shed`. Service list and pre-flight checks:
[`../lab/README.md`](../lab/README.md). `curl -s localhost:8000/stats` reports
`pool_waits`, `pool_wait_seconds`, `inflight_max` and `shed` — the queue this
topic is about, exported on purpose because nothing exports it by default.

The single-file programs, from this topic's directory:

```
python3 python/pool_full_behaviour.py && python3 python/littles_law_and_shedding.py
node nodejs/agent_queue_is_invisible.js
cd golang && go run fails_open_by_default.go
cd rust/pool_permit && cargo run --release
c++ -O2 -std=c++17 -pthread -o /tmp/handpool cpp/hand_rolled_pool.cpp && /tmp/handpool
cd java && javac PoolCeilingVsThreads.java -d /tmp/javabuild && \
  java -cp /tmp/javabuild PoolCeilingVsThreads
```

`python/pool_full_behaviour.py` needs `requests`, `httpx` and `aiohttp`
(`pip install -r python/requirements.txt`); everything else is standard
library.

## Predict, then record

- Arrival rate at which p99 goes vertical, default pools: ______ rps
- Little's Law ceiling, `(pool_size + overflow) / W`: ______ rps
- Does the *error* rate or the *latency* move first? ______
- Under `shed`, what does p99 do as arrival rate keeps climbing? ______

| Config | knee (rps) | p99 at knee | p99 at 2× knee | errors at 2× knee | max pg conns |
|---|---|---|---|---|---|
| default | | | | | |
| wide | | | | | |
| fast_timeout | | | | | |
| shed | | | | | |

| Client | behaviour when full | sockets opened | errors seen | error type |
|---|---|---|---|---|
| requests | | | | |
| httpx | | | | |
| aiohttp | | | | |
| Go database/sql | | | | |
| Node pg.Pool | | | | |
| Java HikariCP | | | | |

**What would mean the experiment is broken, not the prediction wrong:**

- The knee does not move when you quadruple the pool → a *different* pool
  binds. Check uvicorn worker count and httpx limits before concluding that
  pool sizing does not matter.
- No queueing at any arrival rate → your generator is closed-model. A fixed VU
  count self-throttles and cannot exhaust a pool. Confirm the k6 scenario says
  `constant-arrival-rate`.
- Latency rises smoothly with no knee at all → you never crossed capacity.
  Raise the rate; if CPU saturates first, lower the injected database latency
  instead so the ceiling arrives before the CPU does.
- Postgres connection count exceeds workers × (pool + overflow) → something is
  bypassing the pool: a raw `psycopg.connect`, a migration tool, a health
  check with its own engine. Find it. That is a real bug you just discovered
  by accident, and it is worth more than the table.
- `shed` returns 503s from the very first request → your threshold is below
  your steady-state in-flight count. Measure in-flight at low rate first, then
  set the threshold above it.

## Answer before moving on

1. Derive the ceiling for four workers, `pool_size=5`, `max_overflow=10`, and
   a 40 ms mean query. Now the database is twice as slow. Give the new ceiling
   and say what happens to requests still arriving at the old rate.
2. `requests` discards excess connections instead of blocking. Name a
   production failure that `httpx`'s blocking behaviour *prevents*, and a
   different production failure that it *causes*.
3. Why does raising `pool_size` sometimes make an incident strictly worse?
   Think about what is on the other end of those connections.
4. Java's virtual threads removed an accidental limiter and made pool
   exhaustion arrive sooner. Argue that this is an improvement, then argue
   that it is a regression, and say which argument you would make to a team
   that just adopted them.

## Next up

[Topic 3 — Timeouts as a first principle](../03-timeouts-as-a-first-principle/README.md):
the pool queue you just filled has a wait limit, and it is one of four
timeouts, three of which you have never set.
