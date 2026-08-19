# Layer 6 · Topic 3 — Correlation IDs and the one-query test

### The takeaway (read this first)

**The one idea:** a correlation ID is worth something only if it is propagated by
the *transport* rather than by your discipline — which is exactly what W3C
`traceparent` is, and why "we pass a `request_id` header everywhere" quietly stops
working at the first boundary somebody forgets.

**Why it matters in practice:** the roadmap's line is the test. *If you cannot pull
every log line for one request in one query, you do not have logs, you have text.*
Once you can, an incident stops being archaeology: find one bad request in the trace
UI, click through to its logs, read what happened.

**You'll know it landed when:** you can name every place in your architecture where
trace context is at risk of being dropped, and those are the first places you look
when a trace comes back truncated.

## The concept

Propagation has two halves, and they fail differently.

**Cross-process** is the easy half, because it is a wire format. The active context
is serialised into a `traceparent` header on the way out and parsed on the way in.
The format is fixed and small — `version-traceid-spanid-flags`, e.g.
`00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01` — which is the point: any
vendor's SDK can read any other vendor's header, so a request crossing four services
written by four teams with three vendors still produces one trace. That is what
being a W3C standard buys, and it is why the pre-2019 world of `X-B3-*` versus
`uber-trace-id` versus `X-Amzn-Trace-Id` was a real operational problem rather than
a formatting preference.

**In-process** is the hard half, because there is no wire. Something has to make
"the current span" follow execution across `await` points, callbacks, thread
handoffs and task spawns — and every runtime answers that differently, which is the
whole of the next section.

Breakages are always at boundaries the transport does not cover, and the list is
short enough to memorise:

- a job pushed onto a **queue** — nothing carries the header unless you put it in
  the message body yourself;
- a **thread-pool offload**, where the ambient context belongs to the calling thread
  and the work runs on another;
- a **subprocess** or shell-out;
- a **retry loop** that builds a fresh client per attempt;
- a **gateway or proxy** that strips headers it does not recognise.

For logs, three things have to be true at once. A logging filter reads the current
span context and injects `trace_id`/`span_id` into every record; a formatter emits
JSON rather than a human sentence; and the backend makes `trace_id` **searchable but
not a label**. That last one matters more than it sounds: in Loki, an index label
per trace ID creates one stream per request and detonates the index. Ship it as
*structured metadata* instead. That is Topic 4's lesson arriving a topic early, and
it is the single most common way a correlation-ID rollout takes down the logging
system it was supposed to improve.

## How each language actually gets there

In-process context is the runtime's problem, and the six answers are genuinely
different — different enough that a team moving a service between two of them will
reintroduce this bug on the first day.

**Python** — `contextvars`, which asyncio understands: each Task gets a *copy* of
the context at creation, so `asyncio.create_task` propagates and later mutations do
not leak back. The classic loss is `run_in_executor`: the callable runs on a
thread-pool thread that never saw your context, so you get a fresh, empty one unless
you hand it `contextvars.copy_context().run`. Logging instrumentation injects
`otelTraceID`/`otelSpanID` onto the `LogRecord`; you supply the JSON formatter.
Ordering matters — configure your app logger *after* `opentelemetry-instrument` has
patched `logging`, or you get a formatter attached to a handler that never sees the
injected fields.

**Node.js** — `AsyncLocalStorage`, built on `async_hooks`, which tracks context
across promises, timers and most callbacks automatically. It loses context in
exactly one shape: any library that manages its own callback queue and invokes your
callback from a different async resource than the one that registered it. Old
connection pools and hand-rolled event emitters are the usual suspects, and the
symptom is one specific integration that silently starts a new trace while
everything else works.

**Go** — refuses to guess. `context.Context` is a parameter, so nothing is ever lost
silently; a function that takes `context.Background()` instead of the caller's ctx
starts a brand-new trace, and that is a bug visible in code review rather than a
mystery at 3am. Go trades ergonomics for the failure being **loud**, and this topic
is the best argument in the lab for that trade.

**Rust** — the `tracing` crate keeps a current span per thread, and futures do not
inherit it, because a future is a value that can be polled from anywhere. You attach
context explicitly with `.instrument(span)` (or `Span::current().in_scope(...)` for
sync code), and forgetting to do so on a `tokio::spawn` is the canonical Rust
version of this bug. It is the one runtime where the fix is a combinator you can
grep for.

**C++** — no ambient context of any kind. You either thread a context struct through
every call, or you reach for `thread_local`, and `thread_local` is exactly wrong the
moment a thread pool reuses a thread for a different request: you do not lose the
context, you inherit the *previous request's*, which is worse than a truncated trace
because it produces a complete-looking one that is false. C++ is here to make that
failure mode concrete.

**Java** — the OTel `Context` is `ThreadLocal`-backed, so it survives ordinary calls
and dies at every `ExecutorService` handoff unless you wrap the executor
(`Context.taskWrapping`) or capture and re-scope by hand. Virtual threads change the
economics rather than the semantics: one virtual thread per request means the
thread-local *is* per-request again, and Java 21's `ScopedValue` is the structured
answer to the same problem. Java is the only language here where the fix and the
Layer 1 concurrency material are literally the same change.

**Languages: all six.** The subject is how a runtime carries implicit state across
its own concurrency boundaries — the definition of a runtime property.

## The experiment

**Part 1 — lose the context, in six runtimes.** A single small program per language:
start a span, hand a unit of work to whatever that runtime calls "somewhere else"
(executor, spawned task, pooled thread, goroutine, promise chain), and print the
trace ID observed at both ends. Run it twice — once naively, once with the runtime's
correct propagation mechanism — and print both. The C++ version adds a third run: a
`thread_local` context on a two-thread pool serving three requests, which prints a
*wrong* trace ID rather than a missing one.

The output shape is the same everywhere, so the six can be read side by side:

```
caller trace_id   <id>
callee trace_id   <id or "none">   naive
callee trace_id   <id>             propagated
verdict           lost | preserved | WRONG (inherited from previous request)
```

**Part 2 — the one-query test, on the shared stack.**

1. Add the logging filter and JSON formatter to `api` and `worker`. Confirm
   `trace_id` on every line.
2. Run load. Pick one slow trace in Tempo, click through to logs, and verify you get
   *every* line for that request across `api`, `pricing` and `worker` — and only
   those. Both halves of that sentence are the test.
3. **Break it four ways** and observe each: queue the job without embedding
   `traceparent`; offload the pricing call via `run_in_executor` with no context
   copy; have `pricing` build its tracer from a fresh context; strip `traceparent`
   at the collector with a `transform` processor.
4. Record the Tempo waterfall shape per break: an orphan, a truncated trace, or a
   second trace that looks complete and isn't.

The four `BREAK` values are in [`../lab/README.md`](../lab/README.md).

## How to run

Part 1, standalone:

```
python3 python/lose_the_context.py
node nodejs/lose_the_context.js
cd golang && go run lose_the_context.go
cd rust/lose_the_context && cargo run --release
clang++ -O2 -std=c++17 -pthread -o /tmp/lose_the_context \
  cpp/lose_the_context.cpp && /tmp/lose_the_context
cd java && javac LoseTheContext.java -d /tmp/javabuild && \
  java -cp /tmp/javabuild LoseTheContext
```

Five of the six are standard library only. The Rust one depends on **tokio**,
declared in its `Cargo.toml`, because the boundary this topic is about is
`tokio::spawn` — a future polled on a worker thread that never saw your
thread-local. Everything else in that file, including the `Instrument`
combinator that fixes it, is written out in `src/main.rs` rather than pulled
from `tracing`, so the twenty-five lines that make propagation work are on the
page. If `cargo` cannot reach the network, `cargo run --offline --release`
builds from the crate cache.

Part 2, from `lab/`:

```
docker compose up -d api worker pricing
docker compose run --rm k6 run /scripts/arrival.js

# break 1 -- the variable goes on the PRODUCER. `api` is the service that
# decides whether to write the traceparent column; `worker` only reads it.
# Setting BREAK on `worker` alone recreates the consumer and changes nothing.
BREAK=queue_no_traceparent docker compose up -d api worker

# break 2 -- needs the sync_http_in_async defect turned OFF at the same time.
# With that defect enabled (the default) `fetch_price` calls the blocking
# client directly and never reaches `run_in_executor`, so BREAK=executor_no_ctx
# on its own is a no-op: the pricing span stays in the caller's trace.
BREAK=executor_no_ctx DEFECT_DISABLE=sync_http_in_async docker compose up -d api

# break 3 -- this one is `pricing`'s alone and works as printed.
BREAK=pricing_fresh_ctx docker compose up -d pricing
```

Verifying a break landed is worth doing before you go looking in Tempo for its
shape, because three of the four produce no error anywhere. `api` and `pricing`
both log a `trace_id` per request; one request and two `docker compose logs`
tail commands tell you whether the two services agreed on a trace. `pricing`'s
`/stats` also counts `joined_traces` against `orphaned_traces`, but note what
that counter can and cannot see: it reports whether a *parseable* traceparent
arrived, so break 2 leaves it reading 100% joined while the trace it joined is
a brand new one. Comparing the two `trace_id`s is the check that discriminates.

The first three breaks are environment variables read by `api`, `worker` and
`pricing`. The fourth is **not** an environment variable: a collector config is
static YAML with no conditionals, so `BREAK=collector_strip` on the `otelcol`
container does nothing. Uncomment the `filter/drop_pricing` processor in
[`../lab/otelcol/config.yaml`](../lab/otelcol/config.yaml), add it to both traces
pipelines, and recreate the container:

```
# edit lab/otelcol/config.yaml, then:
docker compose up -d --force-recreate otelcol
```

Read that processor's comment before you run it. The obvious break — deleting a
`traceparent` attribute — breaks nothing, because trace linkage lives in each
span's `trace_id`/`parent_span_id` and not in an attribute. The collector-side
break that is real is the collector *dropping* spans, and the shape it produces
is unlike all three in-process breaks: every trace is complete and internally
consistent, and the service that owns the latency is simply not in it.

Revert with `docker compose up -d` and no `BREAK` set (and the processor commented
out again) before moving to the next one; two simultaneous breaks produce a shape
neither of them produces alone.

## Predict, then record

Before Part 1: which runtimes lose context at their thread/task boundary by default,
and which of them tell you?

| Language | Boundary crossed | Naive result | With propagation | Does it fail loudly? |
|---|---|---|---|---|
| Python | `run_in_executor` | | | |
| Node.js | pooled callback | | | |
| Go | explicit `ctx` | | | |
| Rust | `tokio::spawn` | | | |
| C++ | `thread_local` on a reused thread | | | |
| Java | `ExecutorService` submit | | | |

Before Part 2, per break: **one orphan trace**, **a truncated trace**, or **two
unrelated complete traces**? And which of the four is hardest to notice on a
dashboard?

| Break | Trace shape observed | Would I have noticed in an incident? |
|---|---|---|
| queue_no_traceparent | | |
| executor_no_ctx | | |
| pricing_fresh_ctx | | |
| collector_strip | | |

**What would mean the experiment is broken rather than your prediction wrong:**

- If every log line has a `trace_id` but they are all identical, your filter
  captured the span context once at import time instead of once per record.
- If no line has one, check the app logger is configured *after*
  `opentelemetry-instrument` patched `logging` — ordering, not configuration.
- If a Loki query by `trace_id` is slow or errors on cardinality, you put it in
  labels instead of structured metadata. Fix that before reading anything into the
  latency.
- If a break produces a *perfect* trace, confirm the break actually applied: the
  container has to be recreated, not just restarted, for a new env var to take.
- If the C++ run prints "lost" rather than "WRONG", your pool created a fresh thread
  per request, so no thread was ever reused and the interesting case never arose.

## Answer before moving on

1. Why is `traceparent` a W3C standard rather than each vendor's own header, and
   what specifically breaks in a multi-vendor mesh without it?
2. A trace ID is high cardinality. Why is that fine for traces and logs and fatal
   for metrics? Answer in terms of how each one is stored, not in terms of cost.
3. Your queue jobs retry up to five times over an hour. Should all five attempts
   share one trace? Argue both sides, then pick, and say what your choice costs you.
4. C++'s `thread_local` failure produces a complete-looking trace with the wrong ID
   attached. Which of the other five runtimes could be made to produce that same
   failure, and what would you have to do to them to get it?

## Next up

[Topic 4 — cardinality, and how one label takes down monitoring](../04-cardinality/README.md):
you now have a trace ID on everything. Putting it in the wrong place is the fastest
way to lose the monitoring system entirely.
