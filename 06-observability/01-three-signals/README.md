# Layer 6 · Topic 1 — The three signals, and the one you are missing

### The takeaway (read this first)

**The one idea:** the three signals sit at three points on a cardinality/retention
tradeoff, and each answers questions only at its own resolution. Metrics: *is
something wrong, and when did it start*. Traces: *where inside one request did the
time go*. Logs: *what exactly happened at that step*. Asking a signal a question
above its resolution is why incidents take two hours.

**Why it matters in practice:** "most people over-log and under-trace" isn't a style
complaint. Over-logging is the *symptom* of having no traces — with no view of a
request's path, the only tool left is printing at every step and reconstructing the
path by hand, at read time, under pressure.

**You'll know it landed when:** given a question ("did the 14:03 deploy cause
this?", "why was *this* request slow?", "what was `discount` when it blew up?") you
can name the answering signal before opening anything.

## The concept

Cardinality is the axis, and the three signals are three answers to the same
question: *how many distinct things are you willing to be able to name?*

A **metric** is a small, fixed set of time series holding compressed numbers. Its
cost is the product of its label values, not how often you record it, so a bounded
label set stays cheap for a year and aggregates across a fleet for free. The same
property that makes it cheap makes it structurally incapable of describing one
request: there is nowhere in a counter to put a request ID.

A **trace** is one request's causal tree. It is high cardinality by construction —
every span carries IDs unique to that request — so it is sampled on the way in and
kept for days rather than years.

A **log line** is an arbitrary blob attached to one moment. Most expressive, most
expensive per unit of insight, and the only one that can carry the value of a local
variable.

You can derive the whole hierarchy from one rule: **storage cost per unique thing
you can name.** Metrics name a handful of dimensions and pay per series. Traces
name every request and pay by sampling. Logs name everything and pay in bytes.

The consequence people miss: **you cannot get a trace's answer out of metrics by
adding labels.** That move is exactly what takes down monitoring, which is Topic 4.
If you want `user_id` on a counter, you have just discovered you needed a trace.

There is a second cost, invisible in that framing and easy to underrate: what each
signal costs the *process* to emit, on the request path, before anything leaves the
machine. That cost is a property of the runtime, not of the signal, and it is where
the six languages come in.

## How each language actually gets there

The first half of this topic — which signal answers which question — is identical in
every runtime, because it is a property of the data model. The second half is not.
Every language wastes work on a *disabled* debug log, and each wastes it in a
different place for a different reason:

| Language | What the disabled debug call actually costs, and why |
|---|---|
| **Python** | The f-string or `json.dumps` runs before `logger.debug` is entered. There is no lazy call form; `isEnabledFor` is the only fix. Also the slowest logger of the six by a wide margin — `logging` walks a handler list and formats through `%`-style records, on the request thread. |
| **Node.js** | Same eager evaluation, but the bill lands on the one thread running every concurrent request, so the ns/op figure is a *concurrency ceiling* rather than a CPU percentage. The program prints that ceiling. |
| **Go** | `slog` evaluates arguments at the call site *and* boxes them into a heap `[]any` before the handler can reject the level. Go is the only one here that reports `allocs/op` from inside the program, so the cost shows up as GC pressure rather than only as nanoseconds. |
| **Rust** | Macros take the *expression*, so a disabled `debug!` never evaluates its argument, and with a `const` level the branch folds away and the call is not in the binary. The only language here where the honest answer is zero. |
| **C++** | The same macro answer, arrived at by necessity rather than design — the standard library ships no logger, and the argument is usually a `std::ostringstream`, which makes the eager form the most expensive row in the topic. |
| **Java** | The SLF4J `{}` placeholder defers *formatting* but not *argument evaluation*, and the varargs call allocates an `Object[]` either way — so the thing everyone believes is the fix is measurably not. Java also shows the JIT: the same log line costs several times more cold than warm, which is why a benchmark with no warm-up measures the interpreter. |

**Languages: all six for Part 2, Python only for Part 1.** Part 1's subject is what
a metric structurally cannot hold, which is the same in every runtime; six copies of
that simulation would teach nothing about six runtimes. Part 2's subject is the
runtime doing work on your request path, which is exactly the case where all six
earn their place.

### Reading zeros honestly

Rust and C++ compile with optimisation, and a row that reads `0.0` means the
compiler deleted the work. That is the **correct** answer for the compile-time-gated
row and a **broken measurement** anywhere else. Both programs therefore feed every
measured value into a `black_box` / `volatile` sink and print it at the end, and
both state in their own output which single row is allowed to be zero. This is a
direct response to Layer 1 publishing an optimizer artefact as a finding.

Two related traps the programs guard against explicitly:

- **Loop-invariant hoisting.** In Rust the runtime level check is loop-invariant, so
  LLVM will hoist it out of the benchmark loop and report zero for a branch that
  costs something per call. The logger is passed through `black_box` to stop that.
  If you edit the file and the guarded row drops to zero, that is the bug, not a
  result.
- **Escape analysis.** Go reports zero allocations for the span row because the span
  never leaves the function and is stack-allocated. A real SDK hands the span to an
  exporter, so it escapes and is heap-allocated. That row is a floor, not an
  estimate, and the program says so in its output.

### What these programs are not

They use hand-rolled metric, span, and log stores, because no OpenTelemetry SDK is
installed on this machine and installing one is out of scope for the standalone
half. They measure the *shape* of the cost — a map lookup, an allocation, a
serialisation, a level check. A real SDK adds context lookup, attribute validation,
view matching, and batching on top of every number here. It never subtracts any of
them.

## The experiment

Three pieces, in this order. The first two run standalone; the third needs the
shared stack.

**Part 1 — what each signal can answer.** `python/three_signals.py` records **one**
incident — a deploy at T+300s that moves 3% of traffic onto an N+1 code path — into
three stores whose data models are as constrained as the real things: a
bounded-cardinality histogram with no `request_id` and no `customer_id`, 10%
head-sampled span trees, and one unsampled JSON log line per request. It then asks
three real incident questions and lets each signal either answer or fail *for a
printed, mechanical reason*:

| Question | Kind of question | Answered by |
|---|---|---|
| Did the deploy cause this? | when | metrics (traces and logs can, expensively) |
| Why was *this* request slow? | where inside one request | traces |
| What was `discount` when it 500'd? | what | logs (traces only if sampled) |

The "cannot answer" lines are computed, not asserted — the program looks for the
field, does not find it, and prints the series count that adding it would cost. The
last section prints bytes stored per signal for the same 3000 requests, which is the
retention argument in three numbers you produced yourself.

Note the deliberate omission: the log lines carry **no `trace_id`**. That is why Q2
cannot be answered from logs even though the timing data exists somewhere in them.
Topic 3 fixes exactly that.

**Part 2 — what each signal costs to emit.** `signal_cost.*` times five or six
operations per language: a counter increment on a bounded label set; creating and
ending a span with six attributes; writing one JSON log line at INFO; a **disabled**
DEBUG call whose argument is built eagerly (the bug); the same disabled DEBUG call,
guarded (the fix); and in Rust and C++ the same again behind a compile-time level.

Operations 4 and 5 produce byte-identical output: none. The gap between them is pure
waste, paid on every request, forever, and invisible in code review because it reads
as "a debug log."

**Part 3 — the time-to-diagnosis race.** Bring the shared stack up, run k6 at 60 VUs
for five minutes against `/orders`, then diagnose the latency three times with a
stopwatch and only one signal available: logs only (Grafana with the Tempo and
Prometheus datasources removed), metrics only, traces only. Record wall-clock time
to a correct, specific statement of cause. Then do it a fourth time with all three
and record which you reached for first. Part 1 is the same questions without the
stopwatch — it shows you which signal was always going to win each one, before you
spend five minutes finding out the hard way.

## How to run

Parts 1 and 2 are standard-library only and take no arguments:

```
python3 python/three_signals.py
python3 python/signal_cost.py
node nodejs/signal_cost.js
cd golang && go run signal_cost.go
cd rust/signal_cost && cargo run --release
clang++ -O2 -std=c++17 -o /tmp/signal_cost cpp/signal_cost.cpp && /tmp/signal_cost
cd java && javac SignalCost.java -d /tmp/javabuild && java -cp /tmp/javabuild SignalCost
```

The Rust and C++ programs accept an optional `--debug` flag, used only to keep the
optimizer from proving the debug level constant; the default no-argument run is the
one to read.

Part 3 needs Docker and the shared stack — see [`../lab/README.md`](../lab/README.md):

```
cd ../lab && docker compose up -d --build
docker compose run --rm k6 run /scripts/steady.js   # 60 VU, 5 min
open http://localhost:3000
```

## Predict, then record

Before Part 2, write down: **(a)** which language pays the most for a disabled debug
line; **(b)** whether any language pays more for a *disabled* debug line than for an
*enabled* info line; **(c)** the ratio between a counter increment and a log line,
in Python.

| Language | counter ns/op | log INFO ns/op | disabled debug, eager | disabled debug, guarded |
|---|---|---|---|---|
| Python | | | | |
| Node.js | | | | |
| Go | | | | |
| Rust | | | | |
| C++ | | | | |
| Java | | | | |

Before Part 3: **(a)** which signal wins; **(b)** how long before you would give up
on metrics-only; **(c)** whether traces-only can name the cause without logs.

| Signal available | Time to correct cause | Cause I named | Right? |
|---|---|---|---|
| Logs only | | | |
| Metrics only | | | |
| Traces only | | | |
| All three | | | |

**What would mean the experiment is broken rather than your prediction wrong:**

- If every Rust or C++ row reads zero, the sink was optimised out and the compiler
  deleted the benchmark — check that the printed sink value is non-zero.
- If Java's numbers are enormous and uniform, the warm-up loop was skipped and you
  measured the interpreter, not the JIT.
- If Python's counter row is slower than its span row, the dict is rehashing inside
  the timing loop rather than being warmed before it.
- If traces-only finishes in under 30 seconds, check that your sampler is at 100%
  *and* that you opened a p99 trace rather than a median one — Tempo's default
  search returns *recent* traces, not slow ones, and diagnosing a median trace is a
  different, useless exercise.
- If metrics-only shows a flat line, check the metric name against the semconv
  renames in [`../lab/README.md`](../lab/README.md) before concluding anything. An
  empty PromQL result is not an error message.

Output shape for Part 2 is one row per operation, per language:

```
counter increment        <your number> ns/op
span create+end          <your number> ns/op   <your number> allocs/op
log INFO (json)          <your number> ns/op
debug disabled, eager    <your number> ns/op
debug disabled, guarded  <your number> ns/op
sink = <non-zero value>
```

## Answer before moving on

1. Name a question about your production service that *no amount* of logging could
   answer, and say what property of logs makes it unanswerable.
2. Traces are sampled and metrics are not. Why is that asymmetry correct design
   rather than a compromise you would undo given more disk?
3. If you had to delete one signal permanently, which goes, and what incident class
   becomes undiagnosable?
4. The disabled-debug row is pure waste in every language. Why has no runtime here
   except Rust and C++ solved it, given that both solved it decades ago and neither
   solution is subtle?

## Next up

[Topic 2 — instrument the slow service and find the real p99](../02-real-p99/README.md):
four separate ways a percentile is wrong before anyone has lied to you, and which of
the five planted defects owns your tail.
