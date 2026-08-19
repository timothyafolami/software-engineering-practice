# Layer 2 · Topic 3 — Timeouts as a first principle

### The takeaway (read this first)

**The one idea:** a network call with no timeout is not a call, it is a
promise to hang forever — and since every waiting request holds a worker, a
pool slot and some memory, one slow upstream converts into total
unavailability of *your* service without your service ever failing at
anything.

**Why it matters in practice:** slow is worse than down. A dependency that
errors in 5 ms lets you fail fast and shed load; one that answers in 30 s
parks your entire concurrency budget. This is the mechanism behind most "the
service just hangs sometimes" reports, and the fix is frequently one line of
missing code.

**You'll know it landed when:** you can state a timeout *budget* for a call
chain — outer deadline, minus time already spent, divided among the calls
that remain — and explain why an inner call must never be allowed to outlive
the request waiting on it.

**Where this topic sits.** This topic is the canonical owner of the
**Toxiproxy fault-injection harness** for the whole lab; layers 4, 5 and 8
reuse the setup below rather than building their own. In the other direction,
[`05-failure`](../../05-failure/README.md) owns the timeout *budget* pattern
(topic 2) and the retry budget with full jitter (topic 3), and owns
metastability as a subject in its own right (topic 4). Here you build the
minimum of each in order to *see* the failure on the wire; go there for the
general treatment.

## The concept

There are four separate timeouts, and conflating them is the usual bug:

- **connect** — time to establish the TCP (and TLS) connection. Small. A
  healthy host inside your VPC connects in single-digit milliseconds, so
  2–3 s is already generous, and anything larger is not a timeout, it is a
  delay before you find out.
- **read** — time to the *next* byte, not to the end of the response. This
  distinction is load-bearing: a server that trickles one byte every second
  never trips a per-read timeout, no matter how long the response takes.
- **total / deadline** — the whole operation, including retries. This is the
  one your caller cares about and the one almost no library gives you.
- **pool** — time spent waiting for a free connection. Topic 2's hidden
  queue. Almost nobody sets it, which is why Topic 2's incident looks like a
  hang rather than an error.

Then the principle that matters: **timeouts must shrink as you go deeper.**
If the client gave you 3 s and you have already spent 400 ms, the upstream
call gets at most 2.6 s minus whatever you reserve for your own work and for
writing the response — not the 5 s sitting in the library default. Work an
example all the way through:

```
client deadline                     3000 ms
  already spent parsing/auth        - 400 ms
  reserve for own work + response   - 200 ms
                                    = 2400 ms left for three sequential calls
  call A                              800 ms
  call B    (starts with what A left, not with a fresh 800)
  call C    (whatever remains; if it is negative, fail now, do not call)
```

The last line is the part people skip. A deadline is a value you *propagate*
and re-check, not a constant you configure once. gRPC and Go's
`context.Context` make it first-class; Python does not, so you build it, and
it is worth building.

**Retries are part of the budget, not separate from it.** A naive retry policy
— three attempts, no backoff, no cap — multiplies load by four at exactly the
moment your dependency can least absorb it. That is the
[metastable failure](https://sigops.org/s/conferences/hotos/2021/papers/hotos21-s11-bronson.pdf)
mechanism (Bronson et al., HotOS '21): a transient trigger raises load,
retries amplify it, and the system stays collapsed *after the trigger is gone*
because the retry load has become the new trigger. The fix has four parts and
you need all four:

1. exponential backoff,
2. **full jitter** — sleep a uniform random value in `[0, backoff]`, not
   `backoff ± noise`,
3. a hard attempt cap,
4. a **retry budget**: retries may not exceed some small fraction of base
   requests across the whole client, tracked with a token bucket. The AWS
   Builders' Library article *Timeouts, retries and backoff with jitter* and
   Google's SRE book both put that fraction around ten per cent; cite the one
   you actually read rather than this page.

The budget is the part everyone omits and the part that actually stops the
storm, because backoff and jitter only spread the retries out — they do not
reduce how many there are.

## How each language actually gets there

The runtime is the subject: "what is a timeout, mechanically" has six
different answers here, and C++'s is the one that explains the other five.

**Python.** The defaults are not what you would hope.

- `requests`: **no timeout at all**, unless you pass one. Infinite. The single
  most consequential default in the Python ecosystem. `timeout=(3, 10)` means
  `(connect, read)`, and `read` is per-socket-read, so a slow byte trickle can
  exceed it indefinitely.
- `httpx`: 5 s on connect, read, write and pool — a genuinely good default,
  settable separately with `httpx.Timeout(5.0, connect=2.0)`. `timeout=None`
  disables it, which people do while debugging and then commit.
- `aiohttp`: `ClientTimeout(total=5*60)` — five minutes. Technically a
  timeout, practically infinite. Set `total`, `sock_connect` and `sock_read`
  explicitly. Verify the current shape rather than trusting this README:
  `python -c "import aiohttp; print(aiohttp.ClientTimeout())"`.

Mechanically, an asyncio timeout is *cancellation*: `asyncio.timeout()` throws
`CancelledError` into the coroutine at its next suspension point. Nothing is
forcibly stopped; the coroutine simply stops being resumed.

**Node.js.** undici's `headersTimeout` and `bodyTimeout` are on the order of
five minutes, so the per-call answer is `AbortSignal.timeout(ms)` passed into
`fetch`. Node's mechanism is a timer on the event loop plus an abort signal —
which means a timeout cannot fire while the loop is blocked. A CPU-bound
handler in the same process will delay your timeouts along with everything
else, and the timeout you configured becomes a lower bound rather than a
bound. That is Layer 1's blocking-the-loop failure wearing a timeout costume.

**Go.** The zero value `http.Client{}` has **no timeout** — the same trap as
`requests`. But Go has the right tool for budgets: `context.WithTimeout` on
the incoming request, propagated into every outgoing call, so cancellation
flows down the call tree automatically and a cancelled parent cancels its
children without any bookkeeping from you. Read this even though you write
Python: it is the clearest existing model of deadline propagation, and it is
exactly what you are hand-rolling when you thread a `deadline` object through
FastAPI services.

**Rust.** `reqwest` has no default total timeout; you set one per client or
wrap any future in `tokio::time::timeout(dur, fut)`. The mechanism is the
sharpest of the six: a timeout is a future that races another future, and
losing means the loser is **dropped**. Dropping cancels it at whatever await
point it was parked on — which raises *cancellation safety* as a real design
concern, because a half-written request that gets dropped mid-`write_all` may
leave the connection in an unusable state. The compiler will not warn you.
This is the useful contrast: Python and Rust cancel by declining to resume,
while Go and Java must ask the operation to stop and hope it agrees.

**C++.** There is no timeout abstraction, so you see what one actually is.
Either it is an argument to a syscall — the last parameter of `poll()`, the
`struct timeval` in `select()`, `SO_RCVTIMEO` set with `setsockopt` — or it is
a timer you run yourself and a socket you close from another thread. Two
consequences fall straight out and they explain every other runtime in this
list: a "timeout" is really *something else waking you up*, and after it fires
you still hold a connection in an unknown state that you must decide what to
do with. libcurl's `CURLOPT_TIMEOUT_MS` and `CURLOPT_CONNECTTIMEOUT_MS` are
the same two ideas with a nicer surface.

**Java.** `HttpClient.Builder.connectTimeout` covers the connect phase and
`HttpRequest.Builder.timeout` covers the whole exchange; there is no separate
read timeout, which surprises people coming from `HttpURLConnection`'s
`setReadTimeout`. The mechanism is interruption: a blocking call ends early
only if it honours interrupts, so a `synchronized` block or a native call that
does not is genuinely uninterruptible. Java 21's virtual threads make
"one thread parked per in-flight request" cheap enough to stop being the
implicit limiter, which moves the pressure onto the timeout you set — and
structured concurrency's `StructuredTaskScope` (still a preview API in the
JDKs this lab targets; check whether your JDK needs `--enable-preview`)
gives a scope-wide deadline that cancels all children, which is the closest
thing on the JVM to Go's context tree.

## The experiment

`lab/topic3/` — `api` exposes `/order`, which calls `upstream` through
Toxiproxy. Four scenarios, each driven at a constant 150 rps for three
minutes, with the toxic switched **on at t=60 s and off at t=120 s**:

1. **No timeout** — the `requests` default. Watch total unavailability, and
   then watch it persist after the fault is removed.
2. **Flat timeout** — 5 s everywhere regardless of depth. Better, and still
   lets an inner call outlive the outer request that is waiting on it.
3. **Budget** — a `Deadline` built from an incoming `X-Request-Deadline`
   header (or `now + 3 s` when absent), passed down, each call receiving
   `min(remaining - reserve, cap)`. Requests fail fast and early.
4. **Budget + retry with full jitter + retry budget + circuit breaker.** The
   whole pattern, in a form you could paste into a pull request.

The critical measurement is **not** p99 during the fault. It is **time to
recovery after the fault is removed**. Scenario 1 should not recover promptly.
That gap is metastability, watched live, on your own machine.

The single-file programs isolate the two halves:
[`python/deadline_budget.py`](python/) is deadline propagation with no server
involved, and [`python/retry_storm_and_budget.py`](python/) shows the
amplification factor with and without a token-bucket budget. The Go, Node,
Rust, C++ and Java directories each hold the same call chain expressed in that
runtime's cancellation mechanism, which is the comparison worth making: what
does each one actually *do* to an in-flight request when the deadline passes?

Each of those five runs the same four-phase experiment against a server it
starts in its own process — a budget spent down three hops, then a fired
timeout, then a check of whether the connection survived it — and each adds
the one demonstration only that runtime can make: Node blocks its own event
loop and watches a 100 ms timeout fire hundreds of milliseconds late; Rust
cancels mid-write and then reads the *previous* request's response off the
reused connection; C++ does the same thing by the opposite route, taking a
late reply out of the receive buffer, and contrasts `poll()` with
`SO_RCVTIMEO`; Java interrupts one thread that honours it and one that
declines to.

## How to run

```
cd 02-network/lab
TIMEOUT_PROFILE=none docker compose up -d api upstream toxi
docker compose run --rm load run /scripts/topic3.js
curl -X POST localhost:8474/proxies/upstream/toxics \
  -d '{"type":"latency","attributes":{"latency":20000,"jitter":2000}}'
```

Repeat with `TIMEOUT_PROFILE=flat`, `budget` and `full` — the four scenarios
above, in that order. `TIMEOUT_PROFILE` is this topic's selector on the `api`
container, alongside the `POOL_PROFILE` / `KEEPALIVE_PROFILE` / `PROTO`
variables listed in [`../lab/README.md`](../lab/README.md); it is read once at
startup, so the container has to be recreated between scenarios rather than
signalled.

Remove the toxic with `curl -X DELETE localhost:8474/proxies/upstream/toxics/latency`,
and watch recovery — not the fault window — in `curl -s localhost:8000/stats`
(`retries`, `retries_denied_by_budget`, `breaker_open_rejections`,
`upstream_timeouts`). Service names, ports and the pre-flight check are in
[`../lab/README.md`](../lab/README.md).

The single-file programs, from this topic's directory. The first two are the
budget arithmetic and the retry storm; the other five are the same question —
*what does this runtime actually do to an in-flight request when the deadline
passes, and can the connection be reused afterwards* — asked once per
cancellation mechanism:

```
python3 python/deadline_budget.py && python3 python/retry_storm_and_budget.py
node nodejs/abort_signal_and_the_blocked_loop.js
cd golang && go run context_deadline_chain.go
cd rust/cancellation_safety && cargo run --release
c++ -O2 -std=c++17 -pthread -o /tmp/polldeadline cpp/poll_deadline.cpp && /tmp/polldeadline
cd java && javac TimeoutIsAnInterrupt.java -d /tmp/javabuild && \
  java -cp /tmp/javabuild TimeoutIsAnInterrupt
```

The Python pair needs `pip install -r python/requirements.txt`; the Rust
project needs only tokio; everything else is standard library. Each program
prints a two-line summary at the end in exactly the shape of the second table
below — copy it in rather than paraphrasing it.

## Predict, then record

- Time from fault onset to the first client-visible error, scenario 1: ______
- Time from fault *removal* to full recovery, scenario 1: ______
- Extra requests per second generated by naive retries at 150 rps: ______
  (derive it first: three retries with no cap on every failed request is 4×
  base, so predict the arithmetic and then check the arithmetic)

| Scenario | p99 before | p99 during | error % during | recovery after |
|---|---|---|---|---|
| no timeout | | | | |
| flat 5 s | | | | |
| budget | | | | |
| budget + retry + breaker | | | | |

| Runtime | what a fired timeout does to the in-flight request | connection reused after? |
|---|---|---|
| Python (asyncio) | | |
| Node (AbortSignal) | | |
| Go (context) | | |
| Rust (tokio::time::timeout) | | |
| C++ (poll deadline) | | |
| Java (HttpRequest.timeout) | | |

**What would mean the experiment is broken, not the prediction wrong:**

- Scenario 1 recovers instantly → closed-model load again, so no backlog ever
  accumulated. Only open-model load produces metastability.
- No difference between flat and budget → the call chain is too shallow. The
  budget only pays off at two hops or more.
- The breaker never opens → check its window and threshold, not its
  correctness. A breaker sized for 1000 rps sees nothing at 150.
- Errors appear *immediately* at fault onset in scenario 1 → you have a
  timeout you did not know about: uvicorn's, nginx's `proxy_read_timeout`, or
  k6's own client. Find it. That is the real lesson of this topic — something
  in your stack always has a timeout, and if you did not choose it, you do not
  know what it is.
- The retry-amplification number is exactly your predicted multiple with no
  variance → the retries are not actually reaching the upstream. Count
  requests at `upstream`, not at `api`.

## Answer before moving on

1. Your API promises p99 under 800 ms and makes three sequential upstream
   calls. Assign a timeout to each and justify the arithmetic, including what
   you reserve for your own work and for writing the response.
2. Backoff alone is insufficient. Describe the specific failure that jitter,
   and only jitter, prevents — and say why adding jitter to backoff does not
   remove the need for a budget.
3. A circuit breaker opens and starts failing fast. Name a situation where
   that makes an incident strictly worse than having no breaker at all.
4. Rust cancels a timed-out request by dropping the future; Java cancels by
   interrupting a thread that may decline to notice. For each, describe a
   concrete case where the connection cannot safely be returned to the pool
   afterwards.

## Next up

[Topic 4 — Keep-alive across a load balancer](../04-keep-alive-across-a-load-balancer/README.md):
two components with correct timeouts, disagreeing about which of them closes
an idle connection first.
