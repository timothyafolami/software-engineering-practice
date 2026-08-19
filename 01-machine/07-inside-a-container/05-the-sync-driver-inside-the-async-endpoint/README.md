# 7.5 — The sync driver inside the async endpoint, under a quota

### The takeaway (read this first)

**The one idea:** Topic 3's blocking-call failure does not merely reappear
in a container — the quota *amplifies* it, and a stalled event loop and a
frozen cgroup produce the same latency signature. You cannot tell them
apart without reading `cpu.stat`.

**Why it matters in practice:** `async def` calling a synchronous database
driver is the single most common serious FastAPI bug in production code,
and it is invisible in review: the handler is `async`, the driver call
looks like every other line, the tests pass because tests are one request
at a time. Under load it stops the event loop for the duration of every
query — and in a container it also changes how fast you drain the CPU
bucket, so the two effects stack and the obvious fix for one makes the
other worse.

**You'll know it landed when:** given a p99 that is 10× p50 in a
containerised async service, you can name the two candidate mechanisms,
give the one command that separates them, and predict which of "raise the
thread limit" and "respect the quota" is the right advice for this
particular service — because they give opposite advice and knowing which
binds is the skill.

---

## The concept

Two independent stalls, same symptom.

**Stall A — the event loop.** An `async def` handler runs on one thread.
A synchronous call inside it (psycopg2, `requests`, `time.sleep`, a big
`json.dumps`) does not yield to the loop, so every other in-flight request
on that worker waits for it, whether or not they touch the database. This
is Layer 1
[Topic 3](../../03-concurrency-models/README.md) exactly, with a database
driver in the role of `time.sleep`.

**Stall B — the cgroup.** Every runnable thread in the container drains
the same quota bucket. When it empties, the kernel dequeues all of them,
including threads that were only waiting on a socket
([7.2](../02-throttled-at-30-percent-cpu/README.md)).

Both produce: p50 fine, p99 terrible, no errors, healthy-looking average
CPU. They are told apart by exactly one reading — `nr_throttled` in
`/sys/fs/cgroup/cpu.stat`. If it is near zero, your stall is the event
loop and more quota will not help. If it is climbing, at least part of
your latency is the accountant.

The reason they interact rather than merely coexist: Starlette's fix for
Stall A is to run blocking work on a thread pool, and threads are the
input to Stall B. **Every mitigation for the event loop increases the
number of runnable threads in the cgroup.** That is why variant 3 below
exists, and why it is the one to think hardest about.

### The thread limiter, precisely

FastAPI runs plain `def` endpoints via `anyio.to_thread.run_sync()`, whose
default limiter holds **40 tokens per process**. Request 41 blocks
acquiring a token — no exception, no log line, no metric. The limit is
per-process, so `WORKERS=4` means 160 tokens across the container, all
drawing on one CPU bucket.

Raising it is one line in a lifespan handler:

```python
anyio.to_thread.current_default_thread_limiter().total_tokens = 100
```

and it is a genuinely good idea right up until the point where the extra
runnable threads drain the quota faster than the extra concurrency earns
you anything. That crossover is measurable, and this experiment measures
it.

---

## How each language actually gets there

**One language here, on purpose.** The six-runtime treatment of "a
blocking call inside a concurrency primitive" already exists as Layer 1
[Topic 3](../../03-concurrency-models/README.md), across all six languages
and with the ticker experiment that makes the stall visible. What is *new*
here is the interaction with the CPU quota, and that is a property of the
cgroup rather than the language — so this folder builds it once, on the
production stack the rest of the topic uses.

Read the two together. The translation table, if you need it elsewhere:

| Runtime | Stall A appears as | Its offload | What the offload costs you in the cgroup |
|---|---|---|---|
| Python | sync driver inside `async def` | `run_in_executor` / anyio thread pool | +N runnable threads on one bucket |
| Node | CPU-bound JS or `*Sync` fs calls | `worker_threads`, `UV_THREADPOOL_SIZE` | +N threads, and V8 heap per worker |
| Rust | `std::thread::sleep` in a tokio task | `spawn_blocking` (pool default 512) | the largest thread blow-up of the six |
| Java | blocking call on a fixed pool of 1 | virtual threads, or a bigger pool | carriers, not virtual threads, spend quota |
| Go | rarely — netpoller parks the goroutine | none needed | GC workers still spend quota |
| C++ | blocking task on your own pool | a second pool | exactly what you wrote, no more |

Go's cell is the interesting one twice over: it mostly does not have Stall
A, and it still has Stall B, which is a compact demonstration that these
are two different problems that merely look alike.

---

## The experiment

Four variants of the *same* `/db` endpoint, one container spec, one k6
script:

1. **`async def` calling psycopg2 directly** — the sync driver inside the
   async handler. The event loop stops for the duration of every query.
2. **`def` calling psycopg2** — Starlette offloads to the anyio thread
   pool. Correct, and fine up to 40 concurrent requests per process, after
   which requests queue silently.
3. **Same as (2) with the limiter raised to 100** — watch it get *worse*
   past a point, because more runnable threads drain the CPU quota faster.
4. **`async def` with asyncpg** (or psycopg3's async interface) — the
   actual fix.

Drive all four at an arrival rate comfortably above the thread-pool limit
and record p99 **alongside the throttle ratio**. The pairing is the point:
the variant with the worst p99 and the variant with the highest throttle
ratio need not be the same variant, and understanding why is understanding
the topic.

## How to run

```bash
cd 01-machine/07-inside-a-container/00-harness

# each variant is one env flip on the same image
API_CPUS=1.0 WORKERS=1 ANYIO_THREAD_TOKENS=40 DB_SLEEP_S=0.050 \
  docker compose up -d --force-recreate api
./observe/watch.sh api &
docker compose --profile load run --rm --no-deps -e ENDPOINT=/db -e RATE=120 k6 run /scripts/steady.js

# variant 3 differs only here
ANYIO_THREAD_TOKENS=100 docker compose up -d --force-recreate api

# all four in sequence, with the cpu.stat reading paired to each row
cd ../05-the-sync-driver-inside-the-async-endpoint
python3 python/run_variants.py                    # all four
python3 python/run_variants.py --rate 160         # push past the limiter
python3 python/run_variants.py --only 1,4         # the event-loop half only
python3 python/run_variants.py --quota 0.5        # make the cgroup bite harder
```

`run_variants.py` writes a `docker-compose.override.yml` into
`00-harness/` to mount the handler bodies; delete it when you are done.
It prints the path.

The four handler bodies live in
`05-the-sync-driver-inside-the-async-endpoint/app/variants.py` and are
mounted over the harness service, so the container spec is byte-identical
across the four runs — which is the only way the throttle-ratio column
means anything.

**Linux containers only** for the throttling half. The event-loop half
(variants 1 vs 4) is visible on macOS against a local Postgres, and it is
worth seeing there first precisely because the throttle ratio is then
guaranteed to be absent: any p99 you see on the host is Stall A, with no
possibility of Stall B contaminating it.

## Predict, then record

Predict the ordering of all four by p99, and — separately — which variant
has the **highest throttle ratio**. They are not required to be the same
answer, and if your two predictions coincide, write down why you think so.

| Variant | p50 | p99 | req/s | throttle ratio | notes |
|---|---|---|---|---|---|
| 1 · `async def` + psycopg2 | | | | | |
| 2 · `def` + psycopg2 (40 tokens) | | | | | |
| 3 · `def` + psycopg2 (100 tokens) | | | | | |
| 4 · `async def` + asyncpg | | | | | |

**Broken, not merely surprising.** If variant 1 is not dramatically worse
than variant 4, the query returns too fast to block measurably — raise
`DB_SLEEP_S` to 0.05 so the wait is real, which is also a more honest
model of a query crossing a network. If variants 2 and 3 are identical,
you never exceeded 40 requests in flight: raise `RATE` until k6 reports
concurrency above 40, or the limiter was never the constraint. If every
variant shows a throttle ratio of zero, the quota is not binding at this
rate — that is a valid result for the p99 column but it means the
*interaction* this sub-topic is about is not in your data yet; lower
`API_CPUS` or raise `RATE`. If variant 4 is the *slowest*, check that
asyncpg is actually being used and not silently falling back.

## Answer before moving on

1. Variant 1 and variant 3 can produce similar p99 values by completely
   different mechanisms. Describe both chains, and give the two readings
   (one per mechanism) that would let you tell them apart in production in
   under a minute.
2. Raising the anyio limiter increases concurrency and increases
   throttling. Sketch what the p99-versus-token-count curve looks like at
   a fixed quota, name the shape, and say which of the four ceilings from
   [7.4](../04-sizing-a-python-web-service-in-a-container/README.md) sets
   the position of the minimum.
3. Variant 4 removes the blocking call entirely. Under what circumstances
   would variant 4 have a *higher* throttle ratio than variant 1 —
   and is that a regression?
4. Your fix — swapping psycopg2 for asyncpg — cuts p99 by 5×. Two weeks
   later p99 is bad again and the throttle ratio is now *higher* than
   before the fix. Give the most likely causal chain, in order.

## Next up

[7.6 — Memory: the limit that kills you without a traceback](../06-memory-the-limit-that-kills-you-without-a-traceback/README.md).
CPU limits stop you and let you continue. The memory limit does not
negotiate, does not log, and cannot be caught.
