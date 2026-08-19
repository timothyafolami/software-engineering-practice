# Topic 7 · The shared harness

Every experiment in [Topic 7](../README.md) drives this one stack. Build
it once; the sub-topics change knobs on it rather than each standing up
their own service. Nothing in here is a lesson on its own — it is the
load-bearing shape the seven sub-topics measure against.

**This runs inside Linux containers, always.** macOS has no `/sys/fs/cgroup`,
so `cpu.weight`, `cpu.max`, `cpuset.cpus`, `memory.max` and `cpu.stat` do
not exist on the host to be read or written. The Darwin fallbacks in
`local/` model the same accounting rule in userspace and say so loudly; they
are a way to keep working when the daemon is down, not a substitute.

---

## What is in here

```
00-harness/
  docker-compose.yml    # api, db, hog, k6 -- all resource-limited
  app/
    Dockerfile          # ARG PYTHON_IMAGE, so 7.7 can swap interpreters
    main.py             # the FastAPI service: /cpu /db /mixed /stat /healthz
    init.sql            # one indexed table, 1000 rows
    requirements.txt    # fastapi, uvicorn[standard], asyncpg, psycopg2-binary
  load/
    steady.js           # k6 constant-arrival-rate
    spike.js            # k6 ramping-arrival-rate
  local/
    cgroup.py           # read the enforced numbers, or return None. Never guess
    cfs_sim.py          # userspace model of CFS bandwidth control (FALLBACK)
    openloop.py         # stdlib open-loop load driver (FALLBACK)
  observe/
    watch.sh            # samples cpu.stat once a second, prints the ratio
```

## Services, exactly as named

| Service | Image / build | Profile | Its own limit | Why it exists |
|---|---|---|---|---|
| `api` | built from `app/Dockerfile` | default | `cpus: "${API_CPUS:-1.0}"`, `mem_limit: ${API_MEM:-1g}` | The thing under test. Port `8000` published |
| `db` | `postgres:17` | default | `cpus: "2.0"` | Given its own generous quota so it is never accidentally the bottleneck |
| `hog` | `alpine:3` | `contend` | `cpu_shares: 2` | Burns every core so 7.1 has a "contended host" column |
| `k6` | `grafana/k6:latest` | `load` | `cpus: "2.0"` | Load generator. A throttled load generator reports its own latency |

Postgres runs with `-c max_connections=100 -c shared_buffers=256MB`. The
100 is not incidental — [7.4](../04-sizing-a-python-web-service-in-a-container/README.md)
is the arithmetic of blowing through it.

## Environment variables

> **`python:3.14t-slim` does not exist.** Checked against Docker Hub on the
> date of the run in [`VERIFIED.md`](../VERIFIED.md): the official `python`
> repository publishes no free-threaded tag — no `3.14t`, no `*t-slim`, no
> `freethreaded` variant — and `python:3.14-slim` ships only a GIL-enabled
> interpreter (`sysconfig.get_config_var("Py_GIL_DISABLED")` is `0`, and there
> is no `python3.14t` binary on the PATH). The free-threaded rows of this
> experiment need an image you build yourself (CPython configured with
> `--disable-gil`) or one from a publisher that ships a free-threaded build.
> `run_7_7.sh` checks the registry first and says exactly this rather than
> blaming a missing wheel.


Every one of these is read by name somewhere in the stack. Later code
depends on the spelling.

| Variable | Default | Reaches |
|---|---|---|
| `PYTHON_IMAGE` | `python:3.13-slim` | `app/Dockerfile` `ARG`. 7.7 sets it to `python:3.14-slim` and `python:3.14t-slim` |
| `WORKERS` | `1` | `uvicorn --workers`. Swept by 7.2 and 7.4 |
| `DATABASE_URL` | `postgresql://lab:lab@db:5432/container_lab` | `app/main.py` |
| `DB_SLEEP_S` | `0.020` | the `pg_sleep` inside `/db` |
| `POOL_MAX` | `10` | connection pool ceiling per worker process |
| `ANYIO_THREAD_TOKENS` | `40` | Starlette's thread limiter. 7.5 raises it on purpose |
| `API_CPUS` | `1.0` | `cpus:` on `api` → `cpu.max` |
| `API_MEM` | `1g` | `mem_limit:` on `api` → `memory.max` |
| `HOG_THREADS` | `8` | how many spinners the `hog` container starts |
| `TARGET` | `http://api:8000` | k6 |
| `RATE` | `40` | k6 `steady.js` arrival rate, requests/sec |
| `PEAK` | `160` | k6 `spike.js` final arrival rate |
| `DURATION` | `45s` | k6 `steady.js` |
| `ENDPOINT` | `/mixed` | which handler k6 hits |

## The endpoints

| Path | Shape | What it is for |
|---|---|---|
| `/cpu` | ~15ms of hashing, calibrated at import | The endpoint quota bites. Stands in for serialisation and template rendering |
| `/db` | one indexed `SELECT` plus `pg_sleep(DB_SLEEP_S)` | Nearly free on CPU. The `pg_sleep` models network + planning wait honestly |
| `/mixed` | query, then serialise | Enough CPU to drain the bucket, enough wait to keep many requests in flight. Shows throttling worst |
| `/stat` | `/sys/fs/cgroup/cpu.stat` as JSON | So a load script can read ground truth without a shell |
| `/healthz` | liveness | The check that stays green through every failure in this topic |

`/cpu`'s cost is measured at import rather than hardcoded — an M1 core and
a c6i core are not the same core, and a wrong constant silently changes
what all seven experiments measure.

---

## How to run

```bash
cd 01-machine/07-inside-a-container/00-harness

docker compose up -d --build

# ground truth, from inside the container -- this is the habit to build
docker compose exec api cat /sys/fs/cgroup/cpu.max
docker compose exec api cat /sys/fs/cgroup/cpu.stat
docker compose exec api cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory.events

# sample throttling once a second for the duration of a run
./observe/watch.sh api &

docker compose --profile load run --rm --no-deps -e RATE=40 -e ENDPOINT=/mixed k6 run /scripts/steady.js
docker compose --profile contend up -d hog     # only for 7.1's busy column

docker compose down -v
```

Compose knobs the sub-topics change between runs (top-level shorthand;
`deploy.resources.limits` is the equivalent under the spec):

```yaml
services:
  api:
    cpus: "1.0"          # -> cpu.max "100000 100000"
    cpu_shares: 512      # -> cpu.weight (relative, contention-only)
    cpuset: "0"          # -> cpuset.cpus
    mem_limit: 256m      # -> memory.max
```

## Four ways to get meaningless numbers

1. **`docker compose restart` after changing a limit.** A restart reuses
   the old cgroup. Use `docker compose up -d --force-recreate api`, then
   `cat` the cgroup file and confirm the kernel got what you meant. This is
   the single most common way this topic produces confident nonsense.
2. **Closed-loop load.** `constant-vus`, or any hand-rolled "N threads in a
   `while` loop", slows its own send rate the instant the server slows down.
   The queue never builds and the tail you are hunting never appears. Both
   k6 scripts here are open-loop for that reason, and `local/openloop.py`
   is the stdlib equivalent.
3. **Measuring the load generator.** If k6's `dropped_iterations` is not
   near zero, k6 ran out of VUs and every latency below it is understated.
   Raise `preAllocatedVUs` before believing anything.
4. **Letting `docker compose run` recreate the thing you are measuring.**
   The `k6` service `depends_on: api`, so `docker compose run k6` will
   quietly re-create `api` whenever the api container's stored config no
   longer matches what the current environment resolves to — which is
   exactly what happens the moment an experiment sets `WORKERS=4` or
   `API_CPUS=0` for one cell. You get a *different* container, with the
   *default* limits, for the run you are about to measure, and the cgroup
   file you `cat`-ed two seconds earlier belonged to a container that no
   longer exists. Always pass `--no-deps`. Every k6 line in this topic
   does; verify with `docker compose ps -q api` before and after.

## Two knobs Compose cannot express

The CFS **period length** and **`cpu.max.burst`** have no Compose key.
Write them into the container's cgroup directly, from the host or a
privileged sidecar mounting the cgroup filesystem — that is what
`../02-throttled-at-30-percent-cpu/docker/write_cgroup.sh` is for.
Kubernetes cannot express burst either, which is most of why almost nobody
has seen `nr_bursts` be nonzero in production.

## On macOS (this machine: macOS 27, arm64)

Everything above runs inside Docker Desktop's linuxkit VM, so the cgroup
files exist and the experiments work as written. Two caveats:

- **Do not run the probes on the host.** There is no `/sys/fs/cgroup` on
  Darwin. `local/cgroup.py` returns `None` for every reading there, which is
  the correct answer, not a failure.
- **The "host" your runtime sees is the linuxkit VM, not your Mac.** Give
  that VM at least 4 CPUs in Docker Desktop's settings, or the host-vs-quota
  gap this entire topic is about will be too small to observe. Confirm with
  `docker info | grep -i "cgroup\|CPUs"` before you start.

When the daemon is down, `local/cfs_sim.py` applies the kernel's accounting
rule to real threads in userspace: refill a bucket with `quota_us` every
`period_us`, park every thread when it empties. It reproduces the latency
*signature* — freezes quantised to the period, a throttle ratio that moves
independently of average CPU — on this machine, today. It is not the
kernel, it prints a FALLBACK banner saying so, and no row it produces
belongs in a table next to a real `cpu.stat` reading.
