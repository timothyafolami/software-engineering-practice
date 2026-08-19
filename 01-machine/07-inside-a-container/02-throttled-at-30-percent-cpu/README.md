# 7.2 — Throttled at 30% CPU (the headline)

### The takeaway (read this first)

**The one idea:** the throttling ratio, not average utilization, is the
metric that explains your p99 — and the two routinely point in opposite
directions. A container can sit at 30% average CPU and still be frozen
solid for tens of milliseconds, dozens of times a second.

**Why it matters in practice:** this is one of the most common and most
misdiagnosed latency causes in containerised services. The service shows
no errors, passes every health check, reports comfortable average CPU, and
serves a p99 many times its p50. Every instinct says "the database is
slow" or "add more workers" — and adding more workers is frequently the
thing that *caused* it. If you have a slow Dockerised service right now,
this is the first place to look: before the query plan, before the N+1,
before the index.

**You'll know it landed when:** you can look at a p99 that is
suspiciously close to a round multiple of 100ms and know what that
fingerprint means, name the one file that confirms or kills the hypothesis
in ten seconds, and explain why cutting the worker count can improve
latency without costing any throughput at all.

---

## The concept

**Quota is enforced by stopping you, and the enforcement is bursty by
construction.** The kernel refills a global bucket with `$QUOTA` µs every
`$PERIOD` (100000 µs by default). Runnable threads in the cgroup pull from
that bucket in per-CPU slices of 5ms
(`kernel.sched_cfs_bandwidth_slice_us`). When the bucket is empty, every
task in the cgroup is dequeued until the next period boundary — including
the threads that were using no CPU at all.

Sit with the arithmetic, because it is the entire lesson. `--cpus=1` means
100ms of CPU time per 100ms of wall time.

- **One runnable thread:** it consumes 100ms of quota over 100ms of wall
  clock. That is "one core, continuously," and you never notice a thing.
- **Eight runnable threads** — four uvicorn workers, a GC thread, a
  metrics thread, whatever — can drain the same 100ms of quota in roughly
  `100ms ÷ 8 = 12.5ms` of wall clock. The container is then frozen for the
  remaining **87.5ms**.

Averaged over the minute, both cases used 100% of their quota. A dashboard
reports exactly the CPU you asked for, calmly, in both. But a request that
arrived at the wrong moment in the second case took an 87ms penalty for
existing, and nothing in the average shows it.

That is why the symptom is **tail latency at low average utilization**,
and why the freeze is quantised: your worst-case added latency cannot
exceed one period, so p99 clusters near a multiple of the period length.
The period is the fingerprint.

Note the direction of the effect, because it is counter-intuitive: at a
fixed quota, *more threads make the freezes worse, not better*. Threads do
not get you more CPU time — the bucket size is fixed. They only change how
fast you spend it, and therefore how long each freeze lasts.

### Two footnotes for getting this right in 2026

**Pre-5.4 kernels had a real bug stacked on top of this.** Per-CPU slices
*expired*, so a lightly-threaded app on a big box got throttled nowhere
near its aggregate quota. Dave Chiluk's fix (`de53fd7aedb1`) removed slice
expiration in Linux 5.4. Any post written before 2020 telling you
throttling means "the kernel is broken" is describing a bug you no longer
have; everything above is the design working as specified.

**Linux 6.6 replaced CFS's task-picking with EEVDF, but not bandwidth
control.** The `cfs_bandwidth` machinery is the same code, which is why
the files and the Prometheus metrics still say "cfs". Anyone who tells you
throttling is obsolete because we run EEVDF now has confused the picker
with the accountant.

### Where to look, always

`/sys/fs/cgroup/cpu.stat`, from inside the container:

```
usage_usec     <accumulated CPU time, µs>
nr_periods     <100ms periods elapsed>
nr_throttled   <periods in which we were frozen>   <-- this one
throttled_usec <total time spent frozen>
nr_bursts      <periods that drew on banked burst>
burst_usec     <banked time consumed>
```

`nr_throttled / nr_periods` is your throttling ratio. Anything above ~0.05
is worth explaining; above ~0.10 it is almost certainly your latency
story. (Those two thresholds are rules of thumb for where to spend
attention, not kernel constants — the number that matters is whether your
p99 moves when the ratio does.)

The Prometheus/cAdvisor equivalents are
`container_cpu_cfs_throttled_periods_total`,
`container_cpu_cfs_periods_total` and
`container_cpu_cfs_throttled_seconds_total`. Use the *period ratio* for
"how often" and the *seconds* for "how bad" — and know that the seconds
figure sums across tasks, so it can legitimately exceed wall-clock time.

`cpu.pressure` (PSI) is the other file worth knowing: it reports the share
of time tasks were stalled waiting for CPU, catching both throttling *and*
plain host contention. If you add one panel to a dashboard after this
topic, make it the throttling ratio. If you add two, make the second PSI.

---

## How each language actually gets there

All six, because the runtime is exactly the subject: the quota is fixed,
so the only variable is **how many runnable threads that runtime puts into
one cgroup by default** — and every runtime here answers differently, for
reasons that are about its concurrency model rather than its syntax.

**Python — the threads come from *processes*, and the cgroup does not
care.** CPython's GIL means one thread per process executes bytecode at a
time, so the naive expectation is "one runnable thread, no problem". The
trap is that you get parallelism in Python by running `uvicorn --workers
N`, and **the cgroup is per-container, not per-process**: four workers are
four runnable threads pulling from the same bucket. On top of that, the C
libraries that matter (`hashlib`, OpenSSL, compression, numpy) release the
GIL around their work, so those threads genuinely occupy cores. Python is
also the one language here where the measurement itself needs care: under
a GIL, wall time and CPU time diverge inside a single thread, so the
experiment charges the budget with `time.thread_time()` rather than a
stopwatch. Bill wall time and you charge a thread for time it spent
waiting to run, and the whole result inverts.

**Node — "single-threaded" is a statement about your JavaScript, not
about the process.** One JS thread, yes; but libuv's pool
(`UV_THREADPOOL_SIZE`, default 4) runs fs, DNS and crypto, and V8 adds
background compilation and concurrent-marking GC threads sized from what
it believes the machine to be. A Node process at rest routinely has ~10
OS threads. So the runtime with the strongest "I only use one core"
reputation drains a 1-CPU bucket faster than its reputation implies, and
the fix is not a worker count — it is `UV_THREADPOOL_SIZE`, which is read
once at process start and therefore has to be an environment variable, not
a line in your code.

**Go — the runtime that sizes itself, and the version where it started
sizing itself from the right number.** The scheduler runs `GOMAXPROCS` OS
threads, plus dedicated GC mark workers (a fraction of `GOMAXPROCS`) and a
`sysmon` thread. Pre-1.25, `GOMAXPROCS` defaulted to the *host* CPU count,
so a 1.0-CPU container on an 8-core host ran eight threads flat out and
drained its bucket in ~12.5ms — the textbook version of the failure. Go
1.25 (August 2025) made the default the minimum of logical CPUs, the
affinity mask, and the cgroup bandwidth limit, rounding fractional limits
**up**, never below 2 unless the machine has fewer, re-checked about once
a second. The rounding-up is worth noticing: at `cpus: "1.5"` Go
deliberately runs 2 threads against 1.5 CPUs of quota, so it is still
possible to be throttled by the fixed default. `GODEBUG=containermaxprocs=0`
gives you the old behaviour on demand, which is how the experiment
produces both rows from one binary.

**Rust — nothing is implicit, and the standard library reads the quota.**
`std::thread::available_parallelism()` accounts for the cgroup CPU
bandwidth limit on Linux, which means the ecosystem functions that size
themselves from it — tokio's `multi_thread` worker count, rayon's pool —
land on the quota rather than the host by default. That makes Rust the
useful control in this experiment: the same workload, a runtime that is
*already* right, so any throttling you see is your own thread count and
not a bad default. The gap is elsewhere and worth knowing: tokio's
**blocking** pool defaults to 512 threads, so `spawn_blocking` under load
can put far more runnable threads in the cgroup than the worker pool ever
would.

**C++ — the same hazard with nothing to protect you, and the only one
talking to the kernel directly.** `std::thread::hardware_concurrency()` is
host CPUs, is permitted to return 0, and knows nothing about cgroups.
OpenMP defaults its team size to host CPUs too. So a C++ thread pool built
the obvious way is maximally wrong under quota — and being a fast compiled
language buys you exactly nothing here, because the bucket is drained in
CPU-seconds, not instructions. C++ is also the version to read for the
mechanism, because it is the one that just calls `sched_getaffinity` and
`open("/sys/fs/cgroup/cpu.max")` with no runtime in between: no layer to
blame, no default to unpick.

**Java — container-aware for longer than anyone else, and still the
heaviest thread footprint.** `UseContainerSupport` (on by default since
8u191/JDK 10) makes `Runtime.availableProcessors()` derive from the cgroup
CPU limit, so the JVM got this right years before Go did, and
`-XX:ActiveProcessorCount` lets you override it. But *everything* in the
JVM sizes itself from that one number — `ParallelGCThreads`, the G1
concurrent workers, C1/C2 JIT compiler threads,
`ForkJoinPool.commonPool()`, and any `newFixedThreadPool` you wrote — so
if it is wrong, it is wrong everywhere at once. A JVM also has more
always-on background threads than any other runtime here, meaning it can
drain a small bucket while your application code is a single request
handler doing nothing clever at all. Virtual threads (Java 21) change the
count of *application* threads dramatically but not the carrier pool, and
the carriers are what spend quota.

The through-line: **container-awareness of CPU count is a per-runtime,
per-version property, not a language property**, and the ones that get it
right get it right by reading the same file you are about to read by hand.

---

## The experiment

Pin the API to `cpus: "1.0"`, run it with **4** uvicorn workers, and drive
`/mixed` at an average rate low enough that `docker stats` shows roughly
30–40% CPU — **delivered in clumps, not evenly spaced**. Sample `cpu.stat`
every second throughout.

The clumping is not a detail, it is the mechanism, and getting it wrong is
the difference between this experiment working and producing a beautifully
clean `nr_throttled: 0`. `constant-arrival-rate` fires at fixed intervals;
it is the *least* bursty load a generator can emit. Against four
single-threaded uvicorn workers, evenly spaced arrivals at 30% average CPU
essentially never put four requests on the CPU at the same instant, so
instantaneous demand never exceeds the quota and nothing is ever throttled
no matter how long you run it. Averaged demand below quota plus zero
variance equals zero throttling — which is a true statement about a load
shape no production service has ever had.

Real traffic clumps: a page that fans out ten parallel API calls, a queue
consumer that wakes with a batch, a cache stampede, a retry storm. So
`steady.js` takes `BURST=N`: each arrival fires N requests in parallel and
the arrival rate drops to `RATE/N`, holding the average offered rate
constant. `RATE=20 BURST=10` is 2 clumps of 10 per second — the same ~0.3
of a CPU on a 15ms handler, but ten requests needing ~150ms of CPU cannot
fit inside a 100ms bucket however few of them run at once. That is the
whole trick, and it is also the answer to "our average CPU is fine, why
are we throttled".

You are looking for the signature: modest average CPU,
`nr_throttled/nr_periods` well above zero, p50 fine, p99 destroyed, and a
p99 suspiciously close to a multiple of 100ms.

Then fix it, **one variable at a time**, re-measuring each:

1. **Drop to 1 worker** at the same quota. Fewer runnable threads drain
   the bucket more slowly; the throughput ceiling is unchanged, because
   the ceiling was always the quota.
2. **Raise the quota** to `cpus: "2.0"` at 4 workers.
3. **Shorten the period instead:** write `cpu.max` = `50000 50000` —
   50000 µs of quota per 50000 µs period is the same 1.0 CPU on average,
   in 50ms periods, so the same throughput arrives with a freeze quantum
   half as long. Compose cannot express this; write the cgroup file
   directly. (Check the arithmetic yourself every time you touch this
   file: the format is `QUOTA PERIOD`, so `100000 50000` would be *two*
   CPUs, not one.)

   Expect this one to halve the *freeze*, not the *tail*, and be ready for
   it to make the tail worse. A shorter period cuts how long each freeze
   lasts and doubles how many of them there are; it only helps when the
   burst you are absorbing is smaller than the quota, so that finer
   granularity lets it through sooner. A burst that needs more CPU than a
   full period can supply has to wait for the same total number of refills
   either way, and now pays a context switch at each one.
4. **Grant burst:** `cpu.max.burst` (Linux 5.14+, default 0, capped at
   `$QUOTA`) banks unused quota to absorb spikes; watch `nr_bursts` and
   `burst_usec` stop being zero. Neither Docker nor Kubernetes sets this
   by default, which is why almost nobody knows it exists.

The single-language versions (`python/quota_freeze.py` and its five
siblings) reproduce the same failure without a web service in the way: a
workload needing ~0.35 of a CPU, spread across 1 then N threads under a
1.0-CPU quota, plus a heartbeat thread that uses almost no CPU and gets
frozen anyway. The heartbeat's largest gap is the number to watch — it
lands near a multiple of the period length.

## How to run

```bash
cd 01-machine/07-inside-a-container/02-throttled-at-30-percent-cpu

# the baseline and all four fixes, end to end, against the harness
./docker/run_7_2.sh

# or drive the service-level version by hand, from ../00-harness
cd ../00-harness
WORKERS=4 API_CPUS=1.0 CPU_ROUNDS=136 docker compose up -d --force-recreate api
./observe/watch.sh api &
docker compose --profile load run --rm --no-deps \
  -e ENDPOINT=/mixed -e RATE=20 -e BURST=10 k6 run /scripts/steady.js

# --no-deps is not optional: k6 depends_on api, so without it `compose run`
# re-creates the container you are measuring, with the default limits,
# immediately before the run.
#
# CPU_ROUNDS pins /cpu's cost. main.py calibrates it at startup, per worker,
# and four workers calibrating at once inside a 1.0-CPU cgroup each measure a
# cost inflated by the other three and settle on a CHEAPER handler than one
# worker does -- so an unpinned "4 workers vs 1 worker" comparison is two
# different tests. Read the value once from /stat, then hold it fixed:
#   curl -s localhost:8000/stat | python3 -c 'import json,sys;print(json.load(sys.stdin)["cpu_rounds"])'

# the two knobs Compose cannot express (period length, burst)
../02-throttled-at-30-percent-cpu/docker/write_cgroup.sh api "50000 50000"
../02-throttled-at-30-percent-cpu/docker/write_cgroup.sh api --burst 100000

# the per-language versions, each inside a Linux container.
# Subshells, so every line starts from the topic directory again.
cd ../02-throttled-at-30-percent-cpu
python3 python/quota_freeze.py
node nodejs/quota_freeze.js
(cd golang && go run quota_freeze.go)
(cd golang && GODEBUG=containermaxprocs=0 go run quota_freeze.go)   # pre-1.25
(cd rust/quota_freeze && cargo run --release)
g++ -O2 -std=c++17 -pthread -o /tmp/quota_freeze cpp/quota_freeze.cpp && /tmp/quota_freeze
(cd java && javac QuotaFreeze.java -d /tmp/javabuild && java -cp /tmp/javabuild QuotaFreeze)
java -XX:ActiveProcessorCount=1 -cp /tmp/javabuild QuotaFreeze   # the fix, from outside

# watch the ground truth alongside any of them, from inside the container.
# The harness image is built from 00-harness/ and cannot COPY a file out of
# this directory, so hand it in:
(cd ../00-harness \
 && docker compose cp ../02-throttled-at-30-percent-cpu/python/cpu_stat_watch.py api:/srv/ \
 && docker compose exec api python3 /srv/cpu_stat_watch.py --interval 1 --seconds 60)
```

**All of this must run inside a Linux container.** There is no
`/sys/fs/cgroup` on macOS: no quota to set, no `cpu.stat` to read, no
throttling to observe. Run the language versions inside the harness image
(or any Linux image with the toolchain) under `--cpus=1.0`, not on the
Mac host. The Darwin fallback is `../00-harness/local/cfs_sim.py`, which
applies the same accounting rule to real threads in userspace and prints a
FALLBACK banner; it reproduces the *signature*, and none of its rows
belong in a table next to a real `cpu.stat` reading.

## Predict, then record

Commit first to: which of the four fixes gives the biggest p99
improvement, and whether fix (1) reduces throughput.

| Variant | avg CPU | throttle ratio | p50 | p99 | req/s completed |
|---|---|---|---|---|---|
| 4 workers, 1.0 CPU (baseline) | | | | | |
| 1 worker, 1.0 CPU | | | | | |
| 4 workers, 2.0 CPU | | | | | |
| 4 workers, 1.0 CPU, 50ms period | | | | | |
| 4 workers, 1.0 CPU, + burst | | | | | |

And the per-language version, same quota, same offered work:

| Runtime | default threads it created | throttle ratio | heartbeat max gap |
|---|---|---|---|
| Python (4 workers) | | | |
| Node | | | |
| Go (default) | | | |
| Go (`GODEBUG=containermaxprocs=0`) | | | |
| Rust (tokio multi_thread) | | | |
| C++ (`hardware_concurrency()` pool) | | | |
| Java (default) | | | |

**Broken, not merely surprising.** `nr_throttled` stuck at 0 while p99 is
bad means you are not measuring throttling at all — confirm `cpu.max` is
set, check that your load is actually bursty (`BURST=1` at a modest rate
will give you `nr_throttled: 0` forever, correctly), then check k6's
`dropped_iterations` (if it is high, your load generator is the bottleneck
and you are measuring k6's own container). `nr_throttled: 0` combined with
`nr_periods: 0` is a different fault entirely: a cgroup with no quota never
advances `nr_periods`, so that pair means `cpu.max` reads `max` and no
ceiling was applied. A
throttle ratio near 1.0 with a *good* p99 means the quota is so small that
k6 never got enough arrivals in flight to matter; raise the rate. If p99
is identical at 1 and 4 workers, verify the workers actually started —
`docker compose exec api ps aux | grep uvicorn`. If the heartbeat's max
gap is not close to a multiple of the period, you are looking at something
other than throttling (GC pause, host contention, a swapping VM) —
`cpu.pressure` will tell you which.

## Answer before moving on

1. A service is at 25% average CPU with a 4-CPU quota, and p99 is 8× p50.
   Someone proposes doubling the CPU limit. Give the specific number you
   would read first, and describe a plausible state of the world in which
   doubling the limit makes p99 **worse**.
2. You halve the CFS period (`cpu.max` `200000 100000` → `100000 50000`).
   Average throughput is unchanged and tail latency improves. Explain the
   mechanism in two sentences — then name what you gave up, because this
   is not free.
3. A container runs one process with 4 threads under `cpus: "2.0"`, and
   `nr_throttled/nr_periods` is 0.30 while `usage_usec` shows it consumed
   only 60% of its quota. Pre-2020 you could blame a kernel bug. On a
   modern kernel, what is the explanation — and what does the *thread
   count* have to do with a service that is mostly waiting on Postgres?
4. `throttled_usec` can exceed wall-clock time for the same window.
   Explain why that is arithmetically correct rather than a bug, and say
   which of the two Prometheus metrics you would alert on and why.

## Next up

[7.3 — Ask six runtimes how big the machine is](../03-ask-three-runtimes-how-big-the-machine-is/README.md).
You have now seen that thread count is the variable. Next: where each
runtime *gets* that thread count from, and which of them consults the
number the kernel actually enforces.
