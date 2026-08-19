# Layer 6 · Verification record

**Date:** 2026-08-19
**Verified by:** an independent pass — every program below was compiled and run
from scratch on this machine, using the exact command printed in its topic
README's *How to run* section. Nothing here is taken from the author's report.

## The machine

| | |
|---|---|
| OS | macOS 27.0 (build 26A5406e), Darwin 27.0.0 |
| Arch | arm64, Apple M1 |
| Python | 3.13.5 |
| Node.js | v24.14.0 |
| Go | go1.24.5 darwin/arm64 |
| Rust | rustc 1.97.1 (8bab26f4f 2026-07-14), cargo same line |
| C++ | Apple clang 21.0.0 (clang-2100.1.1.101) — `clang++`, `-pthread` works |
| Java | JDK 21.0.2 (`javac`/`java`), virtual threads available |
| Docker | CLI 28.1.1 present, **daemon DOWN** (`docker info` fails) — see the unblock pass at the end of this file, where it is up |
| k6 | **not installed** on the host (the `k6` compose service is how the scripts run) |
| Postgres | `psql`/`pg_isready` present; a local server answers on `/tmp:5432`. Nothing in this layer's standalone code touches it. |

Note for anyone repeating this: there is no `timeout(1)` on stock macOS. Every
run below was wrapped in a hand-rolled `perl` alarm at 120 s. Nothing came
close: the slowest single program is 15 s.

## What this record does and does not say

It says **the code executes**: it compiles, it runs to completion without
error, and it prints the output its header comment says it will print. It does
**not** say anything was learned. The `Predict, then record` tables in every
topic README were checked and are **still blank** — they are the reader's
exercise and filling them in from someone else's run would destroy the point of
them.

No benchmark figure appears in any topic README. The only numbers in the
READMEs are design parameters (pool size 5, 200 req/s, 40 routes × 5 methods ×
8 statuses) and arithmetic you can check by hand; all of it was re-checked and
is correct.

## Every program

Timings are wall clock for the whole command, compile included, on an
otherwise-idle machine.

### Topic 1 — three signals

| Program | Command (from `01-three-signals/`) | Status | Time |
|---|---|---|---|
| `python/three_signals.py` | `python3 python/three_signals.py` | RAN | <1 s |
| `python/signal_cost.py` | `python3 python/signal_cost.py` | RAN | 2 s |
| `nodejs/signal_cost.js` | `node nodejs/signal_cost.js` | RAN | <1 s |
| `golang/signal_cost.go` | `cd golang && go run signal_cost.go` | RAN | 1 s |
| `rust/signal_cost` | `cd rust/signal_cost && cargo run --release` | RAN | 1 s |
| `cpp/signal_cost.cpp` | `clang++ -O2 -std=c++17 -o /tmp/signal_cost cpp/signal_cost.cpp && /tmp/signal_cost` | RAN | 4 s |
| `java/SignalCost.java` | `cd java && javac SignalCost.java -d /tmp/javabuild && java -cp /tmp/javabuild SignalCost` | RAN | 1 s |

The zero-guard the README promises is real and works. Rust's and C++'s
compile-time-gated rows print `0.0`; every other row is non-zero and the sink
value is printed non-zero at the end of both programs. Go reports
`0.00 allocs/op` for the span row and says in its own output that this is escape
analysis and therefore a floor, not an estimate.

### Topic 2 — the real p99

| Program | Command (from `02-real-p99/`) | Status | Time |
|---|---|---|---|
| `python/histogram_lies.py` | `python3 python/histogram_lies.py` | RAN | 2 s |
| `python/coordinated_omission.py` | `python3 python/coordinated_omission.py` | RAN | 10 s |
| `nodejs/coordinated_omission.js` | `node nodejs/coordinated_omission.js` | RAN | 10 s |
| `golang/coordinated_omission.go` | `cd golang && go run coordinated_omission.go` | RAN | 10 s |
| `rust/coordinated_omission` | `cd rust/coordinated_omission && cargo run --release` | RAN | 11 s |
| `cpp/coordinated_omission.cpp` | `clang++ -O2 -std=c++17 -pthread -o /tmp/coordinated_omission cpp/coordinated_omission.cpp && /tmp/coordinated_omission` | RAN | 13 s |
| `java/CoordinatedOmission.java` | `cd java && javac CoordinatedOmission.java -d /tmp/javabuild && java -cp /tmp/javabuild CoordinatedOmission` | RAN | 15 s |

The Java program's three phases take about 15 s, exactly as its README says.
The Python, Rust and C++ open-loop generators really do spawn ~1000 OS threads
on this machine and complete; nothing hit a thread limit.

### Topic 3 — correlation IDs

| Program | Command (from `03-correlation-ids/`) | Status | Time |
|---|---|---|---|
| `python/lose_the_context.py` | `python3 python/lose_the_context.py` | RAN | 1 s |
| `nodejs/lose_the_context.js` | `node nodejs/lose_the_context.js` | RAN | <1 s |
| `golang/lose_the_context.go` | `cd golang && go run lose_the_context.go` | RAN | <1 s |
| `rust/lose_the_context` | `cd rust/lose_the_context && cargo run --release` | RAN | 1 s |
| `cpp/lose_the_context.cpp` | `clang++ -O2 -std=c++17 -pthread -o /tmp/lose_the_context cpp/lose_the_context.cpp && /tmp/lose_the_context` | RAN | 1 s |
| `java/LoseTheContext.java` | `cd java && javac LoseTheContext.java -d /tmp/javabuild && java -cp /tmp/javabuild LoseTheContext` | RAN | 1 s |

The Rust crate is the only one in the layer with a dependency (tokio). Its
offline build was checked from a clean tree: `target/` deleted, then
`cargo build --release --offline` — it resolves entirely from the local crate
cache and finishes in about 6 s. The README's `cargo run --offline --release`
fallback is accurate.

The C++ and Java programs both reach the interesting case rather than the
boring one — the README warns that if the pool creates a fresh thread per
request you get `lost` instead of `WRONG`. On this machine both print
`WRONG (inherited from previous request)` with an explicit `<-- MISMATCH` row,
deterministically.

### Topic 4 — cardinality

| Program | Command (from `04-cardinality/`) | Status | Time |
|---|---|---|---|
| `python/cardinality_overflow.py` | `python3 python/cardinality_overflow.py` | RAN | 2 s |
| `golang/cardinality_overflow.go` | `cd golang && go run cardinality_overflow.go` | RAN | <1 s |

Both label their bytes-per-series extrapolation as a multiplication rather than
a measurement, in their own output.

### Topic 5 — RED and USE

| Program | Command (from `05-red-and-use/`) | Status | Time |
|---|---|---|---|
| `python/pool_saturation.py` | `python3 python/pool_saturation.py` | RAN | 7 s |
| `nodejs/pool_saturation.js` | `node nodejs/pool_saturation.js` | RAN | 6 s |
| `golang/pool_saturation.go` | `cd golang && go run pool_saturation.go` | RAN | 7 s |
| `java/PoolSaturation.java` | `cd java && javac PoolSaturation.java -d /tmp/javabuild && java -cp /tmp/javabuild PoolSaturation` | RAN | 9 s |

The README says six to nine seconds each. Measured: six to nine seconds each.

### Topic 6 — SLOs and error budgets

| Program | Command (from `06-slos-and-error-budgets/`) | Status | Time |
|---|---|---|---|
| `python/burn_rate.py` | `python3 python/burn_rate.py` | RAN | <1 s |

### Topic 7 — symptoms and postmortems

| Program | Command (from `07-symptoms-and-postmortems/`) | Status | Time |
|---|---|---|---|
| `python/page_worthiness.py` | `python3 python/page_worthiness.py` | RAN | <1 s |
| `python/detection_gap.py` | `python3 python/detection_gap.py` | RAN | <1 s |

Every human-derived timestamp in `detection_gap.py`'s output carries a
`[MODELLED]` tag, as claimed.

## BLOCKED: the shared lab  — *superseded, see the unblock pass below*

Nothing in `lab/` was brought up, because two prerequisites are absent from
this machine. This is the whole of Part 2 or Part 3 of every topic.

| Item | Reason | Exact unblock command |
|---|---|---|
| `lab/docker-compose.yml` (`api`, `worker`, `pricing`, `db`, `otelcol`, `lgtm`) | Docker CLI 28.1.1 is installed but the daemon is not running — `docker info` fails | `open -a Docker && until docker info >/dev/null 2>&1; do sleep 2; done && cd 06-observability/lab && docker compose up -d --build` |
| `lab/k6/{steady,arrival,ramp,many_customers}.js` | k6 is not installed on the host. They are mounted into the pinned `grafana/k6:1.4.0` container, so the Docker unblock above covers them; the host binary is only needed if you want to run them outside compose | `brew install k6` |

What *was* checked statically, with the daemon down:

- `docker compose config -q` — passes. The compose file is valid and every
  variable interpolation resolves.
- `otelcol/config.yaml` and `docker-compose.yml` parse as YAML.
- `python3 -m py_compile api/app.py api/worker.py` — passes.
- `go vet ./...` in `lab/pricing` — passes, so `main.go` compiles.
- `node --input-type=module --check` on all four k6 scripts — all parse.
- Every string `lab/README.md` promises exists in the code that has to honour
  it: the five `DEFECT_DISABLE` values, the three service-side `BREAK` values,
  `CARDINALITY_DEMO`, `OTEL_METRIC_CARDINALITY_LIMIT`, and the three `/_fault`
  modes.

**Every one of those static checks passed and every one of them missed a
defect that stopped the stack dead.** The compose file was valid YAML and
mounted Postgres 18's data directory at the pre-18 path; the seed was valid SQL
and overflowed int4 at row 271,410; `app.py` compiled and named a `fastapi`
version that was never published; the string `OTEL_METRIC_CARDINALITY_LIMIT`
was present everywhere it was promised and is read by nothing. That is the
honest value of a static pass, recorded here because the unblock pass below is
what it looks like when the same artifacts meet a runtime.

## Two things fixed during this pass

Both are in the lab, both concern Topic 3's fourth break, and neither is a
program defect — every program ran unmodified.

1. **`03-correlation-ids/README.md` gave a command that does nothing.** It
   listed `BREAK=collector_strip docker compose up -d otelcol` alongside the
   three service-side breaks. A collector config is static YAML with no
   conditionals, so an environment variable on that container cannot select a
   processor. The README now says so and points at the config edit instead.

2. **The commented-out break in `lab/otelcol/config.yaml` would not have
   broken anything.** It deleted a `traceparent` *span attribute*. Trace
   linkage lives in each span's `trace_id`/`parent_span_id`, not in an
   attribute, so the spans would have stitched together perfectly and the
   exercise would have produced no observable at all. Replaced with a `filter/drop_pricing`
   processor that drops `pricing`'s spans — a real collector-side
   failure, and one that produces a shape none of the three in-process breaks
   produce: a complete, internally consistent trace with the service that owns
   the latency missing from it, visible only in
   `otelcol_processor_dropped_spans` on `:8888`.

`lab/README.md`'s environment-variable table and the compose comment were
updated to match. `docker compose config -q` still passes after all three
edits.

## Coverage against what the READMEs promise

Each topic README's *How each language actually gets there* section names the
languages that topic uses and justifies the count. Checked file by file:

| Topic | README says | Files present | Match |
|---|---|---|---|
| 1 | six for Part 2, Python for Part 1 | 6 × `signal_cost` + `three_signals.py` | yes |
| 2 | six, plus a Python-only histogram program | 6 × `coordinated_omission` + `histogram_lies.py` | yes |
| 3 | all six | 6 × `lose_the_context` | yes |
| 4 | two (Python, Go), with the reason stated | 2 × `cardinality_overflow` | yes |
| 5 | four (Python, Node, Go, Java), with the reason stated | 4 × `pool_saturation` | yes |
| 6 | Python only, with the reason stated | `burn_rate.py` | yes |
| 7 | Python only, with the reason stated | `page_worthiness.py`, `detection_gap.py` | yes |

No topic folder is empty and no topic ships fewer languages than its README
claims.

## Portability

The failure that broke Layer 1 does not recur here. Searched every standalone
program for `epoll`, `/proc`, `getrlimit` and cgroup paths: **no hits in any
program**. The only occurrences in the layer are (a) a comment in
`02-real-p99/cpp/coordinated_omission.cpp` stating that it uses standard C++17
threads and no `epoll` or `/proc`, and (b) the cgroup reads in Topic 5's and
the lab's READMEs, both of which say explicitly that they must run *inside* a
container and that on this macOS host the same paths return "no such file".

The C++ files compile with Apple clang and `-pthread`. Python's spawn-vs-fork
difference on macOS never arises: nothing in the layer uses
`multiprocessing`.

## Housekeeping

`01-three-signals/rust/signal_cost/target/`,
`02-real-p99/rust/coordinated_omission/target/` and
`03-correlation-ids/rust/lose_the_context/target/` are checked in — about 30 MB
of cargo build output, 28 MB of it the tokio crate's. They are pure build
artifacts and rebuild offline in seconds. `find . -type d -name target -prune
-exec rm -rf {} +` from this directory removes them; a `.gitignore` with
`target/` prevents them coming back.

## One observation, not a defect

`05-red-and-use/nodejs/pool_saturation.js` prints an *event-loop lag (max)*
column that reads `1 ms` on every row of the ramp, from 2 requests in flight to
120. The measurement is genuine — a `setInterval` measuring its own lateness,
resolution 1 ms — and the flat result is itself the finding: holding 115 queued
requests as pending promises costs the loop essentially nothing. The prose
around it does not claim the column rises, so nothing is being oversold. It is
worth reading as a result rather than skipping as a dead column.

---

# Unblock pass — Docker daemon up

**Date:** 2026-08-19
**Docker:** server 29.5.3, `linux/aarch64`, 4 CPUs / 5.1 GB in the Desktop
linuxkit VM. Compose v5.1.4. cgroup v2 present inside containers.
**k6:** still not installed on the host. Every load run below went through the
pinned `grafana/k6:1.4.0` compose service, which is how the lab intends it.

Everything in the BLOCKED table above now runs. The stack was brought up,
built, driven under four different load shapes, and torn down with
`docker compose down -v`. This section records what the runs actually showed,
including the places where the run disagreed with the material.

**Measurement hygiene, stated because it affects the numbers.** The VM was not
empty: an unrelated project's containers (`interview_*`) and another layer's
lab (`craft-lab-*`) were resident throughout, together about 1.1 GB and under
5% of one core. Latency figures below are therefore honest for this machine and
not laboratory-clean. Nothing here ever ran two load generators at once.

## RAN, after being fixed — the shared lab

| Item | Command | Status |
|---|---|---|
| `lab/docker-compose.yml` — full stack | `cd lab && PRICING_HOST_PORT=8181 docker compose up -d --build` | **FIXED-THEN-RAN** — six services healthy |
| `lab/api` image | built from `api/Dockerfile` | **FIXED-THEN-RAN** — one pin corrected |
| `lab/pricing` image | built from `pricing/Dockerfile` | **FIXED-THEN-RAN** — span export added |
| `lab/db` seed | `db/init/01-schema.sql`, `02-seed.sql` | **FIXED-THEN-RAN** — 2,000,000 orders + 4,000,001 items in ~90 s |
| `lab/otelcol/config.yaml` | `validate --config=` then loaded | **FIXED-THEN-RAN** — internal telemetry reader added |
| `lab/k6/steady.js` | `docker compose run --rm k6 run /scripts/steady.js` | **RAN** — twice, 5 min each |
| `lab/k6/arrival.js` | `docker compose run --rm k6 run /scripts/arrival.js` | **RAN** — twice, 5 min each |
| `lab/k6/ramp.js` | `docker compose run --rm k6 run /scripts/ramp.js` | **RAN** — 10 min |
| `lab/k6/many_customers.js` | `docker compose run --rm k6 run /scripts/many_customers.js` | **RAN** — 10 min |

## Nine defects, found by running

Every one of these stopped something dead, and none of them was visible to the
static pass.

1. **`api/requirements.txt` pinned a FastAPI release that does not exist.**
   `fastapi==0.121.4` — the 0.121 line stops at 0.121.3. Image build failed at
   `pip install`. Corrected to `0.121.3`; every other pin in that file resolved
   exactly as written, including all six OpenTelemetry betas at `0.60b0`.

2. **`db` refused to start: Postgres 18 moved its data directory.** The compose
   file mounted `pgdata:/var/lib/postgresql/data`, which is the pre-18
   convention. postgres:18 keeps PGDATA in a major-version subdirectory and
   *exits 1* if it finds a mount at the old path. Mount is now the parent,
   `pgdata:/var/lib/postgresql`.

3. **The seed overflowed int4 and aborted, leaving an empty database.**
   `(100 + (n * 7919) % 90000)::int` — `generate_series` yields int4 here, and
   `n * 7919` exceeds 2³¹ at n = 271,410, long before the 2,000,000 the seed
   asks for. `db` exited 3 with `ERROR: integer out of range`. Fixed with
   `n::bigint`.

4. **The `db` healthcheck went green 90 seconds before Postgres accepted a TCP
   connection.** This is the best defect in the layer. While the scripts in
   `/docker-entrypoint-initdb.d` run, the entrypoint has a temporary server up
   with `listen_addresses=''` — reachable on the unix socket only. A bare
   `pg_isready` uses the socket, so it answered in 6 s; `depends_on:
   service_healthy` then started `api`, which died with
   `connection to server at "172.21.0.3", port 5432 failed: Connection refused`.
   The check now asks over `-h 127.0.0.1 -p 5432`, which is the question the
   dependents actually care about, and retries went 30 → 60 to cover the seed.

5. **`worker` crashed on its first job.** `json.loads(job["payload"])` against a
   `jsonb` column: psycopg3 adapts jsonb to a Python object on the way out, so
   the value is already a `dict` and `json.loads` raised
   `TypeError: the JSON object must be str, bytes or bytearray, not dict`.
   psycopg2 handed you a string, which is why the line looks right.

6. **`worker` exported no telemetry at all.** Its compose `command:` overrode
   the image CMD and dropped the `opentelemetry-instrument` prefix, so the SDK
   was never installed into the process: every tracer was the no-op proxy, the
   LoggingInstrumentor never patched `logging`, and every worker log line
   carried `trace_id: ""`. Tempo's service list contained `api` and nothing
   else. Topic 3's first break — *does the consumer join the producer's
   trace?* — had no observable in either direction. Fixed; `worker` now appears
   in Tempo and its log lines carry the producer's trace id.

7. **The collector's `:8888` was unreachable, from the host and from inside the
   compose network.** The collector's internal telemetry now takes an OTel-Go
   `readers:` configuration, and its built-in default binds the Prometheus
   reader to *localhost*:8888 — which inside a container means itself and
   nothing else. `curl localhost:8888/metrics` returned a connection error and
   so did `curl http://otelcol:8888/metrics` from a sibling container. Topic 4
   is built on reading that endpoint. Fixed with an explicit
   `readers: [pull: {exporter: {prometheus: {host: 0.0.0.0, port: 8888}}}]`;
   it now serves 55 KB of metrics.

8. **`pricing` never exported a span, so it could not appear in Tempo in any
   configuration.** The compose file sets `OTEL_EXPORTER_OTLP_ENDPOINT` and
   `OTEL_SERVICE_NAME` on `pricing`, and `main.go` reads neither — it hand-rolls
   W3C traceparent parsing and logs JSON, but has no exporter. Two consequences,
   both silent: Topic 3's third break claims "two complete traces, each looking
   healthy" and produced exactly one, and its fourth break — the `filter/drop_pricing`
   processor introduced by the previous verification pass, which drops spans
   whose `service.name` is `pricing` — had nothing to drop. Fixed by adding a
   ~90-line OTLP/HTTP **JSON** exporter to `main.go`, standard library only, in
   keeping with that file's stated design. `pricing` now appears in Tempo,
   parented correctly under `api`'s client span, and both breaks are real. (The
   trap worth knowing: OTLP/JSON encodes `traceId`/`spanId` as lowercase hex,
   not the base64 that proto3's default JSON mapping implies. Get it wrong and
   the collector returns 200 and drops the spans.)

9. **Host port 8081 was already bound** by an unrelated container, so `pricing`
   could not publish and the whole `up` failed. The host side is now
   `${PRICING_HOST_PORT:-8081}` — the default is unchanged, and nothing in the
   lab reaches `pricing` from the host anyway. Every run below used
   `PRICING_HOST_PORT=8181`.

A tenth change is an addition rather than a fix, and it is explained under
Topic 5 below: `api` now takes `cpus: "${API_CPUS:-0}"`.

## What the experiments actually showed

### Topic 2 — coordinated omission. Works, spectacularly.

Same service, same requests, two generator designs, five minutes each, all five
defects enabled:

| | `steady.js` (closed loop, 60 VU) | `arrival.js` (open loop, 300 RPS) |
|---|---|---|
| completed requests | 10,164 | 21,262 |
| achieved rate | 33.69 /s | 64.42 /s |
| p50 | **1.52 s** | **30.14 s** |
| p90 / p95 | 1.96 s / 2.49 s | 31.2 s / 31.7 s |
| p99 | 4.96 s | 32.57 s |
| max | 7.13 s | 37.27 s |
| non-200 | 0 of 10,164 | 12,619 of 21,262 (59.4%) |
| dropped iterations | — | 68,734 |
| thresholds | both passed | all three failed |

The closed-loop test reports a p50 twenty times lower than the open-loop one
against the identical service, and reports a 100% success rate while the same
service is failing 59% of requests. That is the claim Topic 2 makes, and it is
under-sold rather than over-sold.

The open-loop plateau at ~30 s is not a coincidence: it is `pool_timeout=30`
from the `small_pool` defect. Requests queue for a connection, wait exactly
thirty seconds, and 500.

**One calibration finding.** At 300 RPS against a service whose measured
capacity is ~65 RPS, *every* percentile collapses onto that 30 s timeout, so
`arrival.js` cannot separate the five defects. Disabling the N+1 changed
p50 by 0.01 s (30.14 → 30.15) and throughput by 1.6% (64.42 → 65.49 /s). The
same lever measured closed-loop is informative:

| `steady.js`, 60 VU, 5 min | all five defects | `DEFECT_DISABLE=n_plus_one` | delta |
|---|---|---|---|
| achieved rate | 33.69 /s | 37.35 /s | +10.9% |
| p50 | 1.52 s | 1.38 s | −9.2% |
| p95 | 2.49 s | 2.10 s | −15.7% |
| p99 | 4.96 s | 3.94 s | **−20.6%** |

So the N+1 owns twice as much of the p99 as of the p50 — exactly the "a defect
can own one and not the other" result `lab/README.md` promises, and it is only
visible in the closed-loop run. Isolating the remaining four defects is left as
the reader's exercise; the lever is verified working.

The `missing_index` defect is real and independently confirmed:
`EXPLAIN (ANALYZE, BUFFERS)` on the `/orders` query gives a **Parallel Seq
Scan** over 2M rows, 666,600 rows removed by filter per worker, 200.4 ms
execution, no index on the table but the primary key.

### Topic 3 — the four breaks. Two of the four do nothing as printed.

| Break | Command as the topic README printed it | Result |
|---|---|---|
| 1. `queue_no_traceparent` | `BREAK=... docker compose up -d worker` | **no-op.** `api` is the producer that writes the column; `worker` only reads it. Job written with the traceparent intact. Moving the variable to `api` works: column NULL, `had_traceparent: false`, consumer opens a new trace. |
| 2. `executor_no_ctx` | `BREAK=... docker compose up -d api` | **no-op.** The `sync_http_in_async` defect is enabled by default and short-circuits `fetch_price` into the blocking client, so `run_in_executor` — the boundary this break targets — is never reached. Verified by trace id: api `bdbe…c58d`, pricing `bdbe…c58d`, same trace. Paired with `DEFECT_DISABLE=sync_http_in_async` it works: api `34d6…fe5b`, pricing `f510…2a7a`, two traces. |
| 3. `pricing_fresh_ctx` | `BREAK=... docker compose up -d pricing` | **works.** api `01f6…8e12`, pricing `0000…0001`; `/stats` shows `orphaned_traces: 1, joined_traces: 0`. |
| 4. `filter/drop_pricing` | uncomment, add to both traces pipelines, recreate | **works, now that `pricing` emits spans.** Over a 120-second window afterwards: 9 `api` traces in Tempo, **0** `pricing` spans, no error anywhere. |

`pricing`'s `joined_traces` counter is *not* a valid check for break 2 — it
counts whether a parseable traceparent arrived, and a context-less executor
thread still injects one, just for a brand-new trace. It read 20/20 joined in
both directions. Comparing the two services' logged `trace_id`s is the check
that discriminates, and the topic README now says so.

The collector counter for break 4 is
`otelcol_processor_filter_spans_filtered_total{filter="filter/drop_pricing"}`
(observed: 204, i.e. two per span, because the processor is in both traces
pipelines). It is **not** `otelcol_processor_dropped_spans`, which this
collector does not emit — the config comment claiming otherwise has been
corrected.

### Topic 4 — cardinality. The loud half is superb. The silent half cannot happen here.

`CARDINALITY_DEMO=customer_id` plus `many_customers.js` for ten minutes,
sampled live:

| elapsed | series on `http_server_requests_total` | series carrying `otel_metric_overflow="true"` |
|---|---|---|
| baseline | **5** | 0 |
| 2½ min | 3,348 | 0 |
| 4½ min | 4,930 | 0 |
| 6½ min | 7,505 | 0 |
| 8½ min | 9,602 | 0 |
| 10½ min | 11,398 | 0 |
| settled | **12,216** | 0 |

One label, one word, 5 → 12,216 series: a 2,443× multiplication, watched
happening. And the cross-check the topic asks for — do the totals agree? —
comes out `sum(rate(...))` = 17.46/s against
`sum by (customer_id) (rate(...))` = 17.47/s across 8,349 series. The topic
README lists that agreement under *"what would mean the experiment is broken"*,
and it is right: it means the cap never applied.

**It never applied because there is no cap.** The OpenTelemetry Python SDK
1.39.0 implements no per-stream cardinality limit: no `otel.metric.overflow`
attribute anywhere in the installed package, and `OTEL_METRIC_CARDINALITY_LIMIT`
is not a variable it reads. Run 1 and run 2 of the topic's design are therefore
the same run. The *silent* failure — first-seen-wins, totals correct, every
breakdown quietly undercounting — is the more important of the two and it
cannot be produced on this stack. It is available from that topic's standalone
`python/cardinality_overflow.py`, which implements the spec's rule directly, or
from a Go or Java service.

`prometheus_tsdb_head_series`, which the topic tells you to watch, **does not
exist here**: the Prometheus inside `grafana/otel-lgtm` is OTLP-receive-only
with no `scrape_configs` at all, so it never scrapes itself. The query returns
an empty result and no error. `count({__name__="http_server_requests_total"})`
is the working substitute and is what produced the table above.

**Step 4, "fix and prove", works.** Uncommenting `transform/drop_customer_id`
and adding it to the metrics pipeline: in the 80 seconds after the collector
came back, **2** series were written and **0** of them carried `customer_id` —
down from 12,216. The capability survived; the label did not.

### Topic 5 — RED and USE. The finding lands; the ramp starts past the knee.

`ramp.js`, 10 → 120 VU over 10 minutes, sampled every two minutes:

| VUs | api p99 (`http.server.request.duration`) | pool checkout wait p99 | pool connections in use | api CPU | `nr_periods` / `nr_throttled` |
|---|---|---|---|---|---|
| 37 | 3.33 s | 4.95 s | **5 of 5** | 61.2% | 0 / 0 |
| 67 | 6.71 s | 7.98 s | **5 of 5** | 59.2% | 0 / 0 |
| 96 | 10.0 s | 23.58 s | **5 of 5** | 65.5% | 0 / 0 |
| 120 | 7.28 s | 9.42 s | **5 of 5** | 63.3% | 0 / 0 |

Run totals: 14,559 iterations, 24.14 /s, p50 1.85 s, p90 6.27 s, max 30.67 s.

Utilization pins at 100% at the very first sample and then carries no
information for the rest of the ramp, while the saturation measure — checkout
wait p99 — climbs from 4.95 s to 23.6 s. That is precisely Topic 5's thesis,
and CPU sitting flat around 60% of a single core while p99 triples is the
supporting half.

**But there is no baseline.** The pool was already fully committed at 37 VUs,
and almost certainly at the ramp's `startVUs: 10` — one `/orders` request holds
a connection for a 200 ms sequential scan plus 25 N+1 round trips, so five
connections cover fewer than ten concurrent requests. The topic's own
"experiment is broken" list anticipates this ("your pool of 5 was already
saturated at 10 VUs and you have no baseline"), and it is what happened. The
first two rows of that topic's prediction table have no honest answer as
shipped. The README now says so.

**The throttling row could not be filled at all, and that was a gap rather than
a result.** `cpu.max` read `max 100000` — no quota — and `cpu.stat` read
`nr_periods 0`, `nr_throttled 0`. Zero *periods* means the accounting never
ran, which is a different statement from "the quota was applied and never hit";
only the second is a result. No service in the compose file declared any CPU
limit and there was no documented lever to add one. `api` now takes `API_CPUS`,
verified working: `API_CPUS=0.5` gives `cpu.max` = `50000 100000` and
`nr_periods 63 / nr_throttled 29` from startup alone.

**Step 5, the sampler question, gives the interesting answer.** Same traffic,
same collector, spanmetrics sitting *before* `tail_sampling`:

| `api` sampler | SDK `http.server.request.duration` rate | spanmetrics `calls` rate | ratio |
|---|---|---|---|
| `parentbased_always_on` | 32.50 /s | 30.36 /s | 0.93 |
| `parentbased_traceidratio` @ 0.1 | 35.50 /s | 3.31 /s | **0.093** |

Putting spanmetrics ahead of the tail sampler buys nothing against a *head*
sampler, because head sampling happens in the SDK, upstream of the collector
entirely — the spans are never emitted, so no collector-side ordering can
count them. The SDK's own RED metric is unaffected, because metrics are not
sampled. Predicting "spanmetrics is unaffected, it is before the sampler" is
the obvious answer and it is wrong by a factor of ten.

### Topics 6 and 7 — fault injection works; the rules half does not exist yet.

The `/_fault` endpoint is real and accurate:

| Mode | Injected | Observed |
|---|---|---|
| `outage` | `{"mode":"outage","seconds":20}` | `/orders` returned 503, 503, 503 |
| `error_rate` | `{"ratio":0.08,"seconds":60}` | 9 non-200 in 120 requests = **0.075** |
| `pricing_tail` | `{"mode":"pricing_tail"}` | `pricing` logged `tail reconfigured … enabled:true every:100 ms:900` |

`pricing`'s tail ratio is exact: 300 requests, **3** tails, `observed_tail_pc: 1`.

**The command as printed returns 422 and injects nothing.** `curl -d` sends
`application/x-www-form-urlencoded`; the endpoint takes a JSON body, so the
verbatim command gives
`{"detail":[{"type":"dict_type","loc":["body"],"msg":"Input should be a valid dictionary"…`.
Both topic READMEs now carry `-H 'content-type: application/json'`.

`curl localhost:9090/api/v1/rules` returns `{"groups":[]}` and
`/api/v1/alerts` returns `{"alerts":[]}` — see *Still blocked* below.

## Still blocked, honestly

| Item | Reason | What it would need |
|---|---|---|
| Topic 4's **silent** cardinality overflow on the stack (`{otel_metric_overflow="true"}`, first-seen-wins, the SDK cap) | OpenTelemetry Python SDK 1.39.0 does not implement the spec's per-stream cardinality limit. No `otel.metric.overflow` datapoint exists anywhere in the installed package and `OTEL_METRIC_CARDINALITY_LIMIT` is read by nothing. This is an SDK gap, not a lab bug | An SDK that implements the cap — Go and Java do. Adding a second instrumented service in one of those languages and detonating the same label there would restore the exercise. The loud half, and the collector-side fix, both work today |
| Topics 6 and 7 **Part 2** — recording rules, burn-rate alerts, the naive alert, `activeAt` timestamps | The Prometheus inside `grafana/otel-lgtm` ships with no `rule_files:` and no `scrape_configs:`; it is OTLP-receive-only. Rules can be written but there is nowhere to put them, so `/api/v1/rules` stays empty no matter what the reader writes | Two bind mounts on the `lgtm` service — a rules file, and a copy of the image's own `/otel-lgtm/prometheus.yaml` with `rule_files:` added. `lab/docker-compose.yml` now carries both, commented, with the paths. The warning matters: replacing that config wholesale drops its `otlp:` block and silently changes every label you query by |
| `prometheus_tsdb_head_series` and every other `prometheus_*` self-metric | Same cause — this Prometheus does not scrape itself | The same config override, adding a self-scrape job. `count({__name__="…"})` is the substitute and is what this pass used |
| Topic 7 scenario W (CPU pegged, latency flat) | Needs a CPU-bound container alongside the ramp; the lab ships no such service | `API_CPUS` now provides the quota half of it. The load half would be a `stress-ng` sidecar, which is a new service, not a config change |
| `k6` as a host binary | Not installed, and deliberately not installed on this pass | `brew install k6`. Not needed: every script ran through the pinned `grafana/k6:1.4.0` compose service, which is what `lab/README.md` prescribes |

## Files changed on this pass

Code and config: `lab/docker-compose.yml`, `lab/api/requirements.txt`,
`lab/api/worker.py`, `lab/api/app.py` (docstring only), `lab/db/init/02-seed.sql`,
`lab/pricing/main.go`, `lab/otelcol/config.yaml`.

Documentation corrected to match what runs: `lab/README.md`,
`03-correlation-ids/README.md`, `04-cardinality/README.md`,
`05-red-and-use/README.md`, `06-slos-and-error-budgets/README.md`,
`07-symptoms-and-postmortems/README.md`.

The `VERIFICATION STATUS` headers that said "never been brought up" have been
replaced with what was observed, in `docker-compose.yml`, `otelcol/config.yaml`,
`api/app.py`, `api/worker.py` and `pricing/main.go`.

**The `Predict, then record` tables in all seven topic READMEs are still
blank.** They are the reader's exercise. Every measurement from this pass lives
on this page instead, which is where it belongs.

The stack was torn down with `docker compose down -v` at the end of this pass:
no `layer6-lab` containers, volumes or networks remain.
