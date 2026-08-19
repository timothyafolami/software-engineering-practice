# Layer 6 · Topic 4 — Cardinality, and how one label takes down monitoring

### The takeaway (read this first)

**The one idea:** a metric's cost is the product of its label values, not how often
you record it — so one unbounded label multiplies your monitoring system by every
distinct user, URL or ID you have ever seen. And in 2026 the first thing that
happens is not an outage. It is *silence*.

**Why it matters in practice:** this failure takes out the system you use to
diagnose failures, so it always lands at the worst possible moment. The modern
version is sneakier than the classic one: the OTel SDK caps each metric stream at a
default number of attribute combinations and folds everything past it into a single
`otel.metric.overflow=true` point. Totals stay correct. Every dashboard that groups
or filters by that label starts undercounting, with no error anywhere.

**You'll know it landed when:** you can estimate a metric's series count from its
label set before shipping it, and you reach for a span attribute or an exemplar the
moment the dimension you want is unbounded.

## The concept

**Series count is a product, and you can compute it on a napkin.** A time series is
one metric name plus one distinct combination of label values. So for a counter with
`route` (say 40 templated routes), `method` (5 in real use) and `status` (8 distinct
codes):

```
40 × 5 × 8 = 1,600 series
```

Fine. Now add `customer_id` with 50,000 distinct values:

```
1,600 × 50,000 = 80,000,000 series
```

Nothing about the *recording* changed — same counter, same call site, same request
rate. The label multiplied. That is the whole mechanism, and the reason it keeps
happening is that the code change is one word long.

Prometheus keeps metadata for every *active* series in memory; the head block, the
inverted index and query fan-out all scale with the count. So the failure is not
gradual degradation of one dashboard, it is the TSDB.

**Two failure modes, and you must be able to tell them apart.**

*SDK-side, silent.* The OpenTelemetry metrics SDK applies a **cardinality limit per
metric stream** — 2000 attribute sets by default, per the spec — and measurements
past the limit are still counted, but their attributes are *deleted* and replaced by
`otel.metric.overflow=true`.
([spec](https://opentelemetry.io/docs/specs/otel/metrics/sdk/#cardinality-limits))
Nothing logs. The symptom is arithmetic: a per-route breakdown that no longer sums
to the total. If you never sum it, you never find out.

*Backend-side, loud.* Memory climbs, `prometheus_tsdb_head_series` goes vertical,
ingestion lags, queries time out, and eventually the process is OOM-killed. The part
people forget: it usually takes the alerting rules with it, so the system stops
paging at the same moment it stops answering questions.

**The fix is never more memory.** It is three moves, in this order:

1. **Bound the label.** Route *templates* (`/orders/{id}`), never raw paths with
   query strings. Status *classes* where the individual code doesn't change what you
   do. Anything derived from user input is unbounded until proven otherwise.
2. **Move the unbounded dimension to a span attribute**, where high cardinality is
   free — that is what traces are for (Topic 1), and wanting `user_id` on a counter
   is the diagnostic that you needed a trace.
3. **Keep the jump from metric to trace with exemplars.** An exemplar attaches a
   trace ID to a histogram bucket sample. It does not create a series, because it is
   not a label — it is a sample annotation, stored alongside the bucket and retrieved
   only when you click. So you get from "the p99 spiked at 14:07" to one specific
   slow request without paying for a dimension.

The deeper rule worth extracting: **cardinality is not a cost you pay, it is a cost
you multiply.** Every other resource question in this lab is additive — more
requests, more bytes, more connections. This one is the only place where a
one-character change to a label set multiplies the bill, which is why it is the only
observability failure that reliably takes out the observability.

## How each language actually gets there

**Python** — the limit is configured per-view on the `MeterProvider` (or globally
via `OTEL_METRIC_CARDINALITY_LIMIT`). The default is the spec's 2000 and there is no
warning log, so you discover overflow by *querying for the attribute*, not by
reading stderr. That is the habit this topic is trying to build: after adding any
label, query `{otel_metric_overflow="true"}` once.

**Go** — the same default, configured through `metric.NewView`. Go's SDK typically
implements spec changes in this area first, so when a behaviour differs between the
two, Go is usually the one that is current and Python the one that is behind.

**Languages: two, deliberately.** The mechanism here lives in the storage engine and
in a spec-mandated SDK default, not in the runtime — six SDKs implementing the same
spec constant would be six copies of the same paragraph. Python and Go are here
because their *configuration surfaces* differ (a view on a provider versus a view
factory) and because they are the two SDKs the lab's own services use. The
collector-side fix below is language-neutral by design, and that is the point of it.

The real safety net belongs at the **collector**, and this is worth stating plainly
because it is the only intervention available during an incident: `transform` and
`attributes` processors can drop, truncate or hash a label for every service at
once. When 30 services are emitting a bad label, you cannot redeploy 30 services.
You can edit one collector config.

## The experiment

Four runs, in this order, so that you meet the silent failure before the loud one.

1. **Silent.** Add `customer_id` to the request counter with the SDK's default limit
   in place. Drive load with 10,000 distinct customers. Query for the
   `otel.metric.overflow` attribute, then compare `sum(rate(...))` against
   `sum by (customer_id) (rate(...))` and record the gap. Predict the gap first: you
   know the limit and you know the customer count.
2. **Loud.** Raise the SDK limit to unlimited, re-run, and sample
   `prometheus_tsdb_head_series`, container memory and range-query wall time every
   30 seconds. Then find the offender the way you would in production — with
   `topk` by series count — rather than by remembering which label you just added.
3. **The Loki version.** Ship `trace_id` as an index label instead of structured
   metadata. Record stream count and query latency. This is the same lesson in a
   different storage engine, and it is the one people actually ship.
4. **Fix and prove.** Drop the label at the collector, add an exemplar carrying
   `trace_id`, and demonstrate that you can still get from a p99 spike to that
   customer's trace in two clicks. The fix is only real if the capability survives.

## How to run

Runs 1 and 3 of the experiment have standalone versions that need no stack, no
SDK and no Docker. They implement the spec's cardinality-limit rule against
200,000 generated requests and 10,000 customers, so the silent failure is
something you can watch happen in two seconds rather than something you take on
trust:

```
python3 python/cardinality_overflow.py
cd golang && go run cardinality_overflow.go
```

The Python one is the whole arc — the napkin product, the silent overflow,
first-seen-wins, which alerts stop firing, bytes per series measured with
`tracemalloc`, and the exemplar that keeps the metric→trace jump after the
label is gone. The Go one is the same mechanism through Go's configuration
surface: three views over one instrument (attribute filter, default limit,
no limit), bytes per series measured with `runtime.MemStats`, and the
collector-side drop and hash rules scored against ground truth. Read the Go
program's row 3 twice — hashing an unbounded label into 16 buckets is the fix
everyone reaches for, and the series arithmetic says it is not one.

Then the real thing, from `lab/` — see [`../lab/README.md`](../lab/README.md):

```
CARDINALITY_DEMO=customer_id docker compose up -d api
docker compose run --rm k6 run /scripts/many_customers.js

# what is actually in the TSDB, ranked
curl -s localhost:9090/api/v1/query --data-urlencode \
  'query=topk(10, count by (__name__)({__name__=~".+"}))' | jq

# the series count for the one counter you are detonating
curl -s localhost:9090/api/v1/query --data-urlencode \
  'query=count({__name__="http_server_requests_total"})' | jq

# did the SDK silently fold your labels?
curl -s localhost:9090/api/v1/query --data-urlencode \
  'query={otel_metric_overflow="true"}' | jq

# run 2: same load with the cap removed
CARDINALITY_DEMO=customer_id OTEL_METRIC_CARDINALITY_LIMIT=0 \
  docker compose up -d api
```

Two of those queries will disappoint you on this stack, and both disappointments
are worth more than the query would have been.

`prometheus_tsdb_head_series` **does not exist here.** The Prometheus inside
`grafana/otel-lgtm` is OTLP-receive-only: it has no `scrape_configs` at all, so it
never scrapes itself, so none of its own `prometheus_*` metrics are in its own TSDB.
The query returns an empty result and no error — the exact failure this layer keeps
warning you about, arriving unannounced in its own material. Use
`count({__name__="http_server_requests_total"})` instead; it counts the thing you
actually care about rather than the whole head block. Watch it while run 2 is going,
not afterwards: the interesting part is the slope, and after an OOM restart the
count is zero again and tells you nothing.

`{otel_metric_overflow="true"}` **stays empty in Python, permanently.** The
cardinality limit is in the OpenTelemetry metrics spec, and the Python SDK does not
implement it — there is no per-stream cap, no `otel.metric.overflow` datapoint, and
`OTEL_METRIC_CARDINALITY_LIMIT` is not a variable the Python SDK reads. Run 1 and
run 2 are therefore the *same run*: both produce the loud failure, unbounded series
growth straight into the TSDB, and neither produces the silent one. The silent
failure is the more important of the two and you cannot see it here. Get it from
Part 1 instead — `python/cardinality_overflow.py` implements the spec's rule
directly, which is why that program exists — or reproduce it against a Go or Java
service, whose SDKs do implement the cap. Check the current state of the Python row
yourself before believing this paragraph; it is the fact on this page most likely to
have moved.

## Predict, then record

Predict, in writing, before run 1: **(a)** the series count for the counter at
10,000 customers, computed from its label set; **(b)** how long before Prometheus
RSS doubles in run 2; **(c)** whether the *silent* run's dashboards look wrong at a
glance.

| Run | head_series | Prom RSS | Range-query latency | Dashboard visibly wrong? |
|---|---|---|---|---|
| baseline | | | | — |
| customer_id, SDK limit 2000 | | | | |
| customer_id, no limit | | | | |
| trace_id as Loki label | | | | |

| Check | Value |
|---|---|
| `sum(rate(...))` total | |
| `sum by (customer_id) (rate(...))` total | |
| Gap between them | |
| Distinct `customer_id` values that survived | |

**What would mean the experiment is broken rather than your prediction wrong:**

- If `head_series` barely moves in run 2, check the collector is not already
  dropping the attribute, and that Prometheus's OTLP translation is not collapsing
  it. The experiment failed to *reach* the TSDB, which is not the theory being
  wrong.
- If run 1 shows no overflow attribute at all, your generator probably sent fewer
  distinct customers than the limit. Count them before doubting the SDK.
- If the two sums in the second table agree exactly in run 1, either the limit did
  not apply or every customer fitted under it — both are setup problems, not
  results.
- If Prometheus OOMs before you get a single sample, lower the customer count and
  re-run. A crash is a confirmation, not a measurement, and you want the curve.

## Answer before moving on

1. You need per-customer latency for your top 20 enterprise accounts. Cardinality
   says no. Design something that works, and state its limitation in one sentence.
2. Why does SDK overflow keep totals correct while breaking breakdowns? What does
   that imply about which of your alerts survive an overflow and which quietly stop
   firing?
3. Exemplars let a metric point at a trace. Why doesn't that reintroduce the problem
   you just spent an afternoon creating?
4. Both failure modes in this topic are caused by the same code change. Design the
   review check — one question, askable in ten seconds — that would have caught it
   in the pull request.

## Next up

[Topic 5 — RED for services, USE for resources](../05-red-and-use/README.md): you can
now build dashboards that survive. Next is knowing which two numbers to put on them,
and why the green one is usually the liar.
