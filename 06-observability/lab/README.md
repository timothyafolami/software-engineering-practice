# Layer 6 · The shared lab

One `docker compose` stack, used by all seven topics. You build it once in Topic 1;
every topic after that changes a config value, flips an environment variable, or
injects a fault. Nothing here is a toy: it is a small production-shaped Python
service with real defects in it, observed by a real collector, stored in real
backends.

Topic READMEs reference the names on this page rather than restating them. If you
rename a service, a script, or a defect flag, rename it here first — later code
depends on these exact strings.

---

## Services

| Service | What it is | Why it's there |
|---|---|---|
| `api` | FastAPI + uvicorn, SQLAlchemy 2.x, psycopg3, under `opentelemetry-instrument` | Your stack |
| `worker` | Background consumer on a Postgres-backed queue | Where trace context breaks |
| `pricing` | Tiny Go service with a deliberate latency tail | The downstream that lies |
| `db` | `postgres:18` | The layer everyone blames first |
| `otelcol` | Collector contrib: spanmetrics, tail sampling, memory limiter | The part you must read |
| `lgtm` | `grafana/otel-lgtm` | Grafana + Prometheus + Tempo + Loki + Pyroscope |
| `k6` | Grafana k6 v1.x load generator | Real traffic, not a for-loop |

**Ports.** `api` on 8000, Grafana on 3000, Prometheus on 9090, Tempo on 3200, the
collector's own metrics on 8888, and OTLP into `otelcol` on 4317 (gRPC) and 4318
(HTTP). The `lgtm` container exposes OTLP on the same two port numbers; the whole
point of running `otelcol` in front of it is that you write that collector config
yourself instead of inheriting Grafana's.

`pricing` publishes 8081, which is a popular port. Nothing in the lab reaches
`pricing` from the host — `api` talks to it over the compose network — so if
something else on your machine already holds 8081, move the host side and carry on:
`PRICING_HOST_PORT=8181 docker compose up -d`.

**Postgres 18** matches the pin Layer 3 sets (18.6 is the current stable line at the
time of writing) so that a query plan you learn there is the same plan you get here.
k6 is pinned to the **v1.x** line, matching Layer 4.

---

## The five planted defects

`api` ships with five defects, all real things that have been in real Python
services. You are not told which one owns the p99 — that is Topic 2's entire
exercise. Each can be disabled independently, by name:

| Flag value | The defect |
|---|---|
| `n_plus_one` | An ORM N+1 on the list endpoint |
| `sync_http_in_async` | A synchronous `requests.get()` to `pricing` inside an `async def` handler |
| `small_pool` | `pool_size=5, max_overflow=0, pool_timeout=30` against a 60-VU load test |
| `missing_index` | No index on `orders(customer_id, created_at)` over ~2M rows |
| `pricing_tail` | `pricing` returns in 8ms for 99 requests and 900ms for the 100th |

```
DEFECT_DISABLE=n_plus_one docker compose up -d api
```

Disable exactly one at a time and always revert. The measurement you want is the
delta on p50 *and* p99 separately — a defect can own one and not the other, which
is the finding.

---

## Environment variables

| Variable | Used by | What it does |
|---|---|---|
| `DEFECT_DISABLE` | Topic 2 | Turns off one planted defect by name (table above) |
| `BREAK` | Topic 3 | Breaks trace propagation one way at a time. Read the service column carefully — two of the three do not go where the name suggests. `queue_no_traceparent` goes on **`api`**, the producer that writes the column, not on `worker`, which only reads it. `executor_no_ctx` goes on `api` and only does anything **together with `DEFECT_DISABLE=sync_http_in_async`**, because the blocking client short-circuits the executor path it breaks. `pricing_fresh_ctx` goes on `pricing` and works alone. The fourth break, `collector_strip`, is **not** an env var — a collector config has no conditionals, so it is a commented-out processor in `otelcol/config.yaml` that you uncomment. |
| `CARDINALITY_DEMO` | Topic 4 | Adds an unbounded label to the request counter, e.g. `customer_id` |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | Topics 2, 5 | Standard OTel sampler knobs; Topic 5 runs `parentbased_traceidratio` at `0.1` |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | Topics 1, 2, 5 | `http`, `http/dup`, `database`, `database/dup` — the migration lever between old and current attribute names |
| `OTEL_METRIC_CARDINALITY_LIMIT` | Topic 4 | Intended to raise or remove the SDK's per-stream attribute-set cap. **Inert here**: the cardinality limit is in the metrics spec but is not implemented by the Python SDK, which reads no such variable and emits no `otel.metric.overflow` datapoint. Topic 4's loud failure works; its silent one has to come from that topic's standalone program or from a Go/Java service. |

---

## Fault injection

`api` exposes a fault endpoint used by Topics 6 and 7. It is the only endpoint in
the service that exists for the lab rather than for the pretend business:

```
POST localhost:8000/_fault
  {"mode":"outage",      "seconds":180}
  {"mode":"error_rate",  "ratio":0.08, "seconds":14400}
  {"mode":"pricing_tail"}
```

---

## k6 scripts

Mounted into the `k6` container at `/scripts`:

| Script | Shape | Used by |
|---|---|---|
| `steady.js` | Closed-loop, 60 VU, 5 minutes | Topics 1, 2 |
| `arrival.js` | `constant-arrival-rate`, 300 RPS | Topics 2, 3 |
| `ramp.js` | 10 → 120 VU over 10 minutes | Topic 5 |
| `many_customers.js` | 10,000 distinct customer IDs | Topic 4 |

`steady.js` and `arrival.js` are the same requests under two generator designs, and
Topic 2 is about why that difference changes the answer.

For k6's OpenTelemetry metric output, check `k6 --out help` for the exact output
name your build uses — it moved out of the `experimental-` prefix during the v1
line, and the two spellings are a common five-minute confusion.

---

## Bringing it up

```
cd lab && docker compose up -d --build
docker compose run --rm k6 run /scripts/steady.js
open http://localhost:3000
```

First build seeds 2,000,000 rows into `orders` and 4,000,001 into `order_items`,
which takes about 95 seconds on an M1 and only happens once. Bring the stack down
with `docker compose down` and keep the volume; use `down -v` only when you want the
seed to run again.

Two things to know before the first `up`, both of which cost an hour if you meet
them cold:

- **`db` reports healthy before it accepts TCP connections**, if you let it. While
  the scripts in `/docker-entrypoint-initdb.d` run, the entrypoint has a temporary
  server up on the unix socket only, and a bare `pg_isready` is happy with that.
  The healthcheck here asks over `127.0.0.1` for exactly that reason. Anything that
  waits on `service_healthy` is trusting that check to mean what its dependents need.
- **`worker` runs under `opentelemetry-instrument` like `api` does.** It overrides
  the image's command, and an override that drops the `opentelemetry-instrument`
  prefix disables the whole SDK silently — no spans, no `trace_id` on any log line,
  and no error.

---

## Running this on macOS 27, arm64

The host is macOS on Apple silicon, which matters in exactly two places.

**Anything cgroup- or `/proc`-shaped must run inside a container, not in your
shell.** Topic 5 reads `/sys/fs/cgroup/cpu.max` and CFS throttling counters. Those
files exist inside the Linux VM that Docker Desktop runs, so `docker compose exec`
into a container and read them there. On the macOS side those paths do not exist at
all and the failure looks like an empty result rather than an error, which is the
worst kind:

```
docker compose exec api cat /sys/fs/cgroup/cpu.max     # works
cat /sys/fs/cgroup/cpu.max                             # no such file, on the host
```

**Check arm64 images.** Everything above publishes arm64 images, but if you swap a
component and compose starts emulating, every latency number in this layer becomes
a measurement of qemu. `docker compose config --images` then `docker image inspect
<image> --format '{{.Architecture}}'` is the check.

---

## The state of the world, August 2026

The version facts this lab is built on. Recorded because most tutorials you will
find predate them, and each one costs a day when it bites.

- **Semantic conventions renamed the attributes your dashboards key on.**
  `http.method` → `http.request.method`, `http.status_code` →
  `http.response.status_code`, and the server latency metric became
  `http.server.request.duration` in **seconds** (was `http.server.duration` in
  **milliseconds**) with different default buckets. On the DB side `db.system` →
  `db.system.name`, `db.statement` → `db.query.text`, plus a new
  `db.client.operation.duration`. `OTEL_SEMCONV_STABILITY_OPT_IN` is the migration
  lever and is scheduled for removal once stable is the default. PromQL against a
  metric name that no longer exists returns empty, not an error — the most common
  way to build a dashboard that "works" and shows nothing.
  ([semconv HTTP migration](https://opentelemetry.io/docs/specs/semconv/non-normative/http-migration/))
- **OTel logs are stable in the spec, not in every SDK.** Traces and metrics are
  stable for Python/Go/JS; the logs SDK is still below that bar in Python and JS
  (note the underscore in `opentelemetry.sdk._logs`). Emit JSON to stdout and ship
  it rather than betting a pipeline on it. Check the current row yourself at
  [opentelemetry.io/status](https://opentelemetry.io/status/) — this is the fact on
  this page most likely to have moved since it was written.
- **Promtail is end-of-life.** Every "Loki + Promtail" tutorial is stale; use
  Grafana Alloy or push straight to Loki's OTLP endpoint.
- **Prometheus 3 receives OTLP directly** at `/api/v1/otlp/v1/metrics`, but it is
  **off by default** (`--web.enable-otlp-receiver`), since Prometheus has no auth
  layer of its own. OTLP name translation defaults to
  `UnderscoreEscapingWithSuffixes`, so you query
  `http_server_request_duration_seconds`. Delta→cumulative conversion is
  experimental behind `--enable-feature=otlp-deltatocumulative`.
- **Jaeger v2 is built on the OTel Collector** and the old Jaeger clients are
  retired. "Jaeger vs OpenTelemetry" is no longer a choice you make.
- **`grafana/otel-lgtm` is the fastest correct dev stack** — one container,
  Grafana + Prometheus + Tempo + Loki + Pyroscope. Grafana say dev/demo/test only,
  and they mean it: no auth, no retention policy, no HA.
- **Profiles is the fourth signal, in public alpha.** OTLP profiles, collector
  pipelines, an eBPF profiler donated by Elastic. Know it exists; the SIG's own
  advice is not to put critical workloads on it yet.
- **The roadmap's reading still holds, with one correction.** The SRE book and
  workbook remain primary for SLOs — the workbook's *Alerting on SLOs* chapter
  especially. What is superseded is *instrumentation* mechanics from that era
  (OpenCensus, Jaeger clients). For instrumentation go to opentelemetry.io, which
  is good and versioned.

Back to [the layer index](../README.md).
