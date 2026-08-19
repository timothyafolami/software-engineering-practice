# Layer 5 · Designing for failure

Seven topics, each with its own `README.md` carrying the concept, the
per-language mechanism, the experiment, and a blank results table to fill in.

> Not the happy path, which everyone gets right, but what the system does
> when a dependency is **slow** rather than down. Slow is much worse than
> down, and this layer unpacks why.

The through-line: you cannot serve more than capacity, waiting is arithmetic
rather than opinion, and every technique here is a decision about *which*
requests lose. If latency climbs while CPU sits at 40%, you are queueing on a
**count** — pool slots, threads, tokens, workers — not on CPU. Topic 1 is
that arithmetic, and there is a decent chance it is the whole answer to the
latency problem you have right now.

| # | Topic | The thing it teaches |
|---|---|---|
| 1 | [Utilisation, Little's Law, and the latency knee](01-utilisation-littles-law-and-the-latency-knee/README.md) | Why 60% is fine and 80% is not |
| 2 | [Timeout budgets and deadline propagation](02-timeout-budgets-and-deadline-propagation/README.md) | Work nobody awaits is pure amplification |
| 3 | [Retries that don't become the outage](03-retries-that-dont-become-the-outage/README.md) | Backoff, jitter, caps, budgets |
| 4 | [Metastable failure](04-metastable-failure/README.md) | Why removing the trigger doesn't end it |
| 5 | [Load shedding, backpressure, and bulkheads](05-load-shedding-backpressure-and-bulkheads/README.md) | Rejecting beats collapsing |
| 6 | [Tail latency, fan-out, and coordinated omission](06-tail-latency-fanout-and-coordinated-omission/README.md) | p50 describes requests that had no problem |
| 7 | [Idempotency, and degradation decided in advance](07-idempotency-and-degradation-decided-in-advance/README.md) | The precondition that makes 2-6 legal |

Do them in order. Topic 4 is the flagship and needs 1-3 reproducible first.

## The shared harness

Topics 2-7 need more than one process, so they share one Docker Compose stack
— `app`, `postgres`, `redis`, `toxiproxy`, `k6`, and optionally Prometheus and
Grafana. Service names, ports, environment variables, compose profiles and
k6 script paths are specified once in [`lab/README.md`](lab/README.md); the
topic READMEs reference it rather than restating it.

Several topics also ship standalone per-language programs that reproduce the
same mechanism in one process and run natively on macOS 27 / arm64. The
container stack runs Linux inside Docker Desktop's VM, where absolute
throughput is not comparable to production: **shapes transfer, absolute
numbers do not.**

## The language set

Six — Python, Node.js, Go, Rust, C++, Java — used where the runtime is the
subject, which here means anywhere the queue's *bound* comes from the runtime
rather than from you: topics 1, 2, 4 and 5 use all six. Topic 3 uses five,
topic 6 four and topic 7 three, each stating its one-line reason at the top of
its language section. Six near-identical Postgres clients would teach nothing;
six different answers to "where does the queue live and who bounds it" teach
the entire layer.

## The "you own this when" test (from the roadmap)

> You can look at an architecture diagram and point at the place it will fail
> under load, and say why, before anyone runs a test.

Topics 1 and 5 build the two halves of that directly — where the smallest
count is, and what happens at it — and topic 4 is the test of whether you
believed the first three.

## One book, with a caveat

The roadmap names ***Release It!*** by Michael Nygard, and it is still right.
The current edition is the **2nd (2018)**, so it predates the metastable
failure literature (2021 onward), the mainstreaming of adaptive concurrency
limits, and service meshes doing outlier detection and retry budgets as
infrastructure. Its stability patterns remain the right vocabulary; its
emphasis on the circuit breaker as *the* headline pattern has been partly
superseded for the slow-dependency case this layer is about — see topic 5.
Each topic lists its own primary sources.

## Next up

**Layer 6 — Observability and operating.** The natural sequel, because every
experiment here depended on measuring something default dashboards do not
show: goodput rather than throughput, queue wait rather than queue length,
in-flight count rather than CPU, merged histograms rather than averaged
percentiles. This layer teaches you what to look at; the next teaches you how
to be able to.
