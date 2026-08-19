# Layer 5 · Topic 4 — Metastable failure

### The takeaway (read this first)

**The one idea:** the thing that *triggered* an outage and the thing that
*sustains* it are usually different mechanisms, so removing the trigger does
not end the outage — the system has settled into a second stable state where
the retry load, or the cold cache, or the queue of expired work, is now the
problem.

**Why it matters in practice:** this is why "we rolled back and it's still
down", why "the dependency recovered ten minutes ago and we're still at
zero", and why the only thing that reliably works is dropping traffic to zero
and letting it back slowly — an action that feels insane in the moment and
that nobody authorises unless they understood this in advance.

**You'll know it landed when:** during an incident your second question,
after "what changed?", is "what is *sustaining* this?", and you can name the
specific amplification mechanism rather than gesturing at load.

## The concept

Bronson, Aghayev, Charapko and Zhu (HotOS '21) define three states:

- **Stable** — perturbations are absorbed and the system returns to where it
  was.
- **Vulnerable** — still serving fine, still meeting every SLO, but a large
  enough perturbation tips it over. Nothing on your dashboard distinguishes
  vulnerable from stable. That is the whole problem.
- **Metastable** — permanently overloaded with near-zero *goodput*, sustained
  by its own feedback loop, and it stays there after the trigger is gone.

Huang et al. (OSDI '22) studied 22 such failures across 11 organisations.
A HotOS '25 paper sharpens the frame into three named parts — **trigger**,
**amplification mechanism**, **sustaining effect** — and that vocabulary is
worth adopting verbatim, because it forces the question people skip.

**Two defining properties.**

**Goodput collapses while throughput does not.** The machine is busy, CPU is
pinned, requests are flowing, and almost none of them produce a response
anyone receives. Any dashboard measuring "requests handled" rather than
"requests answered within the caller's deadline" shows a healthy system
during a total outage. Define goodput explicitly and put it on the wall:
*responses delivered to a caller that was still waiting for them.*

**It is self-sustaining.** Remove the trigger, wait, nothing improves. The
only escapes are reducing load below the now much lower capacity, or breaking
the feedback loop directly. Note the "now much lower" — capacity in the
metastable state is not the capacity you measured in topic 1, because the
system is spending most of its resources on work that will be discarded.

**Common sustaining effects in a Python / Postgres / Redis / Docker stack:**

- **Retry amplification** — topic 3, the canonical one, and the one where
  restarting your servers changes nothing because the amplifier lives in the
  clients.
- **Cold cache.** Your database is sized for the 5% of requests that miss.
  Flush the cache and every request misses, so the database gets `1/0.05` =
  **20× its normal load** — and the cache cannot refill, because the fills
  themselves time out. The feedback loop is: misses
  → slow database → timed-out fills → continued misses.
- **A timeout shorter than *degraded* service time.** This deserves its own
  line because it converts partial degradation into a **100% failure rate**:
  if service time under load rises above your client timeout, every request
  times out, every one retries, and you have zero successes at maximum load.
  Check your timeouts against degraded latency, not normal latency.
- **Queues of dead work.** FIFO under overload serves the oldest requests
  first — precisely the ones whose callers have already given up. Topic 5's
  adaptive LIFO exists for this.
- **GC death spirals.** More load means more live objects means more GC means
  less CPU for work means more in-flight requests means more live objects.

## How each language actually gets there

All six, because this topic is about what your runtime does when work arrives
faster than it leaves, and that is precisely where the six differ most.

**Python** is the most exposed, because asyncio gives you no natural
backpressure anywhere: `asyncio.create_task` never blocks, so an overloaded
service quietly accumulates pending tasks and pending futures until memory
pressure and GC become a *second* sustaining effect stacked on the first. The
tell is RSS climbing while goodput falls. The other Python-specific amplifier
is the connection pool itself — a waiter whose `pool_timeout` expires and
which immediately retries re-enters the same queue, so you can build a closed
feedback loop entirely inside one process, with no clients involved.

**Node.js** shows it earliest and most legibly, because event loop lag rises
monotonically with queued work: the same single-threaded design that makes it
fragile makes its saturation *unambiguous*. Lag is queue wait, directly
measured, no inference needed. That makes Node the easiest of the six to
**detect** metastability in, and no easier at all to escape it — an
accumulated backlog of pending callbacks and unresolved promises is exactly
as self-sustaining as anyone else's.

**Go** needs a deliberate mistake to get here, which is itself the lesson. A
buffered channel is a genuine bounded queue with real backpressure — a full
channel blocks the producer, which is the correct behaviour — and `context`
cancellation actually removes abandoned work from the system rather than
letting it run to completion. So the Go path into metastability is specific
and nameable: unbounded goroutine spawning per request, `MaxOpenConns` left
unset, or a `select` on an unbounded queue you built yourself. Reproducing it
in Go means opting out of the defaults; that is the language's whole argument.

**Rust** has no GC, which removes one classic sustaining effect entirely, and
tokio's bounded `mpsc` gives real backpressure when you use it. What Rust
still cannot save you from is the *cross-service* loop: a memory-safe,
GC-free, perfectly backpressured service still collapses when a thousand
clients retry into it. Rust is the cleanest demonstration that metastability
is an architectural property, not a memory-management one — and its
`spawn_blocking` pool (bounded, but at 512 threads) is a queue deep enough to
hide a very long backlog.

**C++** gives you every sustaining effect at once and no defence from any of
them: an unbounded `std::queue` behind a mutex is the default shape of every
hand-rolled thread pool, there is no cancellation, and a request whose caller
has vanished runs to completion because nothing in the language knows the
caller existed. It is also the only version where you can instrument the
queue's *age distribution* directly and watch the oldest item's age climb
without bound — the single clearest visualisation of the metastable state
anywhere in this layer.

**Java** carries the GC death spiral as a first-class hazard, and it is worth
watching the specific shape: as live-set grows, collections get more frequent
*and* longer, so allocation stalls rise, so more requests are in flight, so
the live set grows. `-Xlog:gc*` plus a goodput counter shows it in one chart.
Java's other trap is a documented default that reads as safe and is not:
`Executors.newFixedThreadPool(n)` uses an **unbounded** `LinkedBlockingQueue`,
so the pool bounds your *concurrency* while your *queue* grows forever — the
textbook latency bomb. Use a bounded `ArrayBlockingQueue` with an explicit
`RejectedExecutionHandler` and you have built topic 5 by accident.

## The experiment

**This is the flagship. Do not skip the "wait five minutes" part** — the
entire claim is about what happens *after* the trigger is gone, and an
experiment that stops at trigger removal proves nothing at all.

FastAPI + Redis + Postgres + k6, with topic 3's naive retries on, long
timeouts, no budget, no shedding.

1. **Establish the stable state:** 60% of topic 1's measured capacity for
   three minutes, cache warm. Record goodput and confirm it is flat. Do not
   proceed until it is.
2. **Trigger:** `redis-cli FLUSHALL` — one command, instantaneous, fully
   reversible, and the cache is *back* the moment it starts refilling. (An
   alternative trigger: `docker compose pause postgres` for 30s.)
3. **Keep offered load constant at 60%.** Do not increase it. The entire
   point is that offered load never changed.
4. **Observe for five minutes after the trigger is gone**, charting goodput,
   throughput, cache hit rate, database connections and retry rate on one
   axis. The gap between the goodput line and the throughput line is the
   whole topic.
5. If goodput does not recover, try each escape **in isolation** and record
   which are *sufficient* versus merely helpful:
   (a) drop offered load to zero for 10s, then ramp back;
   (b) enable topic 3's 10% retry budget without dropping load;
   (c) enable topic 5's shedder without dropping load;
   (d) restart the app containers.
6. Write the result down in HotOS '25 vocabulary: **trigger**,
   **amplification mechanism**, **sustaining effect**. Three sentences.

Output shape:

```
t=<s>  offered=<rps>  throughput=<rps>  goodput=<rps>  hit_rate=<pct>  pg_conns=<n>  retry_rate=<ratio>
```

## How to run

Uses the shared harness — see [`../lab/README.md`](../lab/README.md).

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
docker compose --profile metastable up -d --build
docker compose run --rm k6 run /scripts/04_metastable.js \
  --out csv=/out/metastable.csv &
sleep 180 && docker compose exec redis redis-cli FLUSHALL
# then watch, and touch nothing, for five minutes
python3 tools/plot_goodput.py out/metastable.csv
```

`04_metastable.js` sizes the load against the **database's** capacity, not the
service's: `-e LOAD_MULT=3` means three times the rate at which uncached reads
can be served, which is a rate only the cache makes possible and is why losing
it matters. `setup()` prints the stable-state database utilisation the current
`LOAD_MULT` and `CACHE_TTL_S` imply — aim it at the 75-85% this topic's
"what would mean the experiment is broken" section asks for, and **verify
goodput is flat before you flush**, because both ends of that band fail in
different directions.

Step 5's escapes are `-e ESCAPE=budget` and `-e ESCAPE=shed`, applied at
`-e ESCAPE_AT=<seconds>` **without dropping load** — dropping load is its own
escape and confounds every other one. Escapes (a) and (d) are the two you run
by hand, because that is what they are: `docker compose stop k6` then restart
it, and `docker compose restart app`.

The plotter runs today against a synthetic CSV that ships with the harness —
a model, not a measurement:

```
cd ../lab && python3 tools/make_fixtures.py
python3 tools/plot_goodput.py out/fixtures/metastable.csv
```

The six standalone versions reproduce the loop in a single process — a
simulated cache and backend, one runtime's own queueing primitives, the same
trigger, the same constants and the same observation window, so the runs are
comparable with each other. All six are written and run natively on macOS /
arm64 with no container; each takes about four minutes (five scenarios, the
four with an escape running longer).

```
python3 python/metastable.py
node nodejs/metastable.js
cd golang && go run metastable.go
cd rust/metastable && cargo run --release
c++ -O2 -std=c++17 -pthread -o /tmp/metastable cpp/metastable.cpp && /tmp/metastable
cd java && javac Metastable.java -Xlint:all -d /tmp/javabuild && \
  java -Xlog:gc -cp /tmp/javabuild Metastable
```

Each prints the same columns plus one its runtime can show and the others
cannot: Node's measured event-loop lag, Go's live goroutine count, Rust's
spawned-task count, C++'s queue length and head-of-line age, Java's
virtual-thread count. The verdict lines at the end of each run are computed
from that run, not asserted by the program.

## Predict, then record

Before running: will goodput recover on its own within five minutes at
constant 60% offered load? If not, which single escape do you expect to be
*sufficient* — not merely helpful? Will CPU during the metastable period be
high or low, and what does your answer imply about CPU as an alerting signal?

| Phase | goodput | throughput | cache hit % | pg conns | retry rate |
|---|---|---|---|---|---|
| stable (t=0-180s) | | | | | |
| t=200s | | | | | |
| t=300s | | | | | |
| t=480s | | | | | |
| escape (a) drop load | | | | | |
| escape (b) retry budget | | | | | |
| escape (c) load shedding | | | | | |
| escape (d) restart app | | | | | |

**What would mean the experiment is broken, not the prediction wrong:**

- **It recovers in twenty seconds.** That is a slow drain, not
  metastability, and your amplification is too weak. Two knobs: raise offered
  load to 75-85% of measured capacity (a system at 30% utilisation cannot go
  metastable no matter what you do to it), and make sure the client timeout
  is longer than *normal* service time but shorter than *degraded* service
  time.
- **It never destabilises at all.** Same diagnosis, plus: verify the cache
  was absorbing most of the load before the flush. At a 40% hit rate,
  flushing barely changes database load; you want 90%+ for the trigger to
  land.
- **"Never recovers", but goodput was also near zero before the trigger.**
  You were already broken. Re-establish and *verify* the stable baseline
  first — step 1 exists for this reason.
- **Escape (d), restarting the app, fixes it.** Not a broken run, but record
  it carefully: your sustaining effect lived in app-local state — an
  in-process queue, a pool of dead connections, a leaked task set — rather
  than in client retry behaviour. If the clients are the amplifier,
  restarting the server changes nothing, which is the result most people find
  genuinely surprising the first time.

## Answer before moving on

1. Requests/sec looks normal, CPU is at 100%, and someone concludes the
   system is "handling the load". What single metric makes that impossible to
   misread, and how exactly do you define it in terms your metrics pipeline
   can compute?
2. Escape (a) is the reliable one and the one nobody wants to authorise.
   Write the two sentences you would say to a VP at 3am, in their units, not
   yours.
3. Why does a *cold* cache trigger metastability where a *slow* cache often
   does not? What does that say about which of the two you should alert on?
4. Construct on paper a metastable failure whose sustaining effect is neither
   retries nor caching. Anything with a feedback loop qualifies: leader
   election, health checks, autoscaling, connection re-establishment.

## Sources

- Bronson, Aghayev, Charapko, Zhu, *Metastable Failures in Distributed
  Systems*, HotOS '21 —
  https://sigops.org/s/conferences/hotos/2021/papers/hotos21-s11-bronson.pdf
  (six pages; the state vocabulary used above)
- Huang et al., *Metastable Failures in the Wild*, OSDI '22 —
  https://www.usenix.org/system/files/osdi22-huang-lexiang.pdf
  (22 failures across 11 organisations; code at
  https://github.com/lexiangh/Metastability)
- Isaacs, Alvaro et al., *Analyzing Metastable Failures*, HotOS '25 —
  https://sigops.org/s/conferences/hotos/2025/papers/hotos25-106.pdf
  (the trigger / amplification / sustaining-effect framing)

## Next up

[Topic 5 — Load shedding, backpressure, and bulkheads](../05-load-shedding-backpressure-and-bulkheads/README.md):
the escape hatch, built deliberately instead of discovered at 3am.
