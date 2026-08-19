# VERIFIED — Layer 1 · Topic 7, executed on this machine

**Date:** 2026-08-19
**Verifier:** an independent pass. Every program below was compiled and run
here, from the command its own topic README gives. Nothing was accepted on
the strength of the author's report.

## What this file is, and what it is not

This records that the code in this folder **executes on this machine and
prints what its header comment says it will print**. It does not record
that anything was learned. The `Predict, then record` tables in the seven
topic READMEs are still blank, deliberately — they are the reader's
exercise, and filling them in from someone else's run defeats the point.

Where a captured number appears below it is real, because it came out of a
run performed for this file. Nothing is quoted from a design document, a
blog post, or the author's summary.

---

## The machine

| | |
|---|---|
| OS | macOS 27.0 (Darwin 27.0.0), arm64 / Apple silicon, 8 logical CPUs, 16 GiB |
| Python | 3.13.5 (GIL enabled; no free-threaded build present) |
| Node.js | v24.14.0 — V8 13.6.233.17-node.41, **libuv 1.51.0** |
| Go | **go1.24.5** darwin/arm64 — *pre-1.25, so `GOMAXPROCS` is not cgroup-aware* |
| Rust | rustc 1.97.1 (8bab26f4f 2026-07-14), cargo |
| C++ | Apple clang 21.0.0 (clang-2100.1.1.101); `-pthread` accepted |
| Java | Temurin JDK 21.0.2+13 LTS (virtual threads available) |
| Docker | CLI 28.1.1; **daemon UP as of the 2026-08-19 unblock pass** — server 29.5.3, linux/arm64, 4 CPUs, 4.804 GiB, cgroup v2, cgroupfs driver, Compose v5.1.4 |
| k6 | **not installed on the host** — every k6 run below went through the harness's `k6` compose service (`grafana/k6:latest`) |
| Postgres | `pg_isready` → `/tmp:5432 - accepting connections` (host server up) |
| `timeout(1)` | **not present** on macOS; a shell helper was used to cap every run |

Two consequences that shape the whole table below:

1. **Darwin has no cgroupfs.** There is no `/sys/fs/cgroup`, no `cpu.max`,
   no `cpu.stat`, no `memory.max`, no `memory.events`, and no cgroup OOM
   killer. Every program here detects that and prints a labelled FALLBACK
   banner or `n/a` rather than a zero. That behaviour was verified
   individually, not assumed.
2. **The Docker daemon was down for the first pass,** so nothing
   container-side ran then. Every blocked item below named the one command
   that unblocks it. On **2026-08-19** the daemon came up and every one of
   those commands was run — see *Unblock pass* at the end of this file, which
   is where the container-side numbers live. Statuses in the tables below have
   been updated in place; the reason a row is still blocked is stated where it
   is still blocked.

---

## Program-by-program status

`RAN` = executed as documented. `FIXED-THEN-RAN` = it was broken or it
demonstrated something other than its claim; the fix is described, and it
was re-run afterwards. `BLOCKED` = cannot run here, with the reason and the
unblock command.

### 00-harness

| Program | Status | Notes |
|---|---|---|
| `local/cgroup.py` | RAN | Prints every reading as `None` on Darwin, with the reason. Correct. |
| `local/cfs_sim.py` | FIXED-THEN-RAN | Had no `__main__`: `python3 cfs_sim.py` printed nothing at all. Added a self-demo (1 thread vs 4, same CPU, same bucket). Observed here: throttle ratio `0.400` → `1.000`. |
| `local/openloop.py` | FIXED-THEN-RAN | Same defect, and 7.5's own fallback text names it as a command. Added a `__main__` that drives a URL, or a synthetic handler with a loud banner when there is no server. Observed: 400 scheduled / 400 completed / 0 dropped at 40 req/s. |
| `docker-compose.yml` | RAN (up, unblock pass) | `docker compose config` parses it without the daemon. Services are exactly `api`, `db`, `hog`, `k6`; `hog` is in profile `contend`, `k6` in profile `load`; Postgres gets `max_connections=100 -c shared_buffers=256MB` — all as the harness README specifies. |
| `observe/watch.sh` | RAN (unblock pass) | `./observe/watch.sh api 55` alongside a k6 run. Prints `quota: 100000 100000` and a per-second `thr/s` / cumulative `ratio` pair that tracked `nr_throttled` correctly across 55 samples. |
| `app/main.py`, `app/Dockerfile`, `app/init.sql` | FIXED-THEN-RAN (unblock pass) | Image builds; `api` + `db` come up healthy; all five endpoints answer 200 and `init.sql` seeded (`/db` → `{"id":1}`). Startup banner from inside the container: runtime sees 4 CPUs, `cpu.max` 1.00 CPU, `memory.max` 1073741824. **Fixed:** `_calibrate()` took one timing sample, so four workers calibrating simultaneously in one cgroup each measured an inflated per-round cost and picked a *cheaper* handler than one worker did — 10.8 ms of CPU per `/cpu` request at 4 workers against 16.4 ms at 1, which inverts every worker-count comparison in 7.2 and 7.4 before it starts. Now best-of-5, pinnable via a new `CPU_ROUNDS` env var, and reported by `/stat`. |
| `load/steady.js` | FIXED-THEN-RAN (unblock pass) | Ran through the `k6` service. **Two defects, both fatal to the topic.** (1) `handleSummary` read `d['p(50)']`, which k6 does not compute by default: every run died with `TypeError: Cannot read property 'toFixed' of undefined or null` *after* paying its full duration. (2) `constant-arrival-rate` fires at fixed intervals — the least bursty load possible — so 7.2's headline measured `nr_throttled 0`. Added `summaryTrendStats`, a `BURST=N` mode (N parallel requests per arrival, arrival rate `RATE/N`, average offered rate held constant), and honest reporting of the rate actually sent. |

### 7.1 — the three knobs are not the same knob

| Program | Status | Notes |
|---|---|---|
| `python/three_knobs.py` | RAN | ~25s. Userspace model, FALLBACK banner shown. Observed here: no-ceiling row fell 262.0 → 146.2 req/s under contention (-44%); the quota row moved 67.0 → 67.2 (+0%). That contrast is the claim, and it held. |
| `docker/run_7_1.sh` | FIXED-THEN-RAN (unblock pass) | Rewritten and run end to end; six cells, real cgroup readings. It previously measured **one** of the three configs and printed "NEXT: uncomment `cpu_shares: 512` in docker-compose.yml" — a 2×3 table that produced 2 cells and asked the reader to hand-edit between runs. It now drives (b) and (c) through generated Compose overrides, defaults to `WORKERS=4`, and warms the workers before measuring. Numbers in the unblock section. |

### 7.2 — throttled at 30% CPU (all six languages)

| Program | Status | Notes |
|---|---|---|
| `python/quota_freeze.py` | FIXED-THEN-RAN | ~76 s (five 15 s rows), on the host and again inside `--cpus=1.0`. **Fixed in the unblock pass:** in a container it printed `quota actually enforced: 1.00 CPU` and then a table produced entirely by the userspace model in `cfs_sim.py`, with no banner — a process cannot rewrite its own cgroup, so the "2.0 CPU", "50 ms period" and "+ burst" rows could not be kernel-enforced from inside it even in principle. It now says so on both platforms. In-container baseline: 37% average CPU, ratio 0.060, p50 42 ms, p99 109 ms, heartbeat gap 78 ms. |
| `nodejs/quota_freeze.js` | RAN | ~50s. At-rest census: 7 OS threads before any `worker_threads` exist. |
| `golang/quota_freeze.go` | RAN | ~47s. Correctly detects a pre-1.25 toolchain and says `GOMAXPROCS` ignores `cpu.max` here. `go run` works without a `go.mod` (stdlib only). |
| `golang` with `GODEBUG=containermaxprocs=0` | RAN | Runs; the program states plainly that the GODEBUG does nothing on 1.24 because there is no new behaviour to switch off. Honest. |
| `rust/quota_freeze` | RAN | ~61s with `cargo run --release`. Deps resolved from the local cargo cache — no network needed. Row 4 measures `spawn_blocking`'s 512-thread pool, as its README says. |
| `cpp/quota_freeze.cpp` | RAN | `g++ -O2 -std=c++17 -pthread` compiles clean, no warnings. ~48s. Takes the Darwin branch: `sysctl` for CPU count, mach for the thread census, `sched_getaffinity` reported `n/a` rather than faked. |
| `java/QuotaFreeze.java` | RAN | `javac` clean. ~60s. Row 4 is virtual threads; observed 0.087 (platform pool) vs 0.100 (virtual) — Loom does not move a CPU ceiling, which is the point being made. |
| `python/cpu_stat_watch.py` | FIXED-THEN-RAN (unblock pass) | The documented unblock command **did not work**: the harness image never contained this file, and `Path(__file__).resolve().parents[2]` raised `IndexError` from `/srv`, so it died before printing anything in the one environment it exists for. Now resolves its imports from the repo layout *or* `/srv`; the image carries `openloop.py` too; the RUN block documents `docker compose cp … api:/srv/`. Live output in the unblock section. |
| `docker/run_7_2.sh` | FIXED-THEN-RAN (unblock pass) | **Was broken for every invocation**: it ran `cd "$(dirname "$0")/../../00-harness"` and only *then* computed `HERE="$(cd "$(dirname "$0")" && pwd)"`, so with a relative `$0` the second `cd` failed and the script died at line 38 with `cd: ./docker: No such file or directory` before it could even check for the daemon. `HERE` is now resolved before the first `cd`. With the daemon up, three more defects surfaced — see the unblock section: `docker compose run` silently re-creating the container under test, the throttle-ratio line never printing because a command substitution ate awk's stdin, and a baseline that measured `nr_throttled 0`. All five cells now run and the headline reproduces. |
| `docker/write_cgroup.sh` | RAN (unblock pass) | Both forms. `"50000 50000"` → cpu.max read back as `50000 50000` and `nr_periods` doubled to 612 over a 45 s run, confirming 50 ms periods. `--burst 100000` → `nr_bursts 66`, `burst_usec 1768505` in the same run. The sidecar located the cgroup under `/sys/fs/cgroup/docker/<id>` on Docker Desktop's linuxkit VM. |

### 7.3 — ask six runtimes how big the machine is

All six probes ran on the host and produced the single-column answer with
`n/a` in the cgroup rows, which is the correct result here.

| Program | Status | Notes |
|---|---|---|
| `python/cpuinfo.py` | RAN | `os.cpu_count()` 8, `os.process_cpu_count()` 8, `sched_getaffinity` `n/a` (Linux-only), quota `n/a`. |
| `nodejs/cpuinfo.js` | RAN | Prints the **live** `process.versions.uv` (1.51.0) instead of trusting a README's version claim, exactly as its own prose insists. |
| `golang/cpuinfo.go` | RAN | Detects the pre-1.25 toolchain and prints which behaviour you actually got. |
| `rust/cpuinfo` | RAN | `cargo run --release`, zero dependencies. |
| `cpp/cpuinfo.cpp` | RAN | Compiles clean; Darwin branch used. |
| `java/CpuInfo.java` | RAN | Reads `ActiveProcessorCount` as a *value* rather than inferring it. |
| `java -XX:ActiveProcessorCount=2` | RAN | `availableProcessors()` → 2, and the flag is reported as `VM_CREATION`. The override works. |
| `java -XX:-UseContainerSupport` | RAN (unblock pass) | Ran in `eclipse-temurin:21` under `--cpus=1.5`: `availableProcessors()` → **4** with the flag against **2** without it, and the flag reports as `VM_CREATION`. Previously blocked because it is a Linux-only HotSpot flag. On this macOS JDK it aborts: `Unrecognized VM option 'UseContainerSupport'` / `Could not create the Java Virtual Machine`. On the macOS JDK it still aborts, and the README still says so. |
| `docker/run_7_3.sh` | FIXED-THEN-RAN (unblock pass) | Both plain and `--only python,go`, exit 0. **Fixed:** it bind-mounted only its own sub-topic directory, so `python /w/python/cpuinfo.py` died on `ModuleNotFoundError: No module named 'cgroup'` — the harness's `cgroup.py` lives two directories up. It now mounts the whole of `07-inside-a-container`. Full 6-runtime × 2-column matrix in the unblock section. |

### 7.4 — sizing a Python web service in a container

| Program | Status | Notes |
|---|---|---|
| `python/sizing_sweep.py` (part 1) | RAN | Instant. Default spec, and `--replicas 3 --workers 4`, which correctly flips ceiling 2 to `BREACH` at 240 backends against 97 usable. |
| `python/sizing_sweep.py --measure` | FIXED-THEN-RAN (unblock pass) | Four defects, then a clean sweep over `{1,2,4,8}` workers at `cpus: 2.0`. Throughput peaks at 2 workers — `floor(quota)` — and *falls* beyond it while the throttle ratio climbs 0.000 → 0.091 → 0.519 → 0.573. Table in the unblock section. |

### 7.5 — the sync driver inside the async endpoint

| Program | Status | Notes |
|---|---|---|
| `python/run_variants.py` | FIXED-THEN-RAN (unblock pass) | All four documented forms run. The override was **not** correct on the one point that mattered: it mounted `variants.py` at `/app/variants.py` while the image's `WORKDIR` is `/srv`, so uvicorn could not import the module at all. Three more defects and the full result in the unblock section — at `--quota 0.5` the worst p99 and the highest throttle ratio land on *different rows*, which is the entire point of the sub-topic. |
| `app/variants.py` | RAN (unblock pass) | Served all four variants over the harness `api` service once the mount point was corrected to `/srv`. `ANYIO_THREAD_TOKENS` took effect: variant 3 reported 100 tokens against variant 2's 40. |

### 7.6 — memory: the limit that kills you without a traceback (all six)

| Program | Status | Notes |
|---|---|---|
| `python/oom.py` | RAN | Both default and `--free`. Reached its self-imposed 512 MiB, said plainly that nothing was enforced. Reports Darwin's `getrusage` figure as a **peak** and says a peak that did not fall proves nothing. |
| `nodejs/oom.js` | FIXED-THEN-RAN | **`--heap` did not test the heap.** It allocated `Float64Array`, whose backing store V8 puts *outside* the JS heap — so `--heap` and `--buffer` printed the same thing (`heapUsed` flat at 3–4 MiB, `external` climbing to 513 MiB) and the documented `exit 134, with a stack trace` was unreachable. Now allocates a SMI-backed `Array`, which lives in V8's old space. Verified after the fix: `--heap` → `heapUsed 516 MiB / external 1 MiB`; `--buffer` → `heapUsed 3 MiB / external 513 MiB`; and under `node --max-old-space-size=128` it dies with `FATAL ERROR: Reached heap limit` and **exit code 134**, which is what the README promised all along. |
| `golang/oom.go` | FIXED-THEN-RAN | **The GC death spiral was not being demonstrated.** The README said `GOMEMLIMIT=64MiB go run oom.go  # the GC spiral, no container needed`, and the program's own banner said the GC-CPU column *was* the spiral — but the retained heap was `[]byte`, which contains no pointers, so the mark phase had nothing to trace. Measured: GC CPU **1.0%** with `GOMEMLIMIT=64MiB`, indistinguishable from the run with no limit at all. Added a `-pointers` mode that retains a pointer-dense structure. Measured after the fix, same program, same 512 MiB: **22.4% GC CPU, 54 GCs, 2.0s** with `GOMEMLIMIT=64MiB` versus **11.7% GC CPU, ~10 GCs, 0.4s** without it. The file and the README now say that the cost is per pointer, not per byte. |
| `rust/oom` | RAN | `--try-reserve`. Prints `n/a` for RSS on Darwin and states why: `getrusage` offers a peak, and it will not print one number under the name of another. |
| `cpp/oom.cpp` | RAN | Both modes. `--reserve-only` is the sharpest single result in the folder: **512 MiB allocated, RSS 3 MiB** — the address-space-versus-RSS gap, shown rather than asserted. Default mode: 512 MiB allocated, RSS 515 MiB. |
| `java/Oom.java` | RAN | `--heap` under `-Xmx64m` caught `OutOfMemoryError: Java heap space` with a stack trace, then the shutdown hook ran — proving it was not a SIGKILL. `--direct` also ran. |
| `docker/run_7_6.sh` | FIXED-THEN-RAN (unblock pass) | Seven language rows at `--memory=256m` plus the `--high` soft-limit run. Four defects fixed, two of which meant the row was measuring something other than its subject — details and the evidence table in the unblock section. |

### 7.7 — free-threaded Python, honestly, in 2026

| Program | Status | Notes |
|---|---|---|
| `python/gil_check.py` | FIXED-THEN-RAN | Ran on CPython 3.13.5 (GIL on). **The legend contradicted the arithmetic**: it printed `speedup is 1-thread / N-thread` while the code computes `one * threads / many`. With 1 thread at 291 ms and 4 threads at 1123 ms it reported `1.04x`, which reads as a contradiction until you know each thread does the full workload. The legend now states the throughput formula and what `1.00x` means. Measured after the fix: pure-Python bytecode **1.04x**, hashlib **3.81x**, `time.sleep` **4.02x** — row 1 is the only one the GIL was ever in the way of, which is the lesson. |
| `python3.14t` variants | **STILL BLOCKED** | The recorded unblock command does not work, and the reason is not the machine. Docker Hub's official `python` repository publishes **no free-threaded tag at all** — `python:3.14t-slim` 404s (`failed to resolve source metadata`), and a tag search for `3.14t`, `*t-slim` and `freethreaded` returns nothing. `python:3.14-slim` ships a GIL-enabled interpreter only: `sysconfig.get_config_var("Py_GIL_DISABLED")` is `0` and there is no `python3.14t` on the PATH. **What it would need:** a free-threaded image — CPython 3.14 configured with `--disable-gil` in a derived Dockerfile, or an image from a publisher that ships one — then `PYTHON_IMAGE=` that image. Nothing else changes. |
| `docker/run_7_7.sh` | FIXED-THEN-RAN, **one row of three still blocked** | Both plain and `RATE=90`, exit 0. Row A (`python:3.14-slim`, 4 workers, `cpus: 2.0`) ran fully. Rows B and C need `python:3.14t-slim`, which does not exist (row above). **Fixed:** on a missing image the script reported "an extension in requirements.txt has no free-threaded wheel" — a confident diagnosis of something that never happened. It now checks the registry first and names the real cause. |

---

## Defects found and fixed

Nine, in three groups.

**Ran but demonstrated the wrong thing** — the expensive kind, because the
output looks fine:

1. `06/nodejs/oom.js` — `--heap` allocated outside the V8 heap, making both
   modes identical and the documented exit 134 unreachable.
2. `06/golang/oom.go` — a pointer-free heap gave the GC nothing to trace, so
   the "death spiral" the file and README both promised measured 1.0% GC CPU.
3. `07/python/gil_check.py` — the printed formula was not the computed one.

**Broken outright:**

4. `02/docker/run_7_2.sh` — died at line 38 on every invocation.
5. `00-harness/local/openloop.py` — documented as a command, had no
   `__main__`, printed nothing.
6. `00-harness/local/cfs_sim.py` — same, and silence is indistinguishable
   from a run that measured zero.

**Documentation that does not survive being followed literally:**

7. `03/README.md` — handed the reader `java -XX:-UseContainerSupport`, which
   aborts the JVM on this machine. Now labelled Linux-only.
8. `02`, `03`, `06/README.md` — the run blocks chained bare `cd`s, so
   following them top to bottom left you in the wrong directory from the
   third command on. Now subshells.
9. `01/docker/run_7_1.sh` and `02/python/cpu_stat_watch.py` — each pointed at
   its sibling with a relative path that does not resolve from where the
   README tells you to stand.

## One caveat worth carrying into your own runs

In `02/python/quota_freeze.py` the **throttle-ratio** column is stable and
reproduces the claim across repeated runs; the **p99 and heartbeat-gap**
columns are not. Across four runs on this host the baseline ratio stayed in
0.060–0.079 while p99 on a single row swung from 87 ms to 2089 ms, and the
row carrying the worst p99 changed between runs. That is expected and it is
worth knowing why: this is a *userspace model* competing for eight real
cores with everything else on a laptop, on asymmetric P/E cores, with no
cgroup isolating it. Inside a container with a real `cpu.max` the tail
columns settle down. Read the ratio column here; read the tail columns in
the container.

The same is true, less severely, of the other five language versions in 7.2.

## Coverage

Every topic folder has code, and every one carries the languages its own
README's *How each language actually gets there* section commits to:

| Topic | README says | Present | |
|---|---|---|---|
| 7.1 | Python + shell, with a stated reason for fewer | `python/`, `docker/` | ✓ |
| 7.2 | all six | python, nodejs, golang, rust, cpp, java | ✓ |
| 7.3 | all six | python, nodejs, golang, rust, cpp, java | ✓ |
| 7.4 | Python only, with a stated reason | `python/` | ✓ |
| 7.5 | Python only, with a stated reason | `app/`, `python/` | ✓ |
| 7.6 | all six | python, nodejs, golang, rust, cpp, java | ✓ |
| 7.7 | Python only (CPython's GIL) | `python/` | ✓ |

Nothing is silently narrowed. `topicsIncomplete` is empty.

## No invented numbers

All eight topic READMEs were grepped for figures presented as observations.
The only numbers in them are derived arithmetic (`100ms ÷ 8 = 12.5ms`, the
`87.5ms` freeze that follows), documented kernel constants (the 5ms
`sched_cfs_bandwidth_slice_us` default, the 100000µs period), and design
targets for the harness (`/cpu` calibrating near 15ms). No measured result
is asserted anywhere in the prose.

The `Predict, then record` tables in all seven topic READMEs were checked
row by row and are **blank**. They stay that way.

---

# Unblock pass — Docker daemon up

**Date:** 2026-08-19
**What changed:** the Docker daemon came up. Every command recorded as an
unblock above was run. Sixteen of the twenty-five programs in this folder
had never executed before this pass; this is their first contact with a
runtime, and it shows.

## The machine, this time

| | |
|---|---|
| Docker | Desktop 4.78.0 (229452); **server 29.5.3**, `linux/arm64`, containerd v2.2.4, runc 1.3.5 |
| Compose | v5.1.4 |
| The VM | **4 CPUs, 4.804 GiB**, cgroup **v2**, driver `cgroupfs`, `cgroupns` supported |
| cgroup files | present and readable from inside containers: `cpu.max`, `cpu.stat`, `cpu.weight`, `cpuset.cpus.effective`, `memory.max`, `memory.high`, `memory.events`, `cpu.pressure`, `memory.pressure` |
| k6 | still not on the host; every k6 line below ran as the compose `k6` service |
| Project name | `COMPOSE_PROJECT_NAME=m1t7-harness` throughout, so nothing collided with pre-existing stacks |

**Honest note on contention.** This laptop was not idle. An unrelated
`interview_*` compose stack (9 containers, ~1.3 GiB, ~4% CPU) was up for the
whole pass. A second unrelated stack (`craft-lab-*`) came up at about 05:22
and was therefore concurrent with the 7.7 run and the later 7.6 rows; the
7.1–7.5 numbers predate it. Four CPUs are not many to share. Ratios and
throughput columns reproduced across repeats; single-run p99 figures did not
always, and are labelled where that matters.

---

## The defect that invalidated every container measurement in the topic

`docker compose run k6` **silently re-creates the container you are about to
measure.** The `k6` service declares `depends_on: api`, so Compose brings
dependencies up to date before running — and "up to date" means *matching
what the current environment resolves to*, not *what the experiment just
configured*. The moment a cell sets `WORKERS=4` or `API_CPUS=0`, the api
container's stored config no longer matches, and `compose run` replaces it
with a fresh one carrying the **default** limits, milliseconds before the
load starts.

Demonstrated directly rather than deduced:

```
BEFORE k6 run: cpu.max=max 100000      cid=21eff4691b06
AFTER  k6 run: cpu.max=100000 100000   cid=30c8c8f17cd6     <- different container
--- now with --no-deps ---
BEFORE: cpu.max=max 100000   cid=c55d24c51c02
AFTER : cpu.max=max 100000   cid=c55d24c51c02               <- same container
```

The symptom was a `cpu.stat` delta that made no sense: a cell whose
`cat cpu.max` printed `max 100000` two seconds earlier reported
`nr_periods 210, nr_throttled 22`, which a cgroup with no quota cannot do —
`nr_periods` does not advance without a quota to advance it against. The
`before` reading came from one container and the `after` reading from
another.

`--no-deps` was added to all thirteen files that invoke the k6 service, and
the harness README's *Three ways to get meaningless numbers* is now four.

---

## 00-harness

Built and up: `api` (built from `app/Dockerfile`, fastapi 0.141.1,
starlette 1.6.0, uvicorn 0.52.3, asyncpg 0.31.0) and `db`
(`postgres:17`, healthy). All five endpoints answered 200 on the first try:

```
/healthz  {"ok":true}
/cpu      {"digest":"db86fdb5025245fd"}
/db       {"id":1}                       <- init.sql seeded
/mixed    {"db":{"id":1},"digest":"db86fdb5025245fd"}
/stat     {"cpu_stat":{"usage_usec":819051,...,"nr_periods":81,"nr_throttled":0,...},"cpu_quota":1.0,"pid":7}
```

Startup banner, from inside the container:

```
  the runtime thinks it has : {'os.cpu_count()': 4, 'os.process_cpu_count()': 4, 'len(sched_getaffinity(0))': 4}
  the kernel will let it use: 1.00 CPU (cpu.max)
  memory.max               : 1073741824
```

`local/cgroup.py` run inside the container reads the real thing —
`cpu.max -> 1.00 CPU, cpu.weight -> 100, cpuset -> 0-3` — which is the same
file that returns `None` for everything on Darwin.

`observe/watch.sh api 55` sampled through a k6 run and tracked
`nr_throttled` correctly.

First k6 run through the service, `RATE=40 ENDPOINT=/mixed`, 45 s:
1801 completed, p50 41.23 ms, p99 306.33 ms, max 431.99 ms,
`dropped_iterations` 0. `usage_usec` climbed ~0.66 CPU/s against a 1.0 CPU
quota with 9 throttled periods out of ~500.

### Defects fixed in the harness

1. **`load/steady.js` crashed after every run.** `handleSummary` read
   `d['p(50)']`, and k6 computes only `p(90)` and `p(95)` by default, so
   every single run ended with
   `TypeError: Cannot read property 'toFixed' of undefined or null` —
   *after* paying its full duration. Because the throw made k6 fall back to
   its built-in summary, the failure looked cosmetic; it was not, and the
   two downstream parsers (`sizing_sweep.py`, `run_variants.py`) were
   written against the fallback.
2. **`app/main.py` calibrated `/cpu` once, badly.** One timing sample, taken
   at import, per uvicorn worker. Four workers calibrate simultaneously
   inside one 1.0-CPU cgroup, each measures a per-round cost inflated by the
   other three, and each therefore picks *fewer* rounds. Measured: **10.8 ms
   of CPU per `/cpu` request at 4 workers against 16.4 ms at 1 worker**, same
   image, same quota, same offered rate. The 4-worker "baseline" was doing
   two thirds of the work of the 1-worker "fix" — the comparison inverting
   itself before it started. Now best-of-5, pinnable through a new
   `CPU_ROUNDS` env var, and reported by `/stat` so an experiment can pin it.
3. **No way to produce bursty load** — see 7.2 below, where it mattered most.
4. **`ps ax | grep -c "[u]vicorn"`** appears in three scripts as the
   worker-count column. `python:*-slim` has no `procps`, so it printed `0`
   for every configuration in every script. Replaced with a `/proc` walk.

---

## 7.1 — the three knobs, six cells

`./docker/run_7_1.sh` (`RATE=60`, `DURATION=25s`, `WORKERS=4`, `/cpu`).

| cell | `cpu.max` | `cpu.weight` | `cpuset.eff` | p50 | p99 | nr_periods | nr_throttled |
|---|---|---|---|---|---|---|---|
| idle / (a) `cpu_shares: 512` | `max 100000` | 59 | 0-3 | 15.1 ms | 64.5 ms | **0** | 0 |
| idle / (b) `cpus: "1.0"` | `100000 100000` | 100 | 0-3 | 14.3 ms | 45.5 ms | 258 | **3** |
| idle / (c) `cpuset: "0"` | `max 100000` | 100 | **0** | 858.0 ms | **4983.0 ms** | **0** | 0 |
| busy / (a) | `max 100000` | 59 | 0-3 | 14.5 ms | 129.0 ms | **0** | 0 |
| busy / (b) | `100000 100000` | 100 | 0-3 | 13.1 ms | 157.9 ms | 262 | **16** |
| busy / (c) | `max 100000` | 100 | **0** | 24.8 ms | **3206.5 ms** | **0** | 0 |

`hog` verified burning before the busy column: `m1t7-harness-hog-1 338.15%`.
Quota row: ratio 0.012 idle → 0.061 busy, average CPU 0.83 → 0.75.
`dropped_iterations` 0 in all six cells.

Three things this pass established that the file could not have known:

- **`nr_periods` is 0 without a quota.** Both no-ceiling rows report zero
  periods, so `nr_throttled: 0` there is structural, not lucky. That is a
  sharper version of the README's `cpuset` claim than the README made.
- **`WORKERS=1` makes the quota row impossible.** One uvicorn process is one
  runnable thread, and one thread cannot spend more than 100 ms of CPU in a
  100 ms period, so it can never exhaust a 1.0-CPU quota. Run this experiment
  at the harness default and `nr_throttled` is 0 by arithmetic. `WORKERS=4`
  is now the script's default, with the reason in the header.
- **The load rate has a narrow window.** The first honest run used `RATE=60`
  at `WORKERS=1`: every contended cell reported p50 ≈ 1.8–2.2 s regardless of
  which knob was set, because the api was at ρ≈1 and what the table showed
  was queue growth, not the knob. Too little load and the rows are identical;
  too much and they are identical for the opposite reason. Both failure modes
  are now in the README's *Broken, not merely surprising*.

**Fixed:** the script measured one config and told the reader to hand-edit
`docker-compose.yml` between runs — a 2×3 table that produced 2 cells. It now
drives all six through generated Compose overrides. Also added: a warm-up
loop replacing `sleep 4` (five workers calibrating on one CPU under
`cpuset: "0"` were still finishing when the measurement began, which is where
the earlier 2.2 s idle p99 came from), and a `hog`-is-actually-burning check.

**Documentation corrected.** The README promised "weight is invisible in the
idle column and **decisive** in the busy one". It is not: the `hog` is
`cpu_shares: 2` → `cpu.weight 1` against the api's 512 → `cpu.weight 59`, and
at 59-against-1 the api wins so completely that the busy column looks like
the idle one. That is the compose file's deliberate design, stated two
paragraphs earlier in the same README. The payoff paragraph now says what the
table actually shows.

---

## 7.2 — the headline. It did not reproduce, and then it did.

### The failure, recorded because it is the instructive part

First honest run of `./docker/run_7_2.sh` (`/mixed`, `RATE=40`, 4 workers,
`cpus: 1.0`, 30 s):

```
=== baseline: 4 workers, 1.0 CPU ===
  cpu.max enforced: 100000 100000
  p50 33.3 ms   p99 55.7 ms   dropped_iterations 0
  nr_periods 309   nr_throttled 0   throttled_usec 0
```

A clean run of an experiment that demonstrated nothing. The cause is the load
shape: `constant-arrival-rate` fires at **fixed intervals** — it is the least
bursty load a generator can emit. Against four single-threaded uvicorn
workers at 42% average CPU, four requests essentially never want CPU in the
same instant, instantaneous demand never exceeds the quota, and `nr_throttled`
stays 0 however long you run it. Average demand below quota plus zero variance
equals zero throttling: true, and a statement about a load shape no production
service has ever had.

`steady.js` now takes `BURST=N` — each arrival fires N requests in parallel
and the arrival rate drops to `RATE/N`, holding the average offered rate
constant. That is what real traffic does: a page fanning out ten parallel API
calls, a queue consumer waking with a batch, a cache stampede. Ten requests
needing ~150 ms of CPU cannot fit in a 100 ms bucket however few of them run
at a time.

### The headline, reproduced

`./docker/run_7_2.sh` with the defaults it now carries (`/mixed`, `RATE=20`,
`BURST=10`, 45 s per cell, `/cpu` pinned to 136 hash rounds for every cell):

| variant | throttle ratio | avg CPU | freeze per throttled period | p50 | p99 | completed |
|---|---|---|---|---|---|---|
| **baseline: 4 workers, 1.0 CPU** | **0.272** | **0.49 CPU = 49% of quota** | **66.8 ms** | 101.2 ms | 278.2 ms | 900 |
| fix 1: 1 worker, 1.0 CPU | 0.003 | 0.44 CPU = 44% | 0.0 ms | 167.1 ms | 260.0 ms | 910 |
| fix 2: 4 workers, 2.0 CPU | 0.000 | 0.51 CPU = 26% of quota | — | 90.8 ms | 245.4 ms | 910 |
| fix 3: 4 workers, 1.0 CPU, 50 ms periods | 0.258 | 0.52 CPU = 52% | **31.8 ms** | 134.1 ms | 450.0 ms | 910 |
| fix 4: 4 workers, 1.0 CPU, `cpu.max.burst=100000` | 0.000 | 0.50 CPU = 50% | — | **84.6 ms** | **219.0 ms** | 910 |

`nr_bursts` on the burst row: **66**, `burst_usec` **1768505**. Zero
everywhere else. `dropped_iterations` 0 in every cell. Throughput is within
1% across all five rows — the quota was never the throughput ceiling.

And the title, taken literally. Same baseline at `RATE=12 BURST=10`:

```
  nr_periods 332  nr_throttled 29  ratio 0.087
  avg CPU 0.27 = 27% of the 1.0 CPU quota
  frozen 40.7 ms per throttled period
  p50 101.2 ms   p99 360.8 ms
```

**27% average CPU, throttled in 8.7% of periods, p99 3.6× p50.** That is the
sentence this whole layer is built on, measured.

### `cpu_stat_watch.py`, live, from inside the container

```
time      cpu%  thr/int  ratio  frozen ms/int  nr_throttled  nr_periods  psi some avg10
03:41:47  53%   3/10     0.300  296            32            77          11.16
03:41:48  50%   4/10     0.400  322            36            87          11.16
03:41:51  38%   2/11     0.182  223            43            115         9.87
03:41:54  37%   1/9      0.111  34             48            143         9.71
03:41:57  67%   4/10     0.400  753            58            171         11.50
03:42:00  35%   1/8      0.125  134            65            199         12.31
  worst interval: 03:41:48  ratio 0.400  at 50% CPU
```

### Two results the README did not predict

- **Shortening the period made the tail worse, not better.** Fix 3 halved the
  freeze quantum exactly as advertised (66.8 ms → 31.8 ms) and *doubled* the
  number of freezes (`nr_periods` 408 → 632), leaving p99 at 450 ms against
  the baseline's 278 ms. A burst that needs more CPU than one full period can
  supply waits for the same number of refills either way and now pays a
  context switch at each one. The README now says to expect this and why.
- **The fix nobody can express won.** `cpu.max.burst`, which has no Compose
  key and no Kubernetes field, gave the best p50 *and* the best p99 of the
  five.

### Defects fixed in 7.2

1. `docker compose run` recreating the container under test (above).
2. **The throttle-ratio line never printed.** Written as
   `... | awk -v qmax="$(read_quota)" '...'`, and `read_quota` runs
   `docker compose exec -T`, which reads stdin — inside a pipeline that stdin
   is the `join` output awk was about to consume. The `exec` ate the entire
   `cpu.stat` delta and awk printed nothing, silently, in every cell. The one
   number the experiment exists to produce was missing and nothing errored.
3. `WORKERS=4 API_CPUS=1.0 recreate` set the variables for the `recreate`
   call and nothing else, so later compose commands in the same cell resolved
   the harness defaults. Now exported for the cell.
4. The k6 output grep still matched k6's built-in summary, which
   `handleSummary` had replaced.
5. `cpu_stat_watch.py` could not run in a container at all (`IndexError` from
   `parents[2]`; the documented `/app/cpu_stat_watch.py` was never in the
   image).
6. `quota_freeze.py` printed `quota actually enforced: 1.00 CPU` inside a
   container and then a table generated entirely by the userspace model, with
   no banner. A process cannot rewrite its own cgroup, so the 2.0-CPU,
   50 ms-period and burst rows could not be kernel-enforced from in there even
   in principle. It now says so on both platforms. Its in-container baseline —
   **37% average CPU, ratio 0.060, p50 42 ms, p99 109 ms, heartbeat gap
   78 ms** — is the same signature the service-level run produced.

---

## 7.3 — the six-runtime matrix, both columns

`./docker/run_7_3.sh` and `./docker/run_7_3.sh --only python,go`, both exit 0.
Each language in its own official image; the kernel's own reading printed
before any runtime is asked anything.

Column 1, `--cpus=1.5` → `cpu.max 150000 100000`, `cpuset.cpus.effective 0-3`:

| runtime (version observed) | host-shaped call | affinity-shaped call | quota-aware call |
|---|---|---|---|
| CPython 3.13.15 | `os.cpu_count()` **4** | `sched_getaffinity` **4**, `os.process_cpu_count()` **4** | none in the stdlib |
| node v24.19.0, libuv **1.52.1** | `os.cpus().length` **4** | — | `os.availableParallelism()` **1** |
| go1.25.13 | — | `runtime.NumCPU()` **4** | `GOMAXPROCS(0)` **2** |
| go1.25.13, `GODEBUG=containermaxprocs=0` | — | `NumCPU()` **4** | `GOMAXPROCS(0)` **4** |
| rustc std (linux/aarch64) | `/proc/cpuinfo` **4** | — | `available_parallelism()` **1** |
| gcc 14.4, C++17 | `hardware_concurrency()` **4**, `sysconf` **4** | `sched_getaffinity` **4** | none |
| Temurin 21.0.11 | — | — | `availableProcessors()` **2** |
| Temurin 21.0.11, `-XX:-UseContainerSupport` | — | — | `availableProcessors()` **4** |

Column 2, `--cpuset-cpus=0,1` → `cpu.max max 100000`,
`cpuset.cpus.effective 0-1`: Python's `sched_getaffinity` and
`process_cpu_count()` both drop to **2** while `os.cpu_count()` stays **4**;
Go's `GOMAXPROCS(0)` is **2** in both GODEBUG modes. The two columns move
different calls, which is the whole matrix.

Side readings the probes surfaced: Node's V8 old-space limit **2240 MiB**
derived from the cgroup; Java's `Runtime.maxMemory()` **1230 MiB** at the
default `MaxRAMPercentage 25.0`, with `ParallelGCThreads 2`,
`ConcGCThreads 1`, `CICompilerCount 2` all fanned out from
`availableProcessors()`.

**Fixed:** the script mounted only its own sub-topic directory, so the Python
probe died on `ModuleNotFoundError: No module named 'cgroup'` before printing
a row — `cgroup.py` lives in `00-harness/local/`, two directories up. Now
mounts the topic root. The Rust row also copied a host-built `target/`
containing Mach-O artifacts into a Linux container; it now copies only
`Cargo.toml` and `src/`.

---

## 7.4 — the sizing sweep

`python3 python/sizing_sweep.py --measure --duration 25s`, `cpus: 2.0`,
`/cpu` pinned to 141 hash rounds, `/db` at 120 req/s, `/cpu` at 200 req/s in
bursts of 10 (above capacity on purpose):

| workers | /db p99 | /db req/s | /cpu p99 | **/cpu req/s** | throttle | PG conns | RSS MiB | dropped db/cpu |
|---|---|---|---|---|---|---|---|---|
| 1 | 29 | 120 | 30860 | **89** | 0.000 | 11 | 43 | 0/138 |
| 2 | 28 | 120 | 25421 | **173** | 0.091 | 8 | 106 | 0/66 |
| 4 | 32 | 120 | 25212 | **143** | 0.519 | 17 | 179 | 0/100 |
| 8 | 36 | 120 | 18832 | **148** | 0.573 | 24 | 320 | 0/104 |

Throughput peaks at **2 workers = `floor(2.0)`** and *falls* beyond it, while
the throttle ratio climbs from 0.000 to 0.573 — extra workers buying no
capacity and a great deal of freezing, which is 7.2's claim arriving as a
capacity number. RSS rises 43 → 320 MiB and Postgres backends 11 → 24 across
the same sweep: two of the other three ceilings, on the same rows.

**Fixed, four things.** (1) `docker compose run` without `--no-deps`.
(2) Every k6 column came back `nan`, twice: first because the regexes matched
k6's built-in summary, which `handleSummary` had replaced, and then because
the replacements anchored on `^` without `re.MULTILINE`. A finished-looking
table of `nan`. (3) `argparse`'s `--cpu-rate` default was 60 while
`Spec.cpu_rate` said 200, and argparse wins — so the "offered above capacity"
comment described a load that was never sent. Both defaults now come from the
dataclass. (4) `/cpu` was offered *below* capacity, and an open-loop generator
below capacity completes exactly what it offers: the req/s column read `60`
for every worker count and the throughput-ceiling claim had nothing to show.

`/db req/s` still reads back the offered rate for every row, and now says so:
that leg is deliberately below capacity, so its story is in the connection
and p99 columns, not in throughput.

---

## 7.5 — the sync driver, and the two stalls

`python3 python/run_variants.py --quota 0.5 --duration 25s`, `/db` at
120 req/s, 1 worker, `pg_sleep 0.05s`, same container spec on every row:

| variant | p50 | p99 | req/s | throttle ratio | OS threads | tokens | dropped | diagnosis |
|---|---|---|---|---|---|---|---|---|
| 1 `async def` + psycopg2 | **4005 ms** | **32956 ms** | 26 | 0.001 | 5 | 40 | 1041 | A (event loop) |
| 2 `def` + psycopg2 (40 tokens) | 54 ms | 267 ms | 120 | 0.159 | 41 | 40 | 0 | B (cgroup) |
| 3 `def` + psycopg2 (100 tokens) | 60 ms | 2328 ms | 120 | **0.375** | 15 | 100 | 0 | B (cgroup) |
| 4 `async def` + asyncpg | 53 ms | **121 ms** | 120 | 0.008 | 5 | 40 | 0 | neither |

**The worst p99 and the highest throttle ratio are different rows.** Variant 1
destroys the tail with a throttle ratio of 0.001; variant 3 has the highest
ratio in the table and a tail an order of magnitude better. That is the
sub-topic's entire reason to exist, and it is now measured rather than
asserted. Variant 3 is also 7.2 applied: raising the anyio limiter from 40 to
100 tokens put more runnable threads in the same bucket and more than doubled
the throttle ratio.

At the harness's default `cpus: 1.0` the quota does not bite at this rate
(every ratio 0.000) and only the event-loop half shows; the script's own
sanity note says so, and it was right.

`--only 1,4 --rate 160` also ran: variant 1 at 31 req/s and p99 23340 ms
against variant 4 at 160 req/s and p99 171 ms.

**Fixed, four things.** (1) The override mounted `variants.py` at
`/app/variants.py` while the image's `WORKDIR` is `/srv` — uvicorn could not
import the module and the container never started. (2) No `--no-deps`, which
here means the variant under test is replaced before it is measured and all
four rows describe the same handler. (3) The k6 regexes matched the replaced
summary. (4) **The `which stall` column was not a measurement.** It assigned
"A (event loop)" to variants 1, 2 and 3 by variant number — which is the row's
own name written twice. Measured, variants 2 and 3 hand the blocking call to
the anyio pool and return p50 54–60 ms against a 50 ms `pg_sleep`, while
variant 1 blocks the loop and returns 4005 ms. Both halves of the diagnosis
are now derived from the run. The script also leaves no
`docker-compose.override.yml` behind — Compose auto-loads that file for every
command in the harness directory, so a stale copy silently re-points `api` for
every other experiment in the topic.

---

## 7.6 — memory

`./docker/run_7_6.sh` at `--memory=256m`, swap disabled.

| row | what happened | `.State.OOMKilled` | exit |
|---|---|---|---|
| python | reached 240 MiB, then `Killed`. No traceback, no `atexit`, no `finally` | true | **137** |
| node `--heap` (V8 sizing itself) | reached 240 MiB / heapUsed 244 MiB, then killed by the **kernel** | true | 137 |
| node `--heap --max-old-space-size=160` | `FATAL ERROR: Reached heap limit` **with a native stack trace** | false | **134** |
| node `--buffer` | `heapUsed` flat at 4 MiB, `external` to 242 MiB, then `Killed` | true | 137 |
| go (no GOMEMLIMIT) | reached 248 MiB, `memory.events max 6`, then killed | true | **137** |
| go `GOMEMLIMIT=230MiB` | reached 248 MiB, GC CPU 6.6% at 10 GCs, then killed | true | **137** |
| rust | reached 248 MiB, `memory.events max 54`, then `Killed` | true | 137 |
| cpp `--reserve-only` | **1024 MiB allocated, RSS 3 MiB**, `bad_alloc` caught 0 times, destructor and `atexit` both ran | false | 0 |
| cpp (touch every page) | `Killed` | true | 137 |
| java `--heap` | **`OutOfMemoryError` caught, with a stack trace**, then the shutdown hook ran at RSS 233 MiB | false | — |
| java `--direct -XX:MaxDirectMemorySize=2g` | `Killed` | true | 137 |

**The documented Node expectation was wrong, and the reason is worth more
than the row was.** V8 *does* derive its old-space limit from the cgroup —
and here it derived **259 MiB against a `memory.max` of 256 MiB**, i.e. 101%
of the container. So plain `--heap` is killed by the kernel at 137 before
V8's own ceiling is ever reached, and the promised "exit 134 with a stack
trace" cannot happen. It happens as soon as you set the ceiling *below* the
container's, which is what you should have been doing anyway. The script now
runs all three node cases and the summary says which is which.

**The Go rows were measuring the Go toolchain.** `go run oom.go` under
`--memory=256m` gets the *compiler* OOM-killed
(`/usr/local/go/pkg/tool/linux_arm64/compile: signal: killed`) and the program
never starts; and because `go run` outlives its child, the container exits **1**
rather than 137, so the evidence column pointed at the wrong process. The
binary is now built in an unconstrained container and only then run under the
limit — both Go rows now report `OOMKilled=true, exit 137`.

### `memory.high`, rewritten

`./docker/run_7_6.sh --high 200m` — `memory.high` 200 MiB under a
`memory.max` of 512 MiB, written into the cgroup by a privileged sidecar,
sampled for 45 s:

```
  before it allocates anything:
    memory.current  860160
    memory.high     209715200
    memory.max      536870912
    memory.events:  low 0  high 0  max 0  oom 0  oom_kill 0
    memory.pressure: some avg10=0.00   full avg10=0.00

  after 45s:
    memory.current  229306368        <- ABOVE memory.high
    memory.high     209715200
    memory.max      536870912        <- and well BELOW memory.max
    memory.events:  low 0  high 2114  max 0  oom 0  oom_kill 0
    memory.pressure: some avg10=79.86  full avg10=79.86
  still running? true   OOMKilled=false
```

That is the claim: a limit you can observe from a live process instead of
infer from a restart log. Three fixes were needed to get there.

- **The soft limit was never being written.** `numfmt` is GNU coreutils and is
  not on macOS; the `|| echo "$HIGH"` fallback handed the literal string
  `200m` to a file that takes a byte count, the sidecar write failed, and the
  run carried on measuring the original `memory.high`. Replaced with an `awk`
  conversion.
- **It was pointed at the wrong process.** The subject was the harness `api`,
  which sits at ~45 MiB whatever you throw at it, so `memory.high` was never
  approached: `high 0`, `memory.pressure` `0.00`, container survives. A
  container that survives because it never allocated is not evidence that
  `memory.high` works. It now drives `python/oom.py`, gated on a file so the
  soft limit is in place before the first page is charged.
- **The run does not end, and the container cannot be inspected from inside.**
  With swap disabled and an all-anonymous heap there is nothing reclaimable,
  so the kernel throttles the allocator instead; a first attempt sat at
  `memory.events high 37993`, `memory.pressure some avg10=86.53` and made
  almost no progress, and `docker exec` into that container **hangs
  indefinitely** — the cgroup cannot schedule a new process either. The demo
  is now bounded by a sampling window and reads every number from a host-side
  sidecar. Both halves are the result: `memory.high` degrades instead of
  killing, and the degradation is severe enough that you must observe it from
  outside.

Also fixed: the script mounted only its own sub-topic directory, so the Python
row died on `ModuleNotFoundError` before allocating a byte.

---

## 7.7 — free-threaded Python: one row of three

`./docker/run_7_7.sh` and `RATE=90 ./docker/run_7_7.sh`, both exit 0.

Row A — `python:3.14-slim`, 4 workers, `cpus: 2.0`, `RATE=90`:

```
  sys._is_gil_enabled() = True    (expected True for python:3.14-slim)
  enforced cpu.max: 200000 100000
  uvicorn workers: 5 forked
  /cpu  at 90 req/s : 1801 completed, p50 15.2 ms, p99 67.0 ms, dropped 0
  /db   at 120 req/s: 2401 completed, p50 22.2 ms, p99 27.8 ms, dropped 0
  PG backends: 9
  cpu.stat delta: nr_periods 420, nr_throttled 0, usage_usec 30287971
  RSS: 193.3MiB / 1GiB
```

Rows B and C are **still blocked**, and not by this machine:
`python:3.14t-slim` does not exist. `docker manifest inspect` returns
`failed to resolve source metadata`, and a tag search of `library/python` for
`3.14t`, `*t-slim` and `freethreaded` returns nothing at all. `python:3.14-slim`
ships a GIL-enabled interpreter only — `sysconfig.get_config_var("Py_GIL_DISABLED")`
is `0` and there is no `python3.14t` binary on the PATH.

**What they would need:** a free-threaded image. Either a derived Dockerfile
that builds CPython 3.14 with `--disable-gil`, or an image from a publisher
that ships one; then point `PYTHON_IMAGE` at it. Nothing else in the script or
the harness changes.

**Fixed:** on a missing image the script printed "an extension in
`requirements.txt` has no free-threaded wheel for this interpreter" — a
confident diagnosis of something that never happened, and exactly the sort of
plausible wrong answer that gets copied into a migration document. It now
checks the registry first and names the real cause. Both READMEs that told the
reader to use `python:3.14t-slim` now say it does not exist.

---

## Still blocked after this pass

| Item | Why | What it would need |
|---|---|---|
| 7.7 rows B and C (`python:3.14t-slim`, 4 workers and 1 worker) | Docker Hub's official `python` repository publishes no free-threaded tag; `python:3.14-slim` is GIL-enabled (`Py_GIL_DISABLED = 0`, no `python3.14t` binary) | A free-threaded image: CPython 3.14 built with `--disable-gil` in a derived Dockerfile, or one from a publisher that ships a free-threaded build. Then `PYTHON_IMAGE=<that image>` |
| `python3.14t` variants of `07/python/gil_check.py` | Same: no free-threaded interpreter exists on this host or in any image reachable from it | The same image as above, or a local 3.14t build |
| `java -XX:-UseContainerSupport` **on the macOS host** | Linux-only HotSpot flag; the macOS JDK aborts with `Unrecognized VM option` | Nothing — it is not meant to work there. It now RUNS in `eclipse-temurin:21`, which is the honest place for it |

Everything else that was BLOCKED on 2026-08-19 morning has run.

## What this pass says about the topic's central claim

The layer's headline — a service throttled while its average CPU looks
comfortable — reproduces on this machine, twice, by two independent routes:

- the service-level run, **0.272 throttle ratio at 49% of quota**, and at a
  lower rate **0.087 at 27% of quota** with p99 3.6× p50;
- the single-language model, **0.060 at 37%** with a 78 ms heartbeat gap.

Both are the same signature, and in both the fix that moves it most is one
that no orchestrator can express. It did not reproduce on the first attempt,
and the reason it did not is the most transferable thing in this file: the
load was perfectly evenly spaced. Average demand below quota plus zero
variance is zero throttling. Production has variance.

## The `Predict, then record` tables

Checked again, row by row, in all seven topic READMEs. Still **blank**. Every
number in this file is in this file, and none of it has been copied into the
reader's exercise.
