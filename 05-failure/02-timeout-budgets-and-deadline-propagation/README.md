# Layer 5 · Topic 2 — Timeout budgets and deadline propagation

### The takeaway (read this first)

**The one idea:** a timeout is a *local* decision about a *global* resource —
without propagating the remaining budget downstream, inner services keep
burning capacity on requests whose caller already gave up, and possibly
already retried.

**Why it matters in practice:** unpropagated timeouts turn a slow dependency
into an amplifier. Every abandoned request is capacity spent producing a
response nobody will read, taken directly from a request that could still
have succeeded. It is topic 1's `ρ` climbing for work with zero value.

**You'll know it landed when:** for any outbound call in your codebase you can
say what the caller's remaining budget is at that line, and a constant
per-hop timeout reads to you as a bug on sight.

## The concept

**Timeouts must shrink as you go deeper.** Each hop computes

```
budget_out = budget_in − elapsed_here − slack
```

passes it down, and refuses to *start* a call whose budget is below a useful
minimum. Constants defeat this completely: a gateway at 2s with every
internal call also at 2s means that the moment anything is slow, an inner
service is still working on requests the gateway abandoned — holding pool
connections, contributing to ρ, contributing nothing.

Call the wasted work what it is: **zombie work.** It is the cleanest thing to
measure in this whole layer, because it is countable. A request that C
finishes after the gateway has already returned 504 is a zombie completion,
and its cost is exactly one pool slot for exactly one service time.

**The defaults you inherit are worse than you think.** Documented library
defaults, not measurements:

| Client | Default timeout |
|---|---|
| Python `requests` | **none** — hangs forever |
| Python `httpx` | 5s |
| Python `aiohttp` | 300s total |
| Node `fetch`/undici | 300s headers, 300s body |
| Node `http.Server` | `requestTimeout` 300s, `headersTimeout` 60s |
| Go `http.Client{}` / `http.Server{}` zero values | **none** |
| Java `HttpClient` (no `.timeout()` on the request) | **none** |

Five of seven are effectively forever. That is the ecosystem's default
posture and it is the wrong one — infinite patience is the setting that turns
a slow dependency into your outage.

**Cancellation is not the same as stopping work**, and this catches Python
teams specifically. Cancelling a coroutine cancels it at its next `await`;
the query you already sent keeps running on the database server until it
finishes or `statement_timeout` kills it. So a Python deadline discipline has
two halves — an application budget *and* a `statement_timeout` derived from
it. Miss the second and you shed load in your app while Postgres stays
pinned, which is the worst of both: you get the errors and keep the load.

**Absolute versus relative on the wire.** An absolute deadline
(`X-Request-Deadline: <unix millis>`) assumes synchronised clocks across
hosts. A relative one (`grpc-timeout: 480m`, which is what gRPC actually
chose) assumes only that each hop can measure elapsed time locally, and pays
for it by losing the network transit time between hops. Neither is free.
Decide which assumption your fleet can actually keep.

## How each language actually gets there

Six languages, because the thing that differs is exactly what the runtime
does about *cancellation* — whether there is an ambient carrier for a
deadline, and whether cancelling the caller actually stops the callee.

**Python** has no built-in deadline carrier, so you build one: a
`contextvars.ContextVar[float]` holding an **absolute** deadline, set by
middleware from an inbound header, read by an httpx event hook that computes
`timeout = remaining − slack`, and used to issue `SET LOCAL statement_timeout`
inside each transaction, with `anyio.fail_after` enforcing the local bound.
The absolute-deadline-in-a-context is the load-bearing part; a per-call
`timeout=` argument cannot compose, because the third call in a handler has
no idea what the first two spent. And remember the asymmetry above: the
`ContextVar` stops your Python; only `statement_timeout` stops Postgres.

**Node.js** does the same job with primitives that are actually in the
platform: `AbortSignal.timeout(ms)` for the local bound, `AbortSignal.any([...])`
to combine the inbound request's signal with it, and `req.signal` on the
server side so a disconnected client propagates automatically. It is the
closest any runtime here gets to "correct by default" *if* you thread the
signal through — and nothing forces you to, so the failure mode is a forgotten
`{ signal }` argument on one call out of nine.

**Go** is genuinely ahead, and it is worth reading the source of `context`
(it is short) as the reference implementation of this idea. `context.WithTimeout`
produces a value that `database/sql`, `net/http` and every well-behaved
library obey; `r.Context()` is *already* cancelled when the client
disconnects; cancellation propagates down the tree without anyone opting in;
and gRPC serialises the remaining budget on the wire as `grpc-timeout`. The Go
failure mode is the inverse of everyone else's: someone writes
`context.Background()` inside a handler and silently detaches a subtree from
the deadline.

**Rust** has tokio's `timeout(dur, fut)`, which is a genuinely *hard* cancel —
dropping a future stops polling it, so the work really does stop at the next
await point, with no ambient runtime cost. What Rust does not have is an
ambient carrier: there is no `Context` in the standard library, so the
deadline is a parameter you thread through every signature, and the compiler
makes you do it. That is the trade in miniature — Rust makes forgetting
*visible* and makes remembering *verbose*. Note also that a `spawn_blocking`
task cannot be cancelled at all: dropping the handle abandons the result, but
the thread runs to completion. That is a zombie you cannot kill.

**C++** has no cancellation story whatsoever, which makes it the honest
baseline. A deadline is an absolute `std::chrono::steady_clock::time_point`
you pass by value, socket-level enforcement is `SO_RCVTIMEO`/`SO_SNDTIMEO` or
a `poll()` with a computed remaining-millis argument, and a thread that has
entered a blocking call stays there until the kernel returns. Everything the
other runtimes give you — cancellation trees, automatic propagation, drop
semantics — is code you write, and writing it once is the fastest way to
understand what those runtimes are actually doing.

**Java** sits between Go and Rust. There is no ambient context in the platform
(gRPC-Java ships its own `Context`/`Deadline`, and `Deadline` is absolute-time
based); `HttpClient.newBuilder().connectTimeout()` plus
`HttpRequest.newBuilder().timeout()` cover the transport;
`CompletableFuture.orTimeout` covers composition; and JDBC's
`Statement.setQueryTimeout` is the equivalent of `statement_timeout` — with
the same caveat, that it asks the driver to cancel, and what the server does
about it is the server's business. Java 21's `ScopedValue` plus structured
concurrency (`StructuredTaskScope`) is the first thing in the platform that
looks like Go's context tree: a scope that cancels its children when the
deadline passes. If you have virtual threads, this is the shape to build.

## The experiment

A three-hop chain — `gateway → service-b → service-c` — where C holds a
Postgres connection for a configurable duration and the gateway times out at
500ms.

1. Set C's service time to **800ms** and offer **50 rps**.
2. Count **zombie completions** at C: requests C finished *after* the gateway
   returned 504, measured by comparing C's completion timestamp against the
   deadline it was told about (or, in the naive variant, was never told).
3. Record C's pool utilisation and CPU while this is happening — this is the
   number that connects the topic back to topic 1.
4. Implement propagation: the gateway sets `X-Request-Deadline` (absolute
   unix millis); B and C reject immediately with 504 when
   `remaining < 20ms`, set outbound timeouts to `remaining − 20ms`, and issue
   `SET LOCAL statement_timeout` derived from the same number.
5. Rerun identically. Measure zombie completions, C's pool utilisation, and
   the number that actually matters — **gateway success rate at a load where
   the naive version collapsed**.

Output shape:

```
naive       zombie completions/s = <n>   C pool in use = <n>/<total>   gateway success = <pct>
propagated  zombie completions/s = <n>   C pool in use = <n>/<total>   gateway success = <pct>
```

## How to run

Uses the shared harness — services, ports and script paths are specified in
[`../lab/README.md`](../lab/README.md).

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
docker compose --profile chain up -d --build
docker compose run --rm k6 run /scripts/02_chain_naive.js \
  --out csv=/out/02_chain_naive.csv
docker compose exec gateway python -m tools.zombie_report

docker compose run --rm k6 run /scripts/02_chain_deadline.js \
  --out csv=/out/02_chain_deadline.csv
docker compose exec gateway python -m tools.zombie_report
```

Both scripts set C to 800ms, the gateway's budget to 500ms and the offered
rate to 50 rps in `setup()`, so the two runs differ in exactly one flag:
`PROPAGATE_DEADLINE`. Run `zombie_report` after each one — its counters are
per process and are reset by the script that just ran.

The per-language versions each build the same three-hop chain in-process, so
you can see the propagation mechanism without the container stack:

```
python3 python/deadline_chain.py
node nodejs/deadline_chain.js
cd golang && go run deadline_chain.go
cd rust/deadline_chain && cargo run --release
c++ -O2 -std=c++17 -pthread -o /tmp/deadline_chain cpp/deadline_chain.cpp && /tmp/deadline_chain
cd java && javac DeadlineChain.java -d /tmp/javabuild && java -cp /tmp/javabuild DeadlineChain
```

The container chain runs Linux inside Docker Desktop's VM; the six standalone
programs run natively on macOS 27 / arm64.

## Predict, then record

Before running: what fraction of C's completed work will be zombie work in
the naive run, at 50 rps with an 800ms service time and a 500ms budget? Will
propagation raise or lower gateway success rate at the same offered load, and
by how much? What happens to C's Postgres connection count in each version,
and why is that the same variable as topic 1's `L`?

| Variant | gateway success % | zombie completions/s | C pool in use | C p99 |
|---|---|---|---|---|
| naive, constant 500ms per hop | | | | |
| naive, C slow to 800ms | | | | |
| deadline propagated | | | | |
| + `statement_timeout` | | | | |

**What would mean the experiment is broken, not the prediction wrong:**

- **Zero zombies in the naive run.** Check whether your ASGI server cancels
  the handler on client disconnect — behaviour varies by server and version,
  and if it does you are getting partial propagation free at the HTTP layer.
  Confirm the real point with a `def` endpoint or a `pg_sleep`: the database
  work keeps running regardless of what your framework does to the coroutine.
- **The propagated version is *slower*.** Your slack is too large, or you are
  rejecting requests that had enough budget to succeed. Log the
  remaining-budget distribution per hop and look at what you rejected.
- **`statement_timeout` changes nothing.** `SET LOCAL` only holds inside a
  transaction; outside one it silently does nothing at all.
- **Success rate is 100% in both variants.** Your offered load is too low for
  wasted capacity to matter. Zombie work only hurts when the capacity it
  consumes was needed — rerun at 70-80% of topic 1's measured capacity.

## Answer before moving on

1. Absolute deadlines on the wire, or relative durations? Name what each
   assumes about clocks, and which assumption is safer in a fleet you do not
   fully control. gRPC chose relative — argue both sides before you decide.
2. A budget arrives at hop 3 with 40ms left and the call normally takes 30ms.
   Do you make it? What if it normally takes 35ms? Write the rule you would
   put in code, in terms of a percentile rather than a mean.
3. Deadline propagation and retries interact badly in one specific way. What
   is it — what does "retry" even mean when the budget is shared and already
   partly spent?
4. What breaks if a service *extends* the deadline it received, because
   "this call is important"? Trace the consequence all the way back to the
   client.

## Next up

[Topic 3 — Retries that don't become the outage](../03-retries-that-dont-become-the-outage/README.md):
the same wasted capacity, but multiplied on purpose.
