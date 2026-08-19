# Layer 3 · Topic 7 — Connection pools, worker counts, and the container CPU limit

### The takeaway (read this first)

**The one idea:** a pool is a **queue**, and sizing it is Little's Law, not
intuition — past the point where the database can usefully run work in parallel,
more connections add queueing *inside Postgres* and make p99 worse, not better.

**Why it matters in practice:** "the database is slow" is very often "we have 400
connections to a database that can usefully run sixteen things at once." And pool
exhaustion is **metastable**: timeouts trigger retries, retries lengthen the
queue, the longer queue causes more timeouts — and the system does not recover
when the spike ends. This is the most likely shape of a latency problem that
started *suddenly*.

**You'll know it landed when:** you can compute your service's maximum possible
connection count from its deploy config in your head, and know whether the pool
is currently the bottleneck without guessing.

## The concept

**The arithmetic first, because it is the part that is checkable rather than
arguable.**

```
total possible connections = replicas × workers × (pool_size + max_overflow)
```

SQLAlchemy's documented defaults are `pool_size = 5` and `max_overflow = 10`, so
one container running 4 Gunicorn workers can open 4 × (5 + 10) = **60**
connections. Ten replicas is **600** — against a Postgres `max_connections` whose
default is **100**. You hit that ceiling long before you hit any CPU limit, and
the failure mode (`FATAL: sorry, too many clients already`) looks like a database
outage while being an arithmetic error committed in a YAML file.

**The sizing.** Little's Law: required concurrency = arrival rate × mean service
time. 500 req/s × 20ms of database time per request = 500 × 0.02 = **10
connections**. If you believe you need 200, then either your service time is far
worse than you think — fix that instead — or you are using the pool as a buffer
for a queue you should be managing explicitly. HikariCP's old sizing formula
(`cores × 2 + spindles`) is dated in its specifics, since network-attached storage
broke the spindle term, but its *shape* holds and remains counterintuitive: **the
optimal pool is much smaller than people set, and a small pool with visible
queueing beats a large pool with invisible queueing** — because at least the
visible queue is yours to observe, bound, and shed.

**The async Python trap, squarely on your stack.** An async worker can have
hundreds of requests in flight, all contending for `pool_size + max_overflow`
connections. When those run out, `pool_timeout` (SQLAlchemy's default: 30
seconds) means requests sit for thirty seconds and *then* raise a timeout —
surfacing as a database error when the database was never the problem. A second
trap in the same place: build the engine **inside the app lifespan**, not at
import time. An engine created before Gunicorn forks is inherited by every worker
with the same sockets, which is its own corruption category and produces errors
that look like nothing else.

**Pooling in front of Postgres.** PgBouncer in **transaction mode** remains the
default answer for a traditional server application: it hands your transaction a
server connection for the duration of that transaction only, so hundreds of idle
clients cost almost nothing. What changed and matters for Python: PgBouncer
**1.21+** supports named prepared statements in transaction mode via
`max_prepared_statements` (1.22 added handling for `DISCARD ALL` /
`DEALLOCATE ALL`), so *"asyncpg needs `statement_cache_size = 0` behind
PgBouncer"* is **outdated** on a current PgBouncer — and keeping the cache on is
worth real throughput ([Crunchy
Data](https://www.crunchydata.com/blog/prepared-statements-in-transaction-mode-for-pgbouncer)).
The alternatives, honestly: **pgcat** (Rust; read/write splitting and sharding)
if you need routing, and **Supavisor** (Elixir; multi-tenant, built for hundreds
of thousands of client connections, and measurably higher latency in
[published benchmarks](https://www.tembo.io/blog/postgres-connection-poolers)).
For a few container replicas, PgBouncer.

**The container tie-in**, which is where this topic meets
[Layer 1's container topic](../../01-machine/07-inside-a-container/README.md).
With `cpus: "0.5"`, the conventional `workers = 2 × cores + 1` gives you nine
workers on half a core — nine processes' worth of pool connections, one
half-process's worth of CPU, plus CFS throttling on top. Worker count must follow
the **quota**, not `os.cpu_count()`, which inside a container reports the
*host's* cores and lies to you. The quota lives in `/sys/fs/cgroup/cpu.max` and
reads as two numbers, `QUOTA PERIOD`, both in microseconds: `50000 100000` is
half a CPU, `100000 100000` is one, `-1 100000` is unlimited. Dividing the first
by the second is the only correct way to ask "how much CPU do I have."

## How each language actually gets there

**Three languages, and this is the one topic in the layer where that is not a
compromise.** Everything above is client-side: the pool lives in your process,
its defaults were chosen by your driver's authors, and the three differ in ways
that change what your incident looks like.

**Python (SQLAlchemy `QueuePool`)** — bounded, with an overflow band and a
timeout. `pool_size` connections are kept; up to `max_overflow` more are opened
under pressure and closed again afterwards; past that, a request **waits up to
`pool_timeout` (30s) and then raises**. So Python's failure mode is *latency then
an exception*, which is why exhaustion shows up in your error rate looking like
the database rejected you. Under asyncio the pressure is much higher than under
sync workers, because concurrency in flight is no longer bounded by the worker
count.

**Go (`database/sql`)** — the opposite defaults, and the opposite failure.
`SetMaxOpenConns` is **unlimited by default**, so a Go service under load happily
opens connections until *Postgres* refuses with `too many clients`. There is no
client-side queue to observe because there is no client-side limit; the ceiling
is the server's. `SetMaxIdleConns` defaults to 2, so a service that does bound
its open connections can still churn — opening and closing sockets under steady
load, paying TLS and backend-startup cost per query, which looks like unexplained
latency with a healthy-looking pool. Setting `MaxOpenConns` is not optional
tuning in Go; it is the difference between your queue and the server's.

**Node (`pg.Pool`)** — bounded small and **waits forever** by default. `max`
defaults to 10, and `connectionTimeoutMillis` defaults to 0, meaning a request
that cannot get a connection is queued indefinitely rather than failing. Node's
failure mode is therefore *neither an error nor a recovery* — the event loop
stays responsive, health checks pass, and requests simply never complete. Add
that one deadlock from [Topic 5](../05-locking-and-deadlocks/README.md) holding a
pool slot for its full `deadlock_timeout` second and you have the whole causal
chain from a lock problem to an apparently-hung service.

Three languages, three defaults, three completely different-looking incidents
from one underlying mechanism. **That is the reason a second and third language
earn their place here and nowhere else in this layer** — and both of them are
scripts of a few dozen lines, not services.

## The experiment

1. **Pool sweep.** Offer a fixed arrival rate *above* capacity. Sweep `pool_size`
   ∈ {2, 5, 10, 25, 50, 100} with workers fixed. Record throughput, p50, p99,
   pool-timeout errors, and — the key one —
   `SELECT count(*), wait_event_type FROM pg_stat_activity GROUP BY 2` sampled
   *during* the run. Find the knee: throughput flat, p99 climbing. That is the
   queue moving out of your application and into the database, where you cannot
   see or shed it.
2. **Exhaustion as an incident.** Add a slow (500ms) endpoint and send a fifth of
   traffic to it. Watch the *fast* endpoints' p99 rise even though their queries
   are unchanged — head-of-line blocking through a shared pool. Then fix it
   properly with a **separate pool** for the slow endpoint (a bulkhead) plus
   `statement_timeout`, and re-measure.
3. **Metastability.** Client retries on timeout, with no budget. Push past
   capacity for 30 seconds, then drop offered load to half of capacity, and
   record whether throughput recovers. Then add jitter plus a retry budget (cap
   retries at ~10% of requests) and repeat. The first should stay dead. That is
   the entire argument for retry budgets, and you will have watched it happen.
4. **PgBouncer.** Transaction mode, `default_pool_size = 20`,
   `max_client_conn = 1000`. Re-run the sweep with large application-side pools.
   Then flip `max_prepared_statements` between 0 and 200 with the driver's
   statement cache enabled, and record throughput and any errors — this is where
   the outdated advice above becomes a measurement.
5. **The three-driver comparison.** The same fixed workload from the Python, Go
   and Node scripts, each at its *default* settings, then each tuned. Record what
   the failure looks like in each: exception, server rejection, or silence.
6. **Container limit.** Run the API at `cpus: "0.5"`, `"1.0"` and `"2.0"` with
   worker count computed both ways — from `os.cpu_count()` and from the cgroup
   quota — and record all six combinations.

## How to run

Experiments 1-3 and 5 run against the local lab; **4 and 6 need the Docker stack**
in [`lab/README.md`](../lab/README.md), because a pooler and a CPU quota are both
things you put *around* a process.

```
python3 07-connection-pools/python/pool_sweep.py
python3 07-connection-pools/python/bulkhead.py
python3 07-connection-pools/python/retry_storm.py
(cd 07-connection-pools/golang/pool_defaults && go run .)
npm install --prefix 07-connection-pools/nodejs    # once
node 07-connection-pools/nodejs/pool_defaults.js
```

`python/pool_lab.py` holds the shared load generator; it is imported, not run,
and its docstring is worth reading before the results — it is **open-loop** on
purpose, and a closed-loop generator would invert the conclusion of every table
in this topic.

Each Python program measures the machine's real capacity first and sizes its
offered load off that, so the numbers mean the same thing on a laptop and on a
server. `pool_sweep.py` takes about a minute, `bulkhead.py` about thirty
seconds, `retry_storm.py` about a minute.

Knobs: `POOL_SIZES`, `MAX_OVERFLOW`, `POOL_TIMEOUT`, `ARRIVAL_RATE`,
`DURATION_S` for the sweep; `FAST_RATE`, `SLOW_RATE`, `SLOW_SECONDS`,
`SLOW_POOL_SIZE`, `STATEMENT_TIMEOUT_MS` for the bulkhead; `SPIKE_MULT`,
`RECOVER_MULT`, `SPIKE_S`, `RECOVER_S`, `MAX_RETRIES`, `RETRY_BUDGET`,
`STATEMENT_TIMEOUT_MS`, `CAPACITY` for the retry storm.

**`retry_storm.py` will usually print "both recovered" on a laptop, and that is
a result rather than a broken run.** Retries can only hold a system down if a
retried request costs the bottleneck something, and a request that gives up
waiting for a *pool slot* never reached the database at all — so the server did
no work for it, stayed work-conserving, and drains the moment offered load falls
below capacity. The program therefore sets a `statement_timeout` at 1.5x the
measured service time, so that queued queries are cancelled *after* burning
server CPU; that wasted work is the amplifier, and `STATEMENT_TIMEOUT_MS=0`
turns it off so you can watch the difference. Even with it, this single machine —
load generator, driver and Postgres on the same eight cores — did not reach the
metastable state at the shipped defaults during verification. The program names
which of the four outcomes it got and which knob moves it, and does not print the
textbook conclusion when it did not measure it.

The Go program deliberately tries to exhaust `max_connections` — point it at the
lab database and nothing else. The Node program needs `pg`; `package.json`
beside it declares it.

**Experiment 4 needs the pooler, and it has two halves.** The first is
`pool_sweep.py` with `LAB_DSN` moved from the database to PgBouncer — one
substitution, nothing else changes:

```
docker compose -f lab/docker/compose.yml --profile pooler up -d pgbouncer
LAB_DSN=postgresql://lab:lab@127.0.0.1:6432/sep_lab_03_data \
  python3 07-connection-pools/python/pool_sweep.py
```

The second half is `python/pgbouncer_prepared.py`, and it is a separate program
for a reason: `pool_sweep.py` runs each request **inside a transaction**, and a
transaction-mode pooler pins one server connection for the whole transaction, so
a prepared statement can never land on a connection that does not have it. Swept
at `max_prepared_statements` 0 and 200 it reports the same numbers both ways —
which reads like "no effect" and is actually "not measured".

```
MAX_PREPARED_STATEMENTS=0 docker compose -f lab/docker/compose.yml \
  --profile pooler up -d --force-recreate pgbouncer
LAB_DSN=postgresql://lab:lab@127.0.0.1:6432/sep_lab_03_data \
  python3 07-connection-pools/python/pgbouncer_prepared.py     # then repeat at 200
```

**Experiment 6 is Linux-only and must run inside a container.**
`/sys/fs/cgroup/cpu.max` does not exist on macOS; a script that reads it on this
Mac will find nothing and report nothing, which is not a result. Run it in the
`api` container from the compose stack, where the quota is real:

```
python3 07-connection-pools/python/worker_count.py    # says BLOCKED here, and why
CPUS=0.5 docker compose -f lab/docker/compose.yml run --rm api python worker_count.py
CPUS=1.0 docker compose -f lab/docker/compose.yml run --rm api python worker_count.py
CPUS=2.0 docker compose -f lab/docker/compose.yml run --rm api python worker_count.py
```

`CPUS=` goes **in front of** `docker compose`, never as `-e CPUS=0.5`. Compose
interpolates `${CPUS}` into the `api` service's `cpus:` limit while it is parsing
the file, so the value has to be in *compose's* environment. Passed with `-e` it
lands inside the container as an ordinary variable, the quota stays at the
compose default, and all three runs print the same `cpu.max` — which looks like
the experiment working and is not.

`worker_count.py` is written to run in both places: on this Mac it reports that
`/sys/fs/cgroup` does not exist, prints the container command above, and refuses
to invent a quota. Inside the container it reads `cpu.max`, applies
`2 × cores + 1` to both the quota and to `os.cpu_count()`, and prints the
connection arithmetic under each.

## Predict, then record

Before running: at what `pool_size` does throughput stop improving? Does p99
improve or worsen past that point? Does the retry-storm scenario recover on its
own? Does PgBouncer help at small application-side pools, at large ones, or at
both? And for each of the three drivers at default settings — what does the
failure look like?

| pool_size | req/s | p50 | p99 | pool timeouts | active / idle-in-txn / waiting |
|---|---|---|---|---|---|
| 2 |  |  |  |  |  |
| 5 |  |  |  |  |  |
| 10 |  |  |  |  |  |
| 25 |  |  |  |  |  |
| 50 |  |  |  |  |  |
| 100 |  |  |  |  |  |

| Scenario | fast-endpoint p99 | slow-endpoint p99 | pool timeouts |
|---|---|---|---|
| shared pool |  |  |  |
| separate pools + statement_timeout |  |  |  |

| Scenario | recovers after load drops? | time to recover |
|---|---|---|
| retries, no budget |  |  |
| retries + jitter + budget |  |  |

| Driver | default limit | failure at exhaustion | req/s tuned |
|---|---|---|---|
| SQLAlchemy QueuePool |  |  |  |
| Go database/sql |  |  |  |
| node-postgres Pool |  |  |  |

| cpus | workers from os.cpu_count() | workers from cpu.max | req/s | p99 |
|---|---|---|---|---|
| 0.5 |  |  |  |  |
| 1.0 |  |  |  |  |
| 2.0 |  |  |  |  |

**Broken experiment, not wrong prediction, if:**

- **Throughput rises monotonically all the way to 100.** You are not saturating
  Postgres. Give the database container a low enough CPU limit that it is the
  bottleneck, or the sweep is measuring your client.
- **p99 is flat across every pool size.** Your load generator is closed-loop with
  a fixed number of virtual users, which hides queueing **by design** — offered
  load falls as the service slows. Use an arrival-rate executor so offered load
  is independent of response time. This is the most common load-testing error
  there is, and it silently inverts the conclusion.
- **Zero pool timeouts anywhere.** `pool_timeout` is still at its 30s default
  while your client's request timeout fires first. Align them deliberately and
  say which one you want to win.
- **PgBouncer makes no difference.** Confirm the traffic actually goes *through*
  it — `SHOW POOLS` on its admin console — rather than to `postgres-primary`
  directly.
- **The container experiment shows identical numbers at every `cpus` value.** The
  quota is not being applied, or you ran it outside a container. Read
  `/sys/fs/cgroup/cpu.max` from inside and confirm it changed.

## Answer before moving on

1. Derive your service's maximum connection count from its deploy config, then
   the pool size Little's Law says you need. Explain the gap in one paragraph.
2. Why can *increasing* pool size raise p99 while leaving throughput flat? Name
   precisely what is queueing, and where.
3. Transaction-mode pooling breaks some things. Name three specifically, and say
   what you would do about each.
4. A retry storm does not recover when load returns to normal. What property
   makes it metastable, and what is the *minimum* change that fixes it?

## Further reading

- [PgBouncer configuration](https://www.pgbouncer.org/config.html) — pool modes and `max_prepared_statements`
- [Tembo: benchmarking PgBouncer, pgcat and Supavisor](https://www.tembo.io/blog/postgres-connection-poolers)
- Your driver's own pool documentation, read once, deliberately: SQLAlchemy's
  `QueuePool`, Go's `database/sql` `SetMaxOpenConns`, and node-postgres' `Pool`.
  Every default in the table above comes from those three pages, and knowing them
  cold is most of this topic.

## Next up

[Topic 8 — Replication lag, read-your-own-writes, and the one-way doors](../08-replication-lag/README.md).
The standard answer to everything in this topic is "add a read replica" — which
is where you stop having a database and start having a distributed system.
