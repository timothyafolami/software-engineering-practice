# Layer 6 · Topic 2 — Instrument the slow service and find the real p99

### The takeaway (read this first)

**The one idea:** the p99 is not "the p50 but slower." It is usually a different
code path or failure mode entirely — which is why optimising what the median does
rarely moves it, and why a percentile computed from bucketed histograms, by a
closed-loop load generator, over too few samples, can be wrong by an order of
magnitude in either direction before anyone has lied to you.

**Why it matters in practice:** this is the live problem that put this layer first
in the running order. Users experience a service at the tail, not at the mean. And
the tail is reached more often than the number suggests: a page that makes 20
backend calls hits a p99 on at least one of them with probability
`1 − 0.99^20 ≈ 0.18` — roughly one page load in five, not one in a hundred.

**You'll know it landed when:** you can look at a latency graph and say what your
bucket boundaries are doing to it, and tell "the service got slower" apart from "my
load generator stopped asking."

## The concept

Four separate things make p99 numbers wrong. They compound, and you need all four
before any latency number means anything.

**Buckets.** A Prometheus classic histogram is a set of per-bucket counters;
`histogram_quantile` interpolates *inside* the bucket the 99th-percentile sample
lands in. If your top finite bucket is `le="1.0"` and the real p99 is 8s, then every
sample past one second is in `+Inf`, there is nothing to interpolate against, and
the documented behaviour is to return the upper bound of the highest finite bucket.
So you get a number near 1s. Not an error. Just wrong — and always wrong at a
suspiciously round number, which is the tell. Prometheus 3's **native histograms**
fix this with exponential buckets at a configurable resolution instead of a boundary
list you guessed at deploy time.

**Aggregation.** You cannot average percentiles. `avg(p99 per pod)` is not the p99
of anything. Sum the bucket counters across pods *first*, then take the quantile —
which is the reason histograms are shipped as counters rather than as computed
quantiles in the first place.

**Sample count.** A p99 over 200 requests is decided by its two or three slowest
samples: at nearest-rank, `ceil(0.99 × 200) = 198`, so exactly two samples sit above
it. Re-run the same load and you get a different pair. That is not a measurement, it
is a coin flip with units.

**Coordinated omission.** This is the one that survives all the other fixes. A
closed-loop generator with N virtual users sends its next request only after the
last one returns. When the server stalls, the generator *stops asking* — so the
stall shows up as fewer requests rather than as slow ones, and `http_req_duration`
p99 looks fine while users time out. The tell k6 gives you: `iteration_duration`
climbs while `http_req_duration` doesn't. The fix is an arrival-rate executor
(`constant-arrival-rate`), which issues at a fixed RPS regardless of what came back.

Work the arithmetic once and it stops being a curiosity. A single server with a FIFO
queue serving one request every 3ms has a capacity of about 333 req/s. Offer it 200
req/s — a comfortable 60% — and stall it for 500ms exactly once. An open-loop
generator issues about `200 × 0.5 = 100` requests into that stall window and every
one of them waits. A closed-loop generator with 4 virtual users issues *at most 4*,
because the other users are all blocked in the stall. Same service, same fault, and
the two generators cannot agree, because one of them was not looking.

## How each language actually gets there

The p99 arithmetic is language-neutral. What is not neutral is the **cost of holding
in-flight requests**, and that cost is the entire reason closed-loop load testing
became the default — the profession quietly redefined the measurement to fit the
tool it could afford.

**Python** — the production stack, and the runtime where the honest generator is
most expensive to build. Holding 250 requests in flight means 250 OS threads, or 250
coroutines and an async client you may not have. That is exactly why so many Python
load tests are written closed-loop with a small fixed worker count, and a
closed-loop test structurally cannot observe the failure it was written to catch.
Python is also where the *service*-side finding lives: auto-instrumentation gives
you the server span and the DB spans and nothing in between, so **connection-pool
wait appears as dead time between the server span's start and the first DB span,
with no span of its own** — SQLAlchemy checks a connection out before any statement
is issued, so there is nothing for the instrumentation to hook. Invisible until you
instrument `checkout`/`checkin` yourself. That is the finding, and Topic 5 turns it
into a metric.

**Node.js** — the cheapest place to write the *correct* generator, which is worth
more than it looks. Every issued-but-unanswered request is a pending promise: a few
hundred bytes, no kernel object, no scheduler decision. The runtime that is worst at
CPU-bound work is the one that makes the honest measurement easiest. The same
property is the trap on the service side: a single server with a queue *is* what a
Node service is, so a 500ms stall in a handler is not one slow request, it is every
request that arrived during those 500ms.

**Go** — makes the honest generator cheap while the code still reads
`submit(); wait()`, one goroutine per in-flight request, which is the mental model
people actually have when they write a load test. The program reports how many OS
threads the runtime actually created to hold ~100 concurrent requests; that number,
next to Python's and C++'s one-thread-per-request, is why vegeta, k6, ghz and hey
are all written in Go rather than in the language of the service they test. Go is
also why `pricing` is Go: no runtime trap of its own (Layer 1 Topic 3 — the
scheduler protects you), so the tail it produces is unambiguously a *dependency*
property rather than a runtime one.

**Rust** — `std` deliberately ships no async runtime, which makes this the clearest
statement of the tradeoff. With the standard library alone, every in-flight request
is a `std::thread` with a real stack. Reach for tokio and you get Go's economics,
but the compiler immediately starts asking which of your futures are `Send`, which
is a different lab. Second contribution, at the type level: the shared latency vector
is behind `Arc<Mutex<_>>` not because a linter said so but because the program does
not compile otherwise.

**C++** — the same design with nothing hiding its cost: every in-flight request in
the open-loop phase is one `std::thread`, one pthread, with the platform's default
stack (512 KB for a secondary thread on macOS — `pthread_attr_getstacksize` will
confirm it on your machine). And by omission: the shared sample vector is guarded by
a `std::mutex` because it must be, not because anything made it so. Delete the
`lock_guard` and it still compiles, still runs, and produces numbers wrong in a way
no output would reveal. The Rust file does not offer that option.

**Java** — the only runtime here that can run the *same* open-loop generator two
ways in one process, one platform thread per in-flight request and one virtual
thread per in-flight request, and show that the measurement is identical while the
cost of taking it is not. That is the practical answer to "why was every load
generator written closed-loop for twenty years": because a thousand in-flight
requests used to mean a thousand OS threads. Java 21 removes the excuse.

**Languages: all six.** The subject is what it costs a runtime to hold work in
flight, which is the runtime's defining property — the case the lab exists for.
`histogram_lies.py` is Python only, and says so below.

## The experiment

**Part 1 — the four arithmetic lies.** `python/histogram_lies.py` runs the same
200,000 latency samples (a realistic bimodal service: most requests fast, a small
tail three orders of magnitude slower) through all four errors: a bucket list whose
top finite boundary is below the real p99; `avg(p99 per pod)` versus summing bucket
counters first; a p99 over 200 samples against the same statistic over 100,000; and
Prometheus 3 native histograms on identical data. Every row is printed against
ground truth computed from the raw samples, so the error column is derived, not
claimed. **Python only:** every one of these errors is arithmetic a monitoring
system does on your behalf, and reimplementing that arithmetic in forty lines is the
only way to stop treating `histogram_quantile()` as an oracle. It is the same
arithmetic in six languages.

**Part 2 — coordinated omission, in six runtimes.** `coordinated_omission.*` builds
the single-server-with-a-queue model above and runs both generators against it. Read
four lines of the output in this order: requests started *during* the stall window
(closed-loop ~4, open-loop ~100 — that one line is the entire mechanism); the two
p99 rows; closed-loop iteration duration against request duration, which is k6's
tell reproduced from first principles; and the count of OS threads the generator
needed. The Java file runs a third phase so the platform-thread and virtual-thread
open-loop generators can be compared directly.

**Part 3 — find the defect that owns the p99.** Against the shared stack:

1. Run `steady.js` (closed-loop, 60 VU). Record p50/p95/p99 from three independent
   sources: k6's `http_req_duration`, the SDK's `http.server.request.duration`
   histogram, and Tempo's span durations.
2. Re-run with `arrival.js` (300 RPS). Record the same three, and compare.
3. In Tempo, filter `duration > 2s`, open five traces, and tabulate where the time
   went per trace: pool wait (the gap with no span), DB, `pricing`, Python.
4. Disable exactly one defect. Re-run. Record the delta on p50 *and* p99
   separately. Repeat for each, always reverting.

The five defect names and what each is are in [`../lab/README.md`](../lab/README.md).

## How to run

Parts 1 and 2 are standard-library only and need no stack:

```
python3 python/histogram_lies.py
python3 python/coordinated_omission.py
node nodejs/coordinated_omission.js
cd golang && go run coordinated_omission.go
cd rust/coordinated_omission && cargo run --release
clang++ -O2 -std=c++17 -pthread -o /tmp/coordinated_omission \
  cpp/coordinated_omission.cpp && /tmp/coordinated_omission
cd java && javac CoordinatedOmission.java -d /tmp/javabuild && \
  java -cp /tmp/javabuild CoordinatedOmission
```

The Java run takes about 15 seconds: three five-second load phases, run in sequence
so they cannot interfere with each other.

Part 3, from `lab/`:

```
docker compose run --rm k6 run /scripts/steady.js
docker compose run --rm k6 run /scripts/arrival.js
DEFECT_DISABLE=n_plus_one docker compose up -d api && \
  docker compose run --rm k6 run /scripts/arrival.js
```

## Predict, then record

Before Part 2: **(a)** how many requests each generator starts inside the stall
window; **(b)** the ratio between the two p99s; **(c)** how many OS threads each
language needs to hold ~100 requests in flight.

| Language | closed-loop p99 | open-loop p99 | started in stall (closed / open) | OS threads |
|---|---|---|---|---|
| Python | | | | |
| Node.js | | | | |
| Go | | | | |
| Rust | | | | |
| C++ | | | | |
| Java (platform) | | | | |
| Java (virtual) | | | | |

Before Part 3: **(a)** which defect owns the p99? **(b)** which owns the p50?
**(c)** will closed-loop and arrival-rate report the same p99, and if not, which is
higher?

| Defect disabled | p50 (arrival) | p95 | p99 | Δp99 vs baseline |
|---|---|---|---|---|
| none (baseline) | | | | — |
| n_plus_one | | | | |
| sync_http_in_async | | | | |
| small_pool | | | | |
| missing_index | | | | |
| pricing_tail | | | | |

**What would mean the experiment is broken rather than your prediction wrong:**

- If p99 ≈ p95 ≈ p50, you have coordinated omission. Check whether
  `iteration_duration` rose while `http_req_duration` stayed flat, and switch to the
  arrival-rate script before concluding the service is healthy.
- If p99 sits flat at a round number — exactly 1s, exactly 10s — you are reading the
  top finite bucket, not the latency.
- If Tempo's slowest trace is faster than Prometheus's p99, your sampler dropped the
  slow traces. Head sampling is blind to latency by construction, which is the whole
  argument for tail sampling.
- If the two generators agree in Part 2, check the stall actually fired — the
  programs print the stall window's start and end; if no request landed inside it,
  you measured a service with no fault in it.
- If a Python or C++ run is slower than the model says it should be, count the
  threads it printed against your core count. Past a few hundred threads you are
  measuring the scheduler, not the queue.

## Answer before moving on

1. Your p50 improves 40% and your p99 doesn't move. What does that say about the
   *shape* of the distribution, and which class of cause does it rule out?
2. Why does pool wait appear as a gap with no span, and what specifically would you
   instrument to give it one?
3. You have a 99.9% latency SLO. Argue for defining the SLI as "fraction of requests
   under 300ms" rather than "p99 under 300ms." (Topic 6 needs this answer.)
4. Coordinated omission is usually described as a load-generator bug. Name a place
   in a *production* telemetry pipeline — not a load test — where the same
   mechanism hides a stall, and say what you would measure instead.

## Next up

[Topic 3 — correlation IDs and the one-query test](../03-correlation-ids/README.md):
the slow trace you just found is only useful if you can get from it to the log lines
that explain it.
