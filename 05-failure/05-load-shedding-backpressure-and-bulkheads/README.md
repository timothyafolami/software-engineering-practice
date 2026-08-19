# Layer 5 · Topic 5 — Load shedding, backpressure, and bulkheads

### The takeaway (read this first)

**The one idea:** you cannot serve more than capacity, so your only real
choice is whether excess requests are rejected in one millisecond or time out
after thirty seconds having consumed a connection, a thread and a query — and
the first choice keeps p99 flat for everyone who *was* admitted.

**Why it matters in practice:** deliberate shedding is the difference between
"we served 95% and cleanly rejected 5%" and "we served 0%". It is also the
escape hatch from topic 4, and the only one you can arm in advance.

**You'll know it landed when:** you see a queue in a design and immediately
ask "what is its bound, and what happens when it's full?", treating
"unbounded" as a defect rather than as a default.

## The concept

**Backpressure** is a bounded queue whose fullness is *visible to the
producer*. Both halves matter: a bound with no signal is just a different
error, and a signal with no bound is advice nobody takes. Unbounded queues do
not smooth load — they convert an availability problem into a latency problem
and hide it until latency exceeds every timeout in the system at once. An
unbounded queue is a latency bomb with a fuse you did not choose.

**Load shedding** is what happens at the bound, and the non-obvious part is
the *signal*. Rejecting on queue **length** is wrong: length tells you how
many items are waiting and nothing about how long they take, so the same
length means a healthy queue for a 1ms handler and a catastrophe for a 500ms
one. Rejecting on queue **wait time** is right: a request that has already
waited longer than target probably has no caller left, and serving it is
strictly wasted work — the same zombie work topic 2 counted, arriving from a
different direction. That is the CoDel insight, imported from network queue
management.

Its refinement is **adaptive LIFO**: FIFO normally, LIFO under pressure.
Under overload, FIFO serves the oldest request first — exactly the one whose
caller has already given up — so when you are behind, the newest request is
the one whose caller is still listening.

**The state of the art has moved past static limits.** Adaptive concurrency
limits (Netflix's `concurrency-limits`, Envoy's `adaptive_concurrency` filter)
borrow directly from TCP congestion control: sample latency continuously,
infer the minimum round-trip time, raise the in-flight limit while latency
stays near that minimum, lower it when latency climbs. You never configure a
number — the system discovers it, and rediscovers it when your code changes,
which matters because the hand-measured number from topic 1 goes stale the
day someone adds a join.

**Priority-aware shedding** goes further: shed the same *users* everywhere
rather than giving everyone a partially broken experience. Uber's published
description of Cinnamon pairs a PID controller (setting rejection rate) with a
modified TCP Vegas (setting the in-flight limit) over a priority queue of 768
levels — 6 tiers × 128 user cohorts. **Their published figures** — roughly a
microsecond of overhead per request, and p50 held near 180ms at 300% overload
where the alternatives they compared against exceeded 500ms — are Uber's own
numbers from their engineering blog, on their workload and their hardware.
They are here as a claim about the *shape* of what priority-aware shedding
buys, not as a result this lab has measured or expects you to reproduce.

**Bulkheads** are the structural version, and the least glamorous idea here
that most often turns out to be the one you needed: separate pools per
dependency or per workload class. Concretely, a separate SQLAlchemy engine
(or a separate PgBouncer pool) for report queries versus checkout, so the
8-second report *cannot* starve checkout — not because it is well behaved,
but because it is structurally incapable of touching checkout's connections.

**And a word on circuit breakers,** because *Release It!* made them the
headline pattern and the advice has genuinely moved. Marc Brooker's
simulations compare naive retries, circuit breakers and token buckets, and
breakers come out poorly for three reasons: they are **modal** (binary
open/closed, so behaviour changes discontinuously exactly when you least want
surprises), each client estimates the failure rate independently from a small
sample, and with many short-lived clients — which containers and serverless
gave us — they trip early and degenerate toward "no retries at all". Token
buckets degrade smoothly instead. Keep breakers for a dependency that is
*hard down*; for the *slow* dependency, which is what this layer is about,
prefer topic 3's retry budget plus an adaptive concurrency limit.

## How each language actually gets there

All six, because "where can I even put an admission decision?" has a
different answer in each runtime, and in half of them the honest answer is
"further out than you think".

**Python** gives you one crude static shedder worth knowing precisely.
`uvicorn --limit-concurrency` returns an immediate 503 with **no queueing**
above the limit; its default is `None`; and it applies only *after* the
connection has been accepted, so the kernel accept queue (`--backlog`,
default 2048) fills first and everything sitting in it is invisible to your
metrics and already burning the caller's budget. Anything smarter is
middleware you write: an `asyncio.Semaphore` sized to your measured knee, a
wait deadline on acquisition, a 503 with `Retry-After`, and a priority tier
derived from the route or the caller. Fifty lines, and the
highest-leverage fifty lines in this layer.

**Node.js** has the most natural adaptive signal of any runtime here, because
event loop lag *is* queue wait for the one server it has:
`monitorEventLoopDelay()` gives you a live histogram, and shedding on a lag
threshold needs no capacity model at all. The catch is that lag measures the
JS queue only — work sitting in libuv's thread pool, or connections in the
kernel backlog, does not move the number, so a Node service can shed nothing
while its file IO queue grows.

**Go** offers the cleanest composition of ideas in this whole layer:
`golang.org/x/sync/semaphore` has a **context-aware** `Acquire`, so topic 2's
deadline performs the queue-wait rejection *automatically* — a request whose
budget expires while queueing for admission is rejected without you writing
any timeout logic at all. Two topics, one line. `net/http`'s zero-value
server has no admission control of its own, so this is entirely yours to add,
but the primitive is right there.

**Rust** puts admission control in the middleware stack where it belongs:
`tower::limit::ConcurrencyLimit` and `tower::load_shed` compose as layers, and
`Semaphore::try_acquire` gives you a non-blocking "is there room?" that is the
literal definition of shedding. Rust's structural advantage is that a bounded
`mpsc::Sender::try_send` returning `Err(TrySendError::Full)` is a *type* you
must handle — the compiler will not let you ignore backpressure, which is
precisely the failure mode in every other language here.

**C++** is the version where you see what the kernel was doing all along.
`listen(fd, backlog)` is the first queue, and shedding there means the kernel
refuses the connection (or drops the SYN) — the cheapest possible rejection,
and the only one that costs the server nothing. Everything above it is a
queue you wrote, so you can timestamp on enqueue and reject on *measured wait*
rather than on length, without any framework's cooperation. Building CoDel by
hand here takes about forty lines and makes the idea permanent.

**Java** has the mature toolkit — Resilience4j's `Bulkhead` (semaphore) and
`ThreadPoolBulkhead`, and a `ThreadPoolExecutor` with a bounded
`ArrayBlockingQueue` plus an explicit `RejectedExecutionHandler`, which is
load shedding spelled out in the standard library. The virtual-threads twist
is the important one: switching to `Executors.newVirtualThreadPerTaskExecutor()`
removes the thread pool that was *accidentally* your admission controller, so
a service that used to reject now accepts everything and queues it against the
database instead. Virtual threads make you write the limit you were getting
for free — the same lesson Go teaches, arrived at from the opposite direction.

## The experiment

Take topic 1's ramp to 130% of capacity, unchanged, and add an admission
controller in front of it.

1. **Baseline:** topic 1's numbers. p99 goes vertical past the knee.
2. **Static shedder:** a semaphore sized to the concurrency measured at the
   knee, plus a 50ms queue-wait deadline → 503 with `Retry-After`. Identical
   ramp, nothing else changed.
3. **Record** accepted rps, **p99 of accepted requests**, rejection rate, and
   goodput. The claim under test: p99 of accepted stays roughly flat past
   100% offered while rejections absorb the excess.
4. **Priority tiers:** `/checkout` at tier 0, `/search` at tier 3. At 130%,
   confirm tier 0's p99 and success rate are unaffected while tier 3 absorbs
   the rejections.
5. **Adaptive:** replace the hand-set limit with a gradient controller. Does
   it converge to the number you measured by hand, and within what
   percentage? Then change service time by 3× at runtime and watch it
   re-converge.
6. **Bulkhead:** add a slow `/report` on the *same* pool at 5 rps and watch
   checkout die. Give `/report` its own engine with 3 connections and watch
   checkout live.

Output shape:

```
mode=<name>  offered=<rps>  accepted=<rps>  rejected=<pct>  goodput=<rps>  p99_accepted=<ms>  tier0_success=<pct>
```

## How to run

**The harness is built and was executed here.** `lab/docker-compose.yml`,
`lab/app/`, `lab/scripts/*.js` and
`lab/tools/*.py` exist (specified in
[`../lab/README.md`](../lab/README.md)) and the commands below were run
against them. You do **not** need to install `k6`: it runs from the
`grafana/k6` image, which is what `docker compose run --rm k6` starts. What
you do need is Docker running (`docker info`) and host ports 8000-8003 free —
if something else on your machine holds 8000, `up` fails with `port is
already allocated`. From `05-failure/lab/`:

```
cd ../lab
docker compose --profile shed up -d --build
for M in none static priority adaptive; do
  docker compose run --rm k6 run /scripts/05_shed.js -e MODE=$M \
    --out csv=/out/05_shed_$M.csv
done
python3 tools/plot_shed.py out/
```

Step 5's re-convergence test is `-e MODE=adaptive -e SERVICE_3X=120`, which
triples service time at t=120s mid-ramp. Step 6's bulkhead is a config change
and a second load source, not a different image:

```
cfg() { curl -s -XPOST localhost:8000/admin/config \
          -H 'Content-Type: application/json' -d "$1" > /dev/null; }

cfg '{"BULKHEAD": 0, "REPORT_SERVICE_MS": 2000}'    # /report shares the pool
while :; do curl -s localhost:8000/report > /dev/null; sleep 0.2; done &
docker compose run --rm k6 run /scripts/05_shed.js -e MODE=static \
  --out csv=/out/05_shed_static_noreport.csv

cfg '{"BULKHEAD": 1, "REPORT_POOL_SIZE": 3}'        # /report gets 3 of its own
docker compose run --rm k6 run /scripts/05_shed.js -e MODE=static \
  --out csv=/out/05_shed_static_bulkhead.csv
kill %1
```

The plotter runs today against the synthetic fixtures that ship with the
harness — a model, not a measurement, there so a broken plotting script is
found by running it:

```
cd ../lab && python3 tools/make_fixtures.py
python3 tools/plot_shed.py out/fixtures/
```

The six standalone versions apply the same seven scenarios — `none` at
ρ=0.8 and ρ=1.3, static, priority, adaptive, and the bulkhead pair — to each
runtime's own admission primitive: Python's `asyncio.Semaphore` with a wait
deadline, Node's hand-rolled permit queue alongside a live
`monitorEventLoopDelay()` histogram, Go's buffered channel selected against a
context, Rust's `tokio::sync::Semaphore` with `try_acquire` and an RAII
ticket, C++'s hand-rolled pool with CoDel checked at both ends of the queue,
and Java's `Semaphore.tryAcquire` on virtual threads. Same constants in all
six, so the runs are comparable. All six are written and run natively on
macOS / arm64 with no container; each takes about two and a half minutes —
seven scenarios of twenty seconds, which is long enough that the adaptive
controller's min-RTT reset lands inside the run and you can watch the limit
come back rather than only watching it dip.

```
python3 python/shedder.py
node nodejs/shedder.js
cd golang && go run shedder.go
cd rust/shedder && cargo run --release
c++ -O2 -std=c++17 -pthread -o /tmp/shedder cpp/shedder.cpp && /tmp/shedder
cd java && javac Shedder.java -Xlint:all -d /tmp/javabuild && \
  java -cp /tmp/javabuild Shedder
```

Each ends with the same summary table — offered, accepted, goodput, p99 of
accepted, p99 of tier 0, reject %, tier-0 success % and the cost of a
rejection — so the numbers you record below can come from whichever runtime
you ran.

## Predict, then record

Before running: what is p99 of *accepted* requests at 130% offered, relative
to p99 at 80%? Will total goodput at 130% be higher or lower with shedding
than without? Does the adaptive controller find the same limit you measured
by hand, and within what percentage? For the bulkhead run: what is the
minimum number of connections `/report` needs before it starts hurting
checkout again?

| Mode | offered | accepted | goodput | p99 accepted | reject % | tier-0 success % |
|---|---|---|---|---|---|---|
| none, ρ=0.8 | | | | | | |
| none, ρ=1.3 | | | | | | |
| static shed, ρ=1.3 | | | | | | |
| priority, ρ=1.3 | | | | | | |
| adaptive, ρ=1.3 | | | | | | |
| bulkhead, shared pool | | | | | | |
| bulkhead, split pool | | | | | | |

**What would mean the experiment is broken, not the prediction wrong:**

- **p99 of accepted still climbs past the knee.** Your shedder sits
  *downstream* of the real queue. Check the kernel accept backlog and the
  worker count — you cannot shed what the kernel has already queued, and a
  request that spent four seconds in the accept queue is late before your
  middleware ever sees it. Drop `--backlog` to 128 and see whether the shape
  changes; if it does, you have located the real queue.
- **Zero rejections at 130% offered.** Your semaphore is larger than actual
  capacity. Re-derive it from topic 1's *measured* knee, not from a guess.
- **Goodput with shedding is *lower* than without.** Your rejection is not
  cheap. If a 503 still runs authentication, a database lookup and full
  serialisation, you saved nothing — reject as early in the stack as you can
  get, and measure the cost of a rejection explicitly.
- **The adaptive controller oscillates wildly.** Expected on the first
  attempt, and instructive: it is reacting faster than the system's settling
  time. That is exactly why Cinnamon's published design uses a PID controller
  with a deliberately slow decrease rate.

## Answer before moving on

1. Why is queue *wait time* a better shedding signal than queue *length*?
   Construct a concrete case where length-based shedding rejects exactly the
   wrong requests.
2. Adaptive LIFO deliberately serves the newest request first, which is
   unfair and can starve some requests indefinitely. Justify it anyway — then
   add the one mechanism that stops "indefinitely" being literal.
3. You split one pool of 20 into two bulkheads of 10. Under what traffic mix
   does that make things *worse*, and how would you know in advance without
   running it?
4. Sketch your own service's degradation matrix: feature, tier, what "off"
   looks like to a user, kill-switch mechanism, who may flip it. Any row
   whose kill switch is "deploy a code change" is not a kill switch.

## Sources

- Yanacek, *Using Load Shedding to Avoid Overload*, AWS Builders' Library
- Google SRE Book (free online), *Handling Overload* and *Addressing
  Cascading Failures*
- Marc Brooker, *Fixing Retries with Token Buckets and Circuit Breakers*
  (2022) — https://brooker.co.za/blog/2022/02/28/retries.html
- Netflix `concurrency-limits`; Envoy's `adaptive_concurrency` filter and
  retry-budget documentation
- Uber, *Cinnamon: Using Century Old Tech to Build a Mean Load Shedder* —
  source of the vendor figures quoted above, which are theirs and not this
  lab's

## Next up

[Topic 6 — Tail latency, fan-out, and coordinated omission](../06-tail-latency-fanout-and-coordinated-omission/README.md):
why the p99 you have been optimising may describe a system that was never
under stress.
