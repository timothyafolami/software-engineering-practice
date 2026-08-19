# Layer 3 · The shared lab

One database, one schema, one seed, serving all eight topics. Build it once.
Every topic README assumes this page and does not restate it.

There are two paths to a working lab. **The local path is the one that runs on
this machine today**; the Docker path is the spec later code targets and the one
you need for the two topics that require more than one Postgres.

---

## Path A — local Postgres, no Docker (the default)

`lab/local/lab_db.py` is the shared helper. It talks to whatever Postgres is
already listening locally and creates **one** scratch database for the entire
layer, so nothing here can touch a database you use for something else.

| Name | Value | Override with |
|---|---|---|
| database | `sep_lab_03_data` | `LAB_DSN` |
| admin connection (to `CREATE DATABASE`) | `postgresql:///postgres` | `LAB_ADMIN_DSN` |
| seed scale | `small` | `LAB_SCALE=small\|full` |
| CSV output directory | the system temp dir | `LAB_OUT` |
| Go programs' connection string | same database | `LAB_PG_URL` |

```
python3 lab/local/check_env.py      # what runs here, what is blocked, and the unblock command
python3 lab/local/setup_lab.py      # front-load the seed (optional)
python3 lab/local/teardown_lab.py   # drop the scratch database
```

Every topic program provisions what it needs on its own, so `setup_lab.py` is a
convenience, not a prerequisite — the first program you run pays the seed cost
and the rest find it already there.

**Run `check_env.py` first anyway.** This layer is written against Postgres 18
and one PG19 feature; the script reports, per capability, whether your server can
run it and prints the exact command that would unblock it. An experiment that
silently degrades is worse than one that refuses to start.

### The schema

Two families of table. The small ones exist to create contention; the big ones
exist so the planner has decisions worth making.

| Table | Purpose | Topics |
|---|---|---|
| `accounts` | `id`, `balance_cents`. Contention table. | 1, 5 |
| `oncall` | `shift_id`, `doctor_id`, `on_call`. The write-skew table: 100 shifts × 2 doctors, invariant "at least one on call per shift". | 1 |
| `jobs` | `id`, `payload`, `state`, `claimed_by`, `claimed_at`. The `SKIP LOCKED` work queue. | 5 |
| `customers` | `id`, `email` unique, `country` (20 deliberately skewed values, `NG` dominant) | 3, 4, 6 |
| `orders` | `customer_id`, `status` (4 values, ~92% `complete`), `total_cents`, `created_at` | 3, 4, 6, 8 |
| `line_items` | `order_id`, `sku`, `qty`, `price_cents` | 4, 6 |
| `mvcc_orders` | Topic 2's own churn table, so wrecking it does not disturb the tables Topics 3-4 read. `status` is indexed on purpose — an `UPDATE` to an indexed column can never be a HOT update. | 2 |

Seed sizes, as `lab_db.SCALES` defines them:

| Scale | customers | orders | line items per order |
|---|---|---|---|
| `small` (default) | 50,000 | 1,000,000 | 3 |
| `full` | 200,000 | 5,000,000 | 4 |

Generated with `generate_series` in SQL, never through an ORM. Two properties are
deliberate and not cosmetic: the data is **skewed** (uniform data hides every
interesting planner decision) and it is **large enough that plans change** — under
a few thousand rows the planner seq-scans everything, correctly, and teaches you
nothing.

**`ANALYZE` after seeding, every time.** A large share of "the planner ignored my
index" reports are stale statistics.

### Session config, not server config

The local path sets planner-relevant settings **per session** (`lab_db.tune_session`)
rather than editing a `postgresql.conf` you may be sharing:

```
SET random_page_cost = 1.1        -- SSD. The 4.0 default assumes spinning rust
SET effective_cache_size = '1GB'  -- and distorts every plan in this layer
SET track_io_timing = on          -- superuser-only on some builds; skipped if refused
```

If a topic's numbers look wrong before anything else looks wrong, check that
`random_page_cost` actually applied — `SHOW random_page_cost` inside the session
that ran the query, not a different one.

---

## Path B — the Docker stack (Topics 7 and 8, and anything under load)

Two topics genuinely need more than one process talking to more than one
Postgres: **Topic 7** (a pooler in front of the database, and a CPU quota around
the client) and **Topic 8** (a streaming replica). Those need this stack.

```
03-data/lab/docker/
  compose.yml                  # postgres-primary, postgres-replica, pgbouncer, api, k6
  postgres/primary.conf
  postgres/replica.conf
  postgres/init/               # schema DDL, runs once on first boot
  api/                         # FastAPI + SQLAlchemy 2.0 async + psycopg (the production stack)
  load/                        # one k6 script per topic that needs load
```

Service names, and the ports they publish on the host. **Later code depends on
these exact names** — a topic README that says "point it at `pgbouncer`" means the
service below.

| Service | Image / build | Host port | Role |
|---|---|---|---|
| `postgres-primary` | `postgres:18` | 55432 | the database everything writes to |
| `postgres-replica` | `postgres:18` | 55433 | streaming standby, Topic 8 only |
| `pgbouncer` | `edoburu/pgbouncer` | 6432 | transaction-mode pooler, Topic 7 only |
| `api` | build `./api` | 8000 | the one HTTP service in this layer |
| `k6` | `grafana/k6` | — | load generator, on the same compose network |

Environment the `api` service reads:

| Variable | Meaning |
|---|---|
| `LAB_DSN` | connection string — point it at `postgres-primary` or at `pgbouncer` to switch Topic 7's experiment |
| `LAB_REPLICA_DSN` | replica connection string, Topic 8 |
| `POOL_SIZE`, `MAX_OVERFLOW`, `POOL_TIMEOUT` | SQLAlchemy pool knobs, swept in Topic 7 |
| `ISOLATION` | `read committed` / `repeatable read` / `serializable` |
| `WORKERS` | worker process count, Topic 7's container-quota experiment |

**Pin Postgres 18.** It is the current stable line as of August 2026 (see the
[release notes](https://www.postgresql.org/docs/18/release-18.html)); 19 is in
beta. Several things in this layer are 18-specific (`BUFFERS` on by default,
B-tree skip scan, eager freezing) and exactly one is 19-specific (`WAIT FOR LSN`,
Topic 8), which that topic builds a separate beta container for.

### Three services, not five — and why

An earlier draft of this layer specified `api-go/` and `api-node/` alongside the
Python `api/`: the same three endpoints, three times. That was cut, and the
reasoning is worth keeping because it recurs.

**Every finding in this layer is Postgres-side.** Write skew, vacuum starvation,
plan flips, lock queues, replication lag — none of them care which client
language issued the statement, and demonstrating them three times produces three
identical tables. Where the *client's* behaviour genuinely is the finding —
retry ergonomics (Topic 1), prepared-statement plan caching (Topic 4), pool
semantics and their defaults (Topic 7) — the second and third language stays, but
as a **20-to-60-line script under that topic's folder**, not as a service:
`01-isolation-levels/golang/write_skew/`, `07-connection-pools/nodejs/`. A
script proves the same claim, and you can read all of it.

### macOS notes, because Layer 1 got this wrong

This machine is macOS 27 on arm64. Everything in the Docker path runs in Linux
containers, so nothing is hostile *in the guest* — but Docker Desktop runs a Linux
VM and two things follow:

- Put `PGDATA` on a **named volume**, never a bind mount into `/Users/...`.
  Bind-mounted Postgres data on macOS is slow enough to swamp every measurement
  in this layer.
- Leave the Postgres 18 default `io_method = worker`. `io_uring` is not usable
  inside Docker Desktop's VM, so any number you produce chasing it is a number
  about the VM.

Anything reading `/sys/fs/cgroup/` — Topic 7's container-quota experiment — is
**Linux-only** and must run *inside* a container, not in a terminal on the Mac.
Those paths do not exist on macOS; a command that appears to do nothing there is
not a result.

---

## Next

[Topic 1 — Isolation levels](../01-isolation-levels/README.md), or the layer
[index](../README.md) for the order to work them in.
