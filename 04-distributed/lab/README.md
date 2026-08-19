# Layer 4 · the lab harness

Every topic in this layer shares one `docker compose` stack. This file is the
spec for it: service names, ports, environment variables and file paths. Topic
READMEs reference these names rather than restating them, so **if you rename
something here you break the run blocks in all seven topics.**

The bar for this layer is that experiments run **in containers, under load**.
A single-process script pretending to be a distributed system cannot show you a
partial failure, because there is no partition to have.

## The stack

| Service | Image / tool | Port(s) | Used by |
|---|---|---|---|
| `payments-api` | FastAPI + psycopg3 / SQLAlchemy 2.x | 8000 | 1, 2, 6 |
| `ledger` | second FastAPI service | 8001 | 1 |
| `api` | FastAPI, read/write split | 8000 | 4 |
| `writer-a` / `writer-b` | FastAPI, two contending writers | 8010 / 8011 | 3 |
| `relay-a` / `relay-b` | outbox relay, elected singleton | — | 6, 7 |
| `postgres` | `postgres:18` | 5432 | 1, 2, 6, 7 |
| `pg-primary` | `postgres:18` | 5432 | 4 |
| `pg-standby` | `postgres:18`, streaming standby | 5433 | 4 |
| `toxiproxy` | `ghcr.io/shopify/toxiproxy` | 8474 (API), 8666 (proxy) | 1, 2, 4, 5 |
| `redpanda` | Kafka API in one container | 9092 | 6 |
| `etcd1` `etcd2` `etcd3` | 3-node etcd | 2379 (client), 2380 (peer) | 5, 7 |
| `k6` | Grafana k6 v1.x | — | 1, 2, 3, 4, 6 |
| `pumba` | container fault injector | — | 5, 7 |

Redis Streams is an acceptable substitute for Redpanda — one container either
way, and nothing in Topic 6 depends on Kafka semantics beyond "a broker that can
be stopped." Toxiproxy is preferred over Pumba's `netem` for latency and
partitions (see macOS notes below); Pumba is kept for `kill`, `pause`, `stop`.

k6 rather than vegeta because Topic 2 needs a script that fires the *same*
idempotency key simultaneously from several VUs, which vegeta's request format
cannot express.

## Environment variables

Read by the services above. Each is the independent variable of one experiment,
so they are the knobs the record tables have columns for.

| Variable | Service | Values | Topic |
|---|---|---|---|
| `CRASH_AFTER_COMMIT` | `ledger` | `0` / `1` | 1 |
| `IMPL` | `payments-api` | `A` / `B` / `C` | 2 |
| `CLOCK_OFFSET_MS` | `writer-b` | integer ms | 3 |
| `APPLY_DELAY` | `pg-standby` | e.g. `500ms`, `5s` | 4 |
| `FIX` | `api` | `none` / `sticky` / `lsn` | 4 |
| `MODE` | `payments-api` | `v0` / `v1` / `v1-bug` / `v2` | 6 |
| `FENCING` | `relay-a`, `relay-b` | `0` / `1` | 7 |
| `LAB_DSN` | all Python | libpq DSN | local fallback |
| `LAB_ADMIN_DSN` | all Python | libpq DSN | local fallback |

`APPLY_DELAY` maps to Postgres's `recovery_min_apply_delay` on the standby. It
is set as an env var rather than baked into a config file so that a topic-4 run
is one `docker compose up` away from a different lag.

## File paths

```
04-distributed/
  docker/                     compose files + service images for the whole layer
  lab/README.md               this file
  lab/local/lab_db.py         shared Postgres helper for the no-Docker fallback
  lab/local/check_env.py      what is and is not runnable on this machine
  lab/local/teardown_lab.py   drops the scratch database
  NN-topic/docker/            topic-specific compose overrides
  NN-topic/sql/topicN_*.sql   the assertions; these are the deliverables
  NN-topic/<language>/        the programs
```

k6 scripts mount into the k6 container at `/scripts/`, so `topic1.js` on disk is
`/scripts/topic1.js` in a run block. Keep that mapping.

## macOS 27, arm64 (M1) — read before debugging anything

Everything runs inside Docker Desktop's Linux VM, so Linux mechanisms work
*inside containers*. Three consequences that have already cost time once:

1. **All containers share one clock.** Linux time namespaces (5.6+) virtualize
   `CLOCK_MONOTONIC` and `CLOCK_BOOTTIME` only — `CLOCK_REALTIME` is
   deliberately not namespaced — and Docker Desktop is a single VM regardless.
   There is no Docker flag that skews one container's wall clock. Topic 3 uses
   an application-level offset instead, which is why `CLOCK_OFFSET_MS` exists.
2. **Pumba's `netem` needs `NET_ADMIN` and a `tc`-capable image**
   (`--tc-image gaiadocker/iproute2`). Prefer Toxiproxy for latency and
   partition; keep Pumba for `kill` / `pause` / `stop`, which need neither.
3. **Absolute latency numbers from this stack are not portable.** The VM's
   network adds a floor you did not choose. Every record table in this layer
   asks for *ratios and deltas between variants* for exactly this reason — those
   survive the floor, absolute milliseconds do not.

Anything that genuinely needs a Linux-only interface (`/proc`, cgroup files,
`epoll`) runs **inside a container**, never on the host. If a run block does not
start with `docker compose` or `docker compose exec`, it is host-safe.

## The no-Docker fallback

The Docker daemon is not running on this machine and k6 is not installed, which
blocks every compose-driven experiment here. Rather than leave the layer
unrunnable, the Postgres-backed topics (2, 4, 6, 7) have a local mode that talks
to whatever Postgres is already listening and creates **one** scratch database
for the whole layer:

```
database   sep_lab_04_dist          (override with LAB_DSN)
helper     lab/local/lab_db.py      ensure_database / open_lab / percentile
check      python3 lab/local/check_env.py
teardown   python3 lab/local/teardown_lab.py
```

`lab_db.py` never edits `postgresql.conf` and never touches a database it did
not create. Everything it changes is inside `sep_lab_04_dist` or session-scoped.

What the fallback **cannot** reproduce, and where the topic README says so
explicitly: a real streaming standby (Topic 4), a real partition (Topics 5, 7),
a broker that can be stopped independently of the writer (Topic 6). Those parts
stay blocked until the daemon is up; they are not silently downgraded into
single-process imitations.

Topic 1's six programs and Topic 5's Raft implementation need none of this —
they run with no Docker, no Postgres and no network.

## Version pins, set once here

Pinned in this file and referenced from the topics, so they do not drift. The
right-hand column is what is actually installed on this machine, checked rather
than assumed — where the two disagree, the topic that cares says so.

| Thing | Pin for containers | Installed here (checked 2026-08-18) |
|---|---|---|
| Postgres | `postgres:18` (18.6 current stable) | **17.5** (Homebrew), server and client |
| Go | 1.26 released; 1.25 and 1.26 supported | **go1.24.5** darwin/arm64 |
| Python | 3.13 | 3.13.5 |
| Node | any version with `fetch` and `AbortSignal.timeout` | v24.14.0 |
| k6 | v1.x | **not installed** |
| Docker | Docker Desktop | **daemon not running** |

Two consequences that will bite in the local fallback, and only there:

- **`uuidv7()` is a Postgres 18 function and the local server is 17.5.** Time-
  ordered UUIDs are the difference between good and terrible B-tree insert
  locality on the idempotency-key and outbox tables this layer builds, so the
  container runs get them and the fallback does not. Use `gen_random_uuid()`
  locally and do not read anything into the insert-throughput numbers.
- **`testing/synctest` graduated in Go 1.25 and the local toolchain is 1.24.5**,
  so it is unavailable until you upgrade. Topic 5 does not need it — the MIT
  harness does not use it — but Topic 7's lease timers would benefit.
