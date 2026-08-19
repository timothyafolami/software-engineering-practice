# Layer 5 · The shared harness

Every topic in this layer that needs more than one process runs against this
one stack. It is specified here once so the topic READMEs can reference it
instead of each restating it, and so the names below stay stable — **service
names, environment variables, ports and script paths are a contract the
topic code depends on. Change them here or not at all.**

A single-process script cannot show any of this. Queueing, retry
amplification, metastability and fan-out tails are all properties of a system
with real network boundaries, a real connection pool and a real load
generator that does not wait for you.

## Services

| Service | Image / build | What it is for |
|---|---|---|
| `app` | built from `lab/app/` — FastAPI on uvicorn | The system under test. Service time and failure behaviour are controllable at runtime through an admin endpoint, so a single build serves every topic |
| `gateway` | same image as `app`, role from env | Hop 1 of the three-hop chain (topics 2 and 3) |
| `service-b` | same image as `app`, role from env | Hop 2 of the chain |
| `service-c` | same image as `app`, role from env | Hop 3 — the one holding a real Postgres connection |
| `postgres` | `postgres:18` | A real pool, a real `max_connections`, a real `statement_timeout`. Not a simulation of a database |
| `redis` | `redis:7` | A cache, so topic 4 has something to cold-start |
| `toxiproxy` | `ghcr.io/shopify/toxiproxy` | Sits between `app` and its dependencies so you inject latency, jitter and resets **without touching application code**. "Make the dependency slow, not just absent" is the whole point of this layer, and this is how |
| `k6` | `grafana/k6` | The load generator. Always open-model executors |
| `prometheus` | `prom/prometheus` | Scrapes `app`'s `/metrics`; histograms, never pre-computed percentiles |
| `grafana` | `grafana/grafana` | The knee is a *shape*, and tables of numbers hide shapes |

Prometheus and Grafana are optional: k6 CSV output plus the plotting scripts
in `tools/` produce the same charts with less running.

## Ports

Host ports are offset so the stack never collides with a locally installed
Postgres or Redis — several topics also ship standalone programs that talk to
your *local* Postgres, and both must be able to run.

| Service | In-container | Published on host |
|---|---|---|
| `app` | 8000 | 8000 |
| `gateway` | 8000 | 8001 |
| `service-b` | 8000 | 8002 |
| `service-c` | 8000 | 8003 |
| `postgres` | 5432 | 55432 |
| `redis` | 6379 | 56379 |
| `toxiproxy` (admin API) | 8474 | 8474 |
| `toxiproxy` (postgres listener) | 5433 | 55433 |
| `toxiproxy` (redis listener) | 6380 | 56380 |
| `prometheus` | 9090 | 9090 |
| `grafana` | 3000 | 3000 |

## Environment variables

Set on `app`, `gateway`, `service-b` and `service-c`. Every one of them is
readable *and writable at runtime* through the admin endpoint, so a sweep
never needs a rebuild.

| Variable | Default | Meaning |
|---|---|---|
| `ROLE` | `app` | `app`, `gateway`, `service_b` or `service_c` |
| `SERVICE_MS` | `40` | Duration of the simulated work in the handler, held inside a real pooled connection |
| `POOL_SIZE` | `5` | SQLAlchemy `pool_size` |
| `MAX_OVERFLOW` | `10` | SQLAlchemy `max_overflow` |
| `POOL_TIMEOUT_S` | `30` | SQLAlchemy `pool_timeout` |
| `STATEMENT_TIMEOUT_MS` | unset | Emitted as `SET LOCAL statement_timeout` when set |
| `CLIENT_TIMEOUT_MS` | `500` | Outbound HTTP timeout for the next hop |
| `DEADLINE_HEADER` | `X-Request-Deadline` | Absolute unix millis; topic 2 |
| `DEADLINE_SLACK_MS` | `20` | Subtracted per hop; also the reject-immediately floor |
| `PROPAGATE_DEADLINE` | `0` | Topic 2's independent variable |
| `RETRY_ATTEMPTS` | `3` | Topic 3 |
| `RETRY_BASE_MS` | `50` | Topic 3 |
| `RETRY_JITTER` | `none` | `none` or `full` |
| `RETRY_BUDGET_PCT` | `0` | 0 disables the token bucket; topic 3's variant C uses 10 |
| `SHED_MODE` | `none` | `none`, `static`, `priority`, `adaptive`; topic 5 |
| `SHED_LIMIT` | unset | In-flight limit for `static`; derive it from topic 1's measured knee |
| `SHED_WAIT_MS` | `50` | Queue-wait deadline before a 503 |
| `CACHE_TTL_S` | `300` | Topic 4 |
| `IDEMPOTENCY_MODE` | `correct` | `naive` or `correct`; topic 7 |
| `UVICORN_BACKLOG` | `2048` | Uvicorn's own default, named here because topic 5 changes it |
| `UVICORN_LIMIT_CONCURRENCY` | unset | Uvicorn's crude static shedder |

`postgres` runs with `POSTGRES_USER=app`, `POSTGRES_PASSWORD=app`,
`POSTGRES_DB=failure_lab` — which is why every `psql` line in this layer reads
`docker compose exec postgres psql -U app`.

## Admin endpoint

`POST /admin/config` on any of the four application services takes a JSON
object of the variables above and applies it live; `GET /admin/config`
returns the current values. This is what makes "change **only** `pool_size`
and rerun" an honest instruction rather than a rebuild.

`POST /admin/fault` controls locally injected failure (error rate, added
latency) for cases where toxiproxy is the wrong layer — toxiproxy breaks the
*network* to a dependency, this breaks the *service*.

## Compose profiles

Nothing starts that a topic does not need.

| Profile | Brings up | Used by |
|---|---|---|
| *(default)* | `app`, `postgres`, `k6` | Topic 1 |
| `chain` | `gateway`, `service-b`, `service-c`, `postgres`, `toxiproxy`, `k6` | Topics 2 and 3 |
| `metastable` | `app`, `postgres`, `redis`, `toxiproxy`, `k6` | Topic 4 |
| `shed` | `app`, `postgres`, `k6` | Topic 5 |
| `fanout` | `gateway`, K× `backend`, `k6` | Topic 6 |
| `payments` | `app`, `postgres`, `toxiproxy`, `k6` | Topic 7 |

## Paths

| Path | Contents |
|---|---|
| `lab/app/` | The FastAPI service, one image, role selected by `ROLE` |
| `lab/scripts/` | k6 scripts, mounted into the `k6` container at `/scripts` |
| `lab/out/` | k6 CSV output, mounted at `/out`; also where the plotting scripts read from |
| `lab/tools/` | `plot_knee.py`, `plot_amplification.py`, `plot_goodput.py`, `plot_shed.py`, `plot_tail.py`, `zombie_report.py` |

k6 scripts, by topic:

```
/scripts/01_ramp.js
/scripts/02_chain_naive.js
/scripts/02_chain_deadline.js
/scripts/03_retry_storm.js      -e VARIANT=naive|jitter|budget|edge_only
/scripts/04_metastable.js
/scripts/05_shed.js             -e MODE=none|static|priority|adaptive
/scripts/06_fanout.js           -e K=<n> -e HEDGE=on|off
/scripts/06_closed_loop.js      -e K=<n>
/scripts/07_idempotency.js      -e MODE=naive|correct|chaos
```

## Load generation rules

**Open model, always.** `constant-arrival-rate` and `ramping-arrival-rate`
only — never `ramping-vus`. A closed-loop generator stops sending load when
the server slows down, which erases every effect this layer exists to
demonstrate. Topic 6 explains the mechanism and includes a deliberate
side-by-side to prove it, and that comparison is the *only* place in this
layer where `ramping-vus` is permitted.

Watch the secondary tell too: if k6 warns that it cannot allocate enough VUs
to sustain the target rate, the generator has fallen behind and is now
coordinating omission itself, arrival-rate executor or not. Raise
`preAllocatedVUs`.

## Running this on a Mac

Everything above runs Linux inside Docker Desktop's VM, which is deliberate:
Layer 1 shipped Linux-only code that failed silently on macOS, and containers
make that impossible here. But that VM has a fixed CPU and memory allocation,
so absolute throughput is not comparable to production or to anyone else's
machine.

**Shapes transfer; absolute numbers do not.** Every prediction in this layer
is phrased as a shape or a ratio for exactly that reason. Where a topic also
ships standalone per-language programs, those run natively on macOS 27 /
arm64 and need no container at all.

## Version notes that matter for this harness

- **Postgres 18** (Sept 2025) shipped asynchronous I/O (`io_method`, including
  `io_uring` on Linux), and its interactions with pooling configuration are
  still being worked out in public. Do not assume PgBouncer sizing carried
  over from 16 or 17 is still optimal.
- **k6 is on v2:** `externally-controlled` was removed, `--no-summary` became
  `--summary-mode=disabled`, and the cloud subcommands changed shape. The
  arrival-rate executors this layer depends on are unchanged.
- **Python 3.14** (Oct 2025) made the free-threaded build supported rather
  than experimental. It changes little here — every problem in this layer is
  a queueing problem, and free-threading helps CPU-bound endpoints rather
  than IO-bound ones — but "Python can't use multiple cores" is now a
  statement about a build flag rather than about the language. If you want
  the current single-threaded penalty figure, read it from the CPython
  release notes rather than from any number quoted in a lab.
