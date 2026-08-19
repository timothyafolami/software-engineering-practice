# 7.4 — Sizing a Python web service in a container, properly

### The takeaway (read this first)

**The one idea:** worker count is not a CPU decision. It is simultaneously
a CPU-quota decision, a memory decision, a database-connection decision
and a thread-pool decision — and the binding constraint is usually not the
one you were tuning.

**Why it matters in practice:** "the app got slow right after we scaled
up" is a stock incident, and the causal chain is almost always arithmetic
someone could have done on paper: more replicas × more workers × a pool
size nobody re-derived = more Postgres backends than `max_connections`
allows. Nothing in Docker, Kubernetes, uvicorn, SQLAlchemy or FastAPI
computes this product for you or warns when it exceeds a limit.

**You'll know it landed when:** handed a spec — `cpus: "2"`, 4 workers,
pool size 10, 3 replicas — you can state, before running anything, the
throughput ceiling, the worst-case backend count at the database, roughly
what RSS to expect, and which of the four ceilings binds first.

---

## The concept

Four ceilings, all real, all easy to blow through, none of which know
about each other.

**1 · Quota.** N CPU-saturated workers under Q CPUs of quota start
throttling once N > Q. For CPU-bound work, `workers ≈ floor(Q)` is the
ceiling; going higher buys no throughput at all (the quota was always the
limit) and costs tail latency — [7.2](../02-throttled-at-30-percent-cpu/README.md)
measures exactly that. For IO-bound work the picture is different, because
workers waiting on a socket are not spending quota, which is why the
best worker count for `/db` and for `/cpu` are different numbers.

**2 · Connections.** The arithmetic that gets skipped:

```
replicas × workers × (pool_size + max_overflow)  ≤  max_connections − reserved
```

With this lab's harness numbers — `max_connections = 100`, Postgres's
default `superuser_reserved_connections = 3` — four workers at
`pool_size 10 + max_overflow 10` is `4 × 20 = 80` backends **from one
replica**. Add a second replica and you are at 160 against a budget of 97.
Each of those backends is a real Postgres process with real memory, so the
failure is not only "connection refused"; it is also the database slowing
down for everyone else first. This is precisely how scaling up makes
things slower.

**3 · Threads.** FastAPI runs plain `def` endpoints on an anyio thread
pool whose default limiter is **40 tokens per process**. Request 41 waits
for a token. There is no log line, no metric, no exception — the latency
simply appears somewhere you are not looking.
[7.5](../05-the-sync-driver-inside-the-async-endpoint/README.md) is that
ceiling under load.

**4 · Memory.** Every worker is a separate interpreter: its own bytecode,
its own imported module objects, its own connection pool, its own copy of
any in-process cache. Copy-on-write after `fork()` shares some of that at
first and then un-shares it as refcounts get written into the same pages
the objects live in. N workers is meaningfully more than N× nothing, and
`memory.max` does not negotiate —
[7.6](../06-memory-the-limit-that-kills-you-without-a-traceback/README.md).

### The opinionated default

For an IO-bound FastAPI service at a 2-CPU quota: **2 uvicorn workers, an
async driver, pool size 5–10 per worker, and a connection cap you can
compute on paper.** Prefer one process per container and let the
orchestrator replicate — which is what FastAPI's own deployment docs now
recommend, notably no longer mentioning gunicorn with `UvicornWorker`.
Gunicorn's supervisor is still defensible on a single-VM Compose deploy
where nothing else restarts a wedged worker; it is dead weight under
Kubernetes, which already does that job. If you need more concurrency than
the connection math allows, the answer is pgbouncer in transaction mode,
not more workers.

---

## How each language actually gets there

**One language here, on purpose.** The ceilings above are properties of a
specific production stack — uvicorn's process model, Starlette's anyio
limiter, SQLAlchemy's pool, Postgres's `max_connections` — and the
interesting part is the *arithmetic between them*, not a language
contrast. Rewriting it six times would produce six copies of the same
spreadsheet.

The shape does transfer, though, and it is worth being able to translate
it. What follows is not built in this folder; it is the map for when you
need it:

| Runtime | "workers" | Its hidden thread ceiling | Where the pool math lives |
|---|---|---|---|
| Python / uvicorn | processes (`--workers`) | anyio limiter, 40 tokens/process | SQLAlchemy `pool_size` + `max_overflow`, per process |
| Node | processes (`cluster`, or one per container) | `UV_THREADPOOL_SIZE`, default 4 | `pg.Pool` `max`, default 10, per process |
| Go | one process, `GOMAXPROCS` threads | none — goroutines are not the scarce thing | `database/sql` `SetMaxOpenConns`, **unlimited by default** |
| Rust | one process, tokio workers | tokio blocking pool, default 512 | `sqlx`/`deadpool` `max_connections`, explicit |
| Java | one process, thread pool or virtual threads | pool size, or carrier count under Loom | HikariCP `maximumPoolSize`, default 10 |
| C++ | whatever you built | your own pool | your own |

Two entries in that table are traps worth naming. Go's `database/sql`
defaults to **no limit** on open connections, so a Go service under load
can open connections until Postgres refuses — the opposite failure from
Python's, arrived at from the same missing arithmetic. And Java's virtual
threads make it trivially easy to have 10,000 concurrent requests in
flight against a 10-connection pool, which converts a thread-pool queue
into a connection-pool queue without changing a number anywhere.

---

## The experiment

At a fixed `cpus: "2.0"`, sweep uvicorn workers over {1, 2, 4, 8} against
`/db` and against `/cpu` separately, recording for each: p99, throughput,
throttle ratio, RSS, and the live backend count from

```sql
SELECT count(*) FROM pg_stat_activity WHERE datname = 'container_lab';
```

read **at peak**, not after the run — pools are lazy and a post-run
reading shows you connections that have already been returned or closed.

The two endpoints are the experiment. `/cpu` is bounded by quota, so its
best worker count should be near `floor(Q)`. `/db` spends most of its time
waiting, so its best worker count should be higher — until the connection
math or the memory limit stops it. Predicting *both* numbers, and being
able to say why they differ, is the skill.

## How to run

```bash
cd 01-machine/07-inside-a-container/00-harness

for W in 1 2 4 8; do
  WORKERS=$W API_CPUS=2.0 docker compose up -d --force-recreate api
  docker compose exec api cat /sys/fs/cgroup/cpu.max         # confirm, every time
  ./observe/watch.sh api 60 > /tmp/watch_w$W.txt &
  docker compose --profile load run --rm --no-deps -e ENDPOINT=/db  -e RATE=120 k6 run /scripts/steady.js
  docker compose --profile load run --rm --no-deps -e ENDPOINT=/cpu -e RATE=60  k6 run /scripts/steady.js
  docker compose exec db psql -U lab -d container_lab \
    -c "select count(*) from pg_stat_activity where datname='container_lab';"
  docker stats --no-stream api
done

# the sweep, driven end to end (part 1 is arithmetic and needs nothing;
# part 2 drives Compose and needs the daemon)
cd ../04-sizing-a-python-web-service-in-a-container
python3 python/sizing_sweep.py                              # the four ceilings
python3 python/sizing_sweep.py --replicas 3 --workers 4     # a spec you were handed
python3 python/sizing_sweep.py --measure --duration 30s     # the {1,2,4,8} sweep
```

**Inside Linux containers only.** The quota half of this experiment does
not exist on macOS; the harness runs in Docker Desktop's VM, and
`sizing_sweep.py` drives Compose from the host rather than reading cgroups
on it. Give that VM at least 4 CPUs or a 2.0-CPU quota is not a
constraint.

## Predict, then record

Predict the worker count that maximises throughput on `/db`, and
separately on `/cpu`. They should not be the same number — write down why
before you run it. Then predict the backend count at 8 workers from the
formula, and check it against `pg_stat_activity`.

| Workers (Q=2.0) | `/db` p99 | `/db` req/s | `/cpu` p99 | `/cpu` req/s | throttle ratio | PG conns | RSS |
|---|---|---|---|---|---|---|---|
| 1 | | | | | | | |
| 2 | | | | | | | |
| 4 | | | | | | | |
| 8 | | | | | | | |

**Broken, not merely surprising.** If p99 is flat across all four rows,
the database or k6 is your bottleneck rather than the API — check the `db`
container's own CPU and confirm `/db` is not doing a sequential scan
(`EXPLAIN` it; `init.sql` indexes the key it reads). If PG connections do
not rise with workers, the pool is lazy and has not opened them yet: read
`pg_stat_activity` during the run. If RSS is flat across worker counts,
you are reading the container total from a stale sample or only one worker
actually started — `docker compose exec api ps aux | grep uvicorn`. If
throughput *rises* from 4 to 8 workers on `/cpu` under a 2.0 quota,
something is not CPU-bound: check that `/cpu`'s calibration at import
actually landed near 15ms.

## Answer before moving on

1. You have `cpus: "2.0"`, 3 replicas, and `max_connections = 100`.
   Choose worker count and pool size so that the worst case fits, and
   state the throughput you gave up to make it fit. Then say what
   pgbouncer in transaction mode would let you change, and what it would
   cost you (hint: what breaks when two requests share a backend between
   transactions?).
2. `/db` throughput improves from 1 to 4 workers and then stops improving,
   while the throttle ratio stays near zero throughout. The quota is not
   binding, so what is? Name three candidates and the one reading that
   distinguishes them.
3. Copy-on-write means forked uvicorn workers *start* sharing most of
   their memory and stop sharing it over time. Explain the mechanism, and
   say what that implies for a memory limit sized from RSS measured in the
   first ten seconds after startup.
4. FastAPI's docs moved away from recommending gunicorn with
   `UvicornWorker`. Give the argument for one process per container in
   terms of the four ceilings above — then describe a deployment where the
   old advice is still the right call.

## Next up

[7.5 — The sync driver inside the async endpoint, under a quota](../05-the-sync-driver-inside-the-async-endpoint/README.md).
You have sized the pools. Next: what happens when a single blocking call
inside one of these workers makes the size irrelevant — and why the
resulting latency looks identical to CFS throttling until you read
`cpu.stat`.
