# 7.7 — Free-threaded Python, honestly, in 2026

### The takeaway (read this first)

**The one idea:** removing the GIL does not remove the quota.
Free-threading changes *how you spend* your CPU allowance; it does not
enlarge it, and by putting more runnable threads in one cgroup it can make
throttling worse.

**Why it matters in practice:** free-threaded Python is the most hyped
change to this lab's primary language in a decade, and the hype is aimed
at a bottleneck most containerised web services do not have. If your p99
is CFS throttling or waiting on Postgres, the GIL was never in the way,
and swapping interpreters will move nothing. The place it genuinely helps
is the one nobody advertises: it collapses *process count*, which is the
variable every ceiling in
[7.4](../04-sizing-a-python-web-service-in-a-container/README.md) is
multiplied by.

**You'll know it landed when:** asked "should we move to free-threaded
Python", you answer with a question about which ceiling currently binds,
and you can say what you would measure to find out — and when you check
`sys._is_gil_enabled()` before believing any free-threading benchmark,
including your own.

---

## The concept

### State of the world, as of this writing (August 2026)

- Free-threading shipped **experimental** in CPython 3.13 (October 2024).
- It became an **officially supported** build in 3.14 (October 2025),
  under PEP 779's phase II.
- It remains an **opt-in build** (`python3.14t`), not the default
  interpreter. You get it by installing a different binary, not a flag.
- 3.15 is due in late 2026 and is expected to continue narrowing the gap
  for C extensions; read that release's own notes rather than this line,
  because "which ABI guarantee landed in which release" is exactly the
  detail that slips.

Costs, from CPython's own [free-threading
HOWTO](https://docs.python.org/3/howto/free-threading-python.html) and the
release notes rather than from any blog: measurable single-threaded
overhead (smaller on aarch64 than on x86-64 — check the numbers on the
page matching your interpreter, since they have improved between releases)
and meaningfully higher memory use, from larger object headers,
immortalised interned strings, mimalloc, and deferred reclamation. Do not
take a number for either from a README; both are version-dependent and
both are measurable on your own machine in minutes.

### What it changes for a container-bound service

**Does not help:** your latency, if your latency is CFS throttling or a
database wait. Neither of those cares about the GIL. A frozen cgroup
freezes free-threaded interpreters exactly as thoroughly.

**Genuinely helps: process count.** This is the real argument and it is a
7.4 argument, not a performance one. N workers cost N interpreters, N
connection pools, N copies of every in-process cache, N× the memory
overhead. One free-threaded process with a thread pool collapses that to
one pool with arithmetic you can do on paper — a direct fix for the
connection ceiling, and a real reduction in RSS at the same concurrency.

**Can hurt: the throttle ratio.** More runnable threads in one cgroup
drain the quota bucket faster. That is the arithmetic from
[7.2](../02-throttled-at-30-percent-cpu/README.md), and it does not care
why the threads are runnable. At a fixed `cpus:`, the free-threaded build
can show a *higher* throttle ratio for the same offered work — and,
because the freezes are quantised to the period, a worse tail while doing
the same throughput.

### The check you must do first, every time

Any C extension not marked free-thread-safe causes the interpreter to
**re-enable the GIL at import**, with a warning that is easy to miss in a
container's log stream. `psycopg2` is a C extension. So is anything
wrapping a native library. Assert it explicitly at startup:

```python
import sys
assert not sys._is_gil_enabled(), "GIL was re-enabled -- find the extension"
```

A free-threading benchmark that shows no difference is far more often a
silently re-enabled GIL than a real null result. Check before you conclude.

---

## How each language actually gets there

**One language, and it is the whole point.** The GIL is a CPython
implementation detail; there is no Node, Go, Rust, C++ or Java version of
"what happens when we remove it". The nearest cross-runtime comparison
already exists in this lab as Layer 1
[Topic 3](../../03-concurrency-models/README.md), which shows five other
runtimes that never had this constraint and what they pay for that
instead.

The one comparison worth carrying in your head while you read the results:
Python is arriving, in 2026, at the thread-shared-memory model that Java
and C++ have had since the 1990s — including its hazards. Free-threaded
CPython makes data races between Python-level objects genuinely possible
in code that was previously serialised by the GIL, which is Layer 1
[Topic 4](../../04-races-and-atomicity/README.md) becoming newly relevant
to Python for the first time. "The GIL protected my sloppy code" was
always a joke that was slightly true; in a free-threaded build it stops
being true at all.

---

## The experiment

Two images from the same Dockerfile — one on `python:3.14-slim`, one on
`python:3.14t-slim` — running identical code at `cpus: "2.0"`, asserting
`sys._is_gil_enabled()` is `False` at startup on the `t` build.

Compare across the two:

- `/cpu` — CPU-bound. Should improve when threads can genuinely run in
  parallel. This is the endpoint the GIL was in the way of.
- `/db` — IO-bound. Should not improve; the wait was never a GIL wait.
- **RSS** at the same concurrency, which is where the process-count
  argument is won or lost.
- **Throttle ratio**, which is where it can be lost anyway.

Run the free-threaded side twice: once with the same worker count as the
GIL build, and once as a single process with a thread pool sized to match
the total concurrency. The second configuration is the one the whole
argument is actually about.

Note on pins: the harness's `PYTHON_IMAGE` defaults to `python:3.13-slim`,
which is not free-threaded. This sub-topic overrides *both* sides to 3.14
so the comparison differs in exactly one variable — the interpreter build,
not the minor version. Do not compare 3.13 against 3.14t; you would be
measuring two changes at once.

## How to run

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


```bash
cd 01-machine/07-inside-a-container/00-harness

# the GIL build
PYTHON_IMAGE=python:3.14-slim  API_CPUS=2.0 WORKERS=4 \
  docker compose up -d --build --force-recreate api
docker compose exec api python -c "import sys; print(sys._is_gil_enabled())"
docker compose --profile load run --rm --no-deps -e ENDPOINT=/cpu -e RATE=60 k6 run /scripts/steady.js

# the free-threaded build -- same everything else
PYTHON_IMAGE=python:3.14t-slim API_CPUS=2.0 WORKERS=4 \
  docker compose up -d --build --force-recreate api
docker compose exec api python -c "import sys; print(sys._is_gil_enabled())"   # must be False

# and the configuration the argument is really about
PYTHON_IMAGE=python:3.14t-slim API_CPUS=2.0 WORKERS=1 \
  docker compose up -d --build --force-recreate api

# all three configurations, with the GIL assertion and the cpu.stat reading
# paired to each row (it refuses to report numbers if the assertion fails)
cd ../07-free-threaded-python-honestly-in-2026
./docker/run_7_7.sh
RATE=90 ./docker/run_7_7.sh

# the interpreter check itself -- runs on macOS too
python3 python/gil_check.py
python3.14t python/gil_check.py    # if you have the free-threaded build
PYTHON_GIL=0 python3.14t python/gil_check.py
```

`gil_check.py` is the one script in this topic that is useful on the Mac
host: it tells you which interpreter you are on and whether the GIL is
enabled, and that answer is not container-specific. Everything with a
`cpus:` or a throttle ratio in it must run inside the Linux container.

## Predict, then record

Predict all four columns for both builds *before* running — in particular,
predict the sign of the RSS difference and the sign of the throttle-ratio
difference. They may not point the same way.

| Build | `sys._is_gil_enabled()` | `/cpu` p99 | `/db` p99 | RSS | throttle ratio |
|---|---|---|---|---|---|
| 3.14, 4 workers | | | | | |
| 3.14t, 4 workers | | | | | |
| 3.14t, 1 worker + thread pool | | | | | |

| Build | PG connections at peak | `/cpu` req/s | `/db` req/s |
|---|---|---|---|
| 3.14, 4 workers | | | |
| 3.14t, 1 worker + thread pool | | | |

**Broken, not merely surprising.** If the free-threaded build shows no
difference on `/cpu`, check `sys._is_gil_enabled()` before concluding
anything — a re-enabled GIL is by far the most likely explanation and it is
invisible unless you look. If RSS is *lower* on 3.14t at the same worker
count, you are almost certainly comparing different worker counts rather
than different interpreters; free-threading's per-object overhead goes the
other way. If `/db` improves noticeably, the improvement is not the GIL:
look for a changed pool size or a warmed page cache. If the container will
not start on `python:3.14t-slim`, an extension in `requirements.txt` has no
free-threaded wheel — that is a result too, and worth recording as one.

## Answer before moving on

1. A service is at 30% average CPU, throttle ratio 0.25, p99 6× p50, and
   spends most of its time awaiting Postgres. Someone proposes moving to
   free-threaded Python. Give your answer in one sentence, and name the
   fix you would ship instead.
2. Free-threading lets you drop from 4 processes to 1. Walk each of 7.4's
   four ceilings and say which improve, which are unchanged, and which get
   *worse* — with the mechanism for each.
3. The GIL used to serialise access to Python-level objects. Give a
   concrete piece of everyday Python — a counter, a memo dict, a cache —
   that was accidentally safe under the GIL and is not safe in a
   free-threaded build, and name the primitive that fixes it. (Layer 1
   [Topic 4](../../04-races-and-atomicity/README.md) is the companion
   reading.)
4. Free-threading has a real single-threaded cost. Under what container
   configuration would that cost dominate the parallelism benefit
   entirely, so that the free-threaded build is *strictly* worse? Give the
   quota and the workload shape.

## Next up

Back to the [topic index](../README.md), then Layer 2 — the network. Much
of what you just measured as "the database is slow" turns out to live
there, and the two compose: a connection pool is a queue for a scarce
resource, and you have now seen what happens to queues when the process
holding them is frozen for 87ms at a time.
