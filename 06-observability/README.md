# Layer 6 · Observability and operating

> **You own this when:** a production incident starts and you go to a dashboard
> rather than to a hunch, and you can say within a few minutes which layer is lying
> to you.

You have a live latency problem right now. That is the best reason to do this layer
and the worst reason to skim it. The temptation is to skip to "make a Grafana
dashboard." A dashboard is the *last* thing you build — it is a question you already
know how to ask, and right now you do not know the question. Every topic below makes
one specific question answerable. Topic 2 is yours.

Layer 1 taught you why a blocking call inside an `async def` stalls every concurrent
request on that process. This layer teaches you to *see* it happening, in a service
you did not write, at 3am, from a dashboard, in under five minutes.

| # | Topic | Folder |
|---|---|---|
| 1 | The three signals, and the one you are missing | [`01-three-signals/`](01-three-signals/README.md) |
| 2 | Instrument the slow service and find the real p99 | [`02-real-p99/`](02-real-p99/README.md) |
| 3 | Correlation IDs and the one-query test | [`03-correlation-ids/`](03-correlation-ids/README.md) |
| 4 | Cardinality, and how one label takes down monitoring | [`04-cardinality/`](04-cardinality/README.md) |
| 5 | RED for services, USE for resources, and the seam between them | [`05-red-and-use/`](05-red-and-use/README.md) |
| 6 | SLIs, SLOs and error budgets: reliability as a number | [`06-slos-and-error-budgets/`](06-slos-and-error-budgets/README.md) |
| 7 | Alert on symptoms, and write the postmortem that changes the system | [`07-symptoms-and-postmortems/`](07-symptoms-and-postmortems/README.md) |

## The shared lab

One `docker compose` stack in [`lab/`](lab/README.md), used by all seven topics:
`api`, `worker`, `pricing`, `db`, `otelcol`, `lgtm`, `k6`. Built once in Topic 1;
later topics change a config value or inject a fault. `api` ships with **five
planted defects**, and you are not told which one owns the p99 — that is Topic 2.

[`lab/README.md`](lab/README.md) holds the service table, the defect flags, every
environment variable and k6 script name, the macOS/arm64 notes for anything
cgroup-shaped, and the August 2026 version facts (semantic-convention renames,
Prometheus 3's OTLP receiver, Promtail's end of life). Read it before Topic 1 and
refer back rather than trusting a 2023 tutorial.

`SEQUENCE.md` splits this layer three ways: topics 1-3 come first in the running
order, 4-5 attach to Layer 5, and 6-7 attach to Layer 8 — because SLOs and
postmortems need incidents to have happened first.

## On the language set

Six languages are available — Python, Node.js, Go, Rust, C++, Java — and this layer
uses all six exactly where the runtime is the subject:

- **Topics 1, 2 and 3 use all six.** What a signal costs to emit, what it costs to
  hold a request in flight, and how implicit context survives a concurrency boundary
  are all properties of the runtime, and the six answers are genuinely different.
- **Topic 5 uses four** (Python, Node, Go, Java), because the observable is what a
  connection pool tells you about its own saturation, and Rust and C++ have no
  conventional pool whose defaults teach anything.
- **Topics 4, 6 and 7 are narrower still**, because their mechanisms live outside
  the process entirely — in the TSDB's index, in PromQL arithmetic, in alert-rule
  design. Each topic states its reason in one line.

## The "you own this" test

The test at the top of this page is answered by the layer in two halves. Topics 1-3
give you the *seeing* — which signal answers which question, where the time actually
went, and how to get from one slow request to the lines that explain it. Topics 4-7
give you the *operating* — keeping the monitoring alive, knowing which number on a
green dashboard is lying, spending reliability deliberately, and paging only for
things that happened to a person.

The obligation this layer carries that the others do not is in
[Topic 7's closing note](07-symptoms-and-postmortems/README.md#next-up): point this
collector at your actual service for one afternoon and run Topic 2's procedure
against real traffic.
