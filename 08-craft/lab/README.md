# Layer 8 · The shared lab

One service, one compose stack, nine topics. Build it once. Everything here
is specified in one place so the topic READMEs can reference it instead of
restating it — and so the names stay stable. **Service names, environment
variables, ports, script names and file paths below are a contract the topic
code depends on. Change them here or not at all.**

The service is small on purpose — orders and customers, five endpoints — but
it is a *real* FastAPI + SQLAlchemy 2.0 async + Postgres service in Docker,
because half the findings in this layer only exist across a process boundary.
A swallowed exception is invisible in a unit test and obvious the moment a
real database goes away; a connection pool cannot exhaust in a script.

## Services

| Service | Image / build | What it is for |
|---|---|---|
| `api` | built from `lab/api/` — FastAPI on uvicorn | The system under test for every topic. Both the `shallow` and `deep` implementations of topic 1 are mounted in this one app, on different route prefixes |
| `postgres` | `postgres:18` | Real transactions, real ordering behaviour, a real pool ceiling. Matches Layer 3's pin |
| `toxiproxy` | `ghcr.io/shopify/toxiproxy` | Sits between `api` and `postgres` so topics 3 and 7 can make the database *slow* or *gone* without touching application code |
| `k6` | `grafana/k6` (v2 line) | Load generator. Open-model executors only — see below |
| `consumer-go` | built from `lab/consumer-go/` | A Go client generated from the committed OpenAPI snapshot. Topic 6 |
| `consumer-node` | built from `lab/consumer-node/` | A Node client of the same snapshot. Topic 6 |
| `tools` | small image with schemathesis, oasdiff, mutmut | Where CLI tooling runs, so nothing has to be installed on the host |

## Ports

Host ports are offset so this stack does not collide with Layer 5's harness
or with a locally installed Postgres. In-container ports are the ones every
command in the topic READMEs uses (`http://api:8000`, `postgres:5432`),
because everything runs on the compose network.

| Service | In-container | Published on host |
|---|---|---|
| `api` | 8000 | 8010 |
| `postgres` | 5432 | 55442 |
| `toxiproxy` (admin API) | 8474 | 8475 |
| `toxiproxy` (postgres listener) | 5433 | 55443 |
| `consumer-go` (ladder F) | 8080 | 8090 |
| `consumer-node` (ladder F) | 8081 | 8091 |

`postgres` runs with `POSTGRES_USER=app`, `POSTGRES_PASSWORD=app`,
`POSTGRES_DB=craft_lab` — which is why every `psql` line in this layer reads
`docker compose exec postgres psql -U app -d craft_lab`.

## Environment variables

Set on `api`. The pool and timeout names deliberately match Layer 5's harness
so a finding transfers between the two layers without a translation step.

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://app:app@toxiproxy:5433/craft_lab` | Points at toxiproxy, not at Postgres directly, so topics 3 and 7 need no restart to inject a fault |
| `POOL_SIZE` | `5` | SQLAlchemy `pool_size` |
| `MAX_OVERFLOW` | `10` | SQLAlchemy `max_overflow` |
| `POOL_TIMEOUT_S` | unset | SQLAlchemy `pool_timeout`; unset means wait forever, which is topic 7's baseline |
| `STATEMENT_TIMEOUT_MS` | unset | Emitted as `SET LOCAL statement_timeout` when set |
| `REQUEST_DEADLINE_MS` | unset | Topic 7's propagated per-request budget, applied with `asyncio.timeout()` |
| `RETRY_ATTEMPTS` | `0` | Topic 7 |
| `RETRY_BUDGET_PCT` | `0` | 0 disables the token bucket; topic 7's variant C uses 10 |
| `BREAKER_LATENCY_MS` | unset | Latency threshold for topic 7's circuit breaker |
| `ERROR_MODE` | `swallow` | `swallow`, `none`, or `correct` — topic 3's three variants of the repository `except` block |
| `PAGINATION_STRATEGY` | `narrow` | `narrow` or `wide`; selects which Hypothesis strategy topic 5's property test uses |
| `SEED_ORDERS` | `50000` | Rows created by `make seed` |

## Paths

```
08-craft/
  lab/
    compose.yml               # api, postgres:18, toxiproxy, k6, consumer-go, consumer-node, tools
    api/
      app/
        shallow/              # Topic 1: router -> service -> repository -> dao, four files
        deep/                 # Topic 1: the same feature, one substantial module
        core/pagination.py    # Topic 5's flagship bug lives here
        core/money.py         # Topic 5's warm-up bug
        core/errors.py        # Topic 3: the error taxonomy
        db.py  main.py
      tests/
        unit/ integration/ properties/ contract/ regression/
      openapi.snapshot.json   # Topic 6: the committed contract
      pyproject.toml
      Makefile                # seed, regression, coverage targets
    consumer-go/              # Topic 6: a Go client of the Python API
    consumer-node/            # Topic 6: a Node client of the same
    load/                     # k6 scripts, one per topic, mounted at /load
    tools/
      temporal_coupling.py    # Topic 2: git-history coupling analysis
      name_audit.py           # Topic 9: verb/noun census and the blind-name quiz
```

k6 scripts, by topic:

```
/load/t3_errors.js
/load/t7_latency_ladder.js    -e STEP=<ms>
/load/t7_clients.js           -e CLIENT=python|go|node
```

## Toxiproxy, invoked one way

The CLI binary lives at `/toxiproxy-cli` inside the image, so every command
in this layer reads:

```
docker compose exec toxiproxy /toxiproxy-cli <subcommand>
```

The proxy `pg` is created once, listening on `0.0.0.0:5433` inside the
toxiproxy container and forwarding to `postgres:5432`:

```
docker compose exec toxiproxy /toxiproxy-cli create -l 0.0.0.0:5433 -u postgres:5432 pg
```

Toxic names matter when you delete them: `toxic add pg -t latency` creates a
toxic named `latency_downstream` unless you pass `-n`. This layer always
passes `-n` explicitly (`-n lat`, `-n cut`) so the delete command is not
guessing.

## Seed data

Reuse Layer 3's seed if you already built it. If not, `make seed` creates
`SEED_ORDERS` orders across ~2,000 customers. Two properties of the seed are
load-bearing and easy to get wrong:

- **Insert order must not match any sort order you later assert.** Topic 4's
  whole point is that a missing `ORDER BY` is invisible when the rows happen
  to come back in insertion order. Shuffle before inserting, and `UPDATE` a
  few hundred rows afterwards so their heap positions move.
- **`created_at` must contain deliberate ties.** Topic 5's flagship bug only
  exists when two rows share a timestamp. Real systems produce ties from
  bulk imports and from any column with second-level resolution; the seed
  reproduces that on purpose.

## Load generation rules

**Open model, always.** k6's `constant-arrival-rate` and
`ramping-arrival-rate` executors only — never `constant-vus` or
`ramping-vus`. A closed-loop generator stops offering load when the server
slows down, which erases exactly the effect topic 7 exists to demonstrate.
This trap is flagged in four layers of this lab because it is the single most
likely way to get a null result and draw the wrong conclusion from it.

If k6 warns that it cannot allocate enough VUs to sustain the target rate,
the generator itself has fallen behind and is now coordinating omission —
raise `preAllocatedVUs` and rerun before believing the numbers.

## Tool versions, pinned

Pinned so that mutation scores and property-test runs are comparable across
sessions, not because newer versions are broken.

| Tool | Pin | Note |
|---|---|---|
| Python | 3.13 | 3.14 works with everything below; pin one so mutation runs compare |
| pytest | 9.1.x | |
| hypothesis | 6.165.x | requires Python ≥3.10; the `backend=` setting is experimental |
| schemathesis | 4.24.x | v4 line; CLI is `schemathesis run` (alias `st run`) |
| coverage.py | 7.15.x | see topic 8 on `sysmon` |
| mutmut | 3.7.x | needs `fork()`: macOS fine, Windows needs WSL |
| testcontainers | 4.15.x | |
| postgres | 18 | matches Layer 3's pin |
| k6 | v2 | `--no-summary` is now `--summary-mode=disabled`; the arrival-rate executors are unchanged |
| Go | 1.26.x | native `go test -fuzz` since 1.18 |
| Node | 24 LTS | |

## Running this on a Mac

The machine this lab targets is **macOS 27 on arm64**, and everything above
runs Linux inside Docker Desktop's VM. That is deliberate: Layer 1 shipped
Linux-only code that failed silently on macOS, and containers make that
mistake impossible here.

Two consequences worth knowing before you record anything:

- Put `PGDATA` on a **named volume**, never a bind mount into `/Users/...`.
  A bind mount routes every write through Docker Desktop's file sharing, and
  topic 7's latency ladder will then be measuring that instead of your
  timeout budget.
- The VM has a fixed CPU and memory allocation, so absolute throughput is not
  comparable to production or to anyone else's machine. **Shapes transfer;
  absolute numbers do not.** Every prediction in this layer is phrased as a
  shape, a ratio, or an ordering for exactly that reason.

Topics 1, 2, 5 and 9 need no container at all — they are pure-function and
git-history work that runs natively on the host.
