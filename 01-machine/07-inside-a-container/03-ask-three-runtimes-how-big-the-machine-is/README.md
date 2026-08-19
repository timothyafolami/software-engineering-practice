# 7.3 — Ask six runtimes how big the machine is

*(The folder slug says "three". It is kept because code and scripts on
disk already reference the path; the experiment is six runtimes, and the
narrowing to three was a mistake from an earlier pass.)*

### The takeaway (read this first)

**The one idea:** the same container, at the same instant, will be told
different CPU counts by different runtimes — and most of the calls people
actually reach for report a number the kernel is not enforcing.

**Why it matters in practice:** every "how many workers / threads / pool
slots" default in your stack is computed from one of these calls. Get the
call wrong and you do not get an error; you get a plausible number that is
4× or 8× too large, and 7.2's failure mode for free. The most-copied line
in Python deployment guides, `workers = 2 * os.cpu_count() + 1`, gives you
**17 workers on a 2-CPU quota** — 17 is not a typo, it is what the formula
returns on an 8-core host, and nothing anywhere will warn you.

**You'll know it landed when:** for any runtime — including one not in
this lab — you can name which of its CPU-count calls tracks the *affinity
mask*, which tracks the *bandwidth quota*, which tracks neither, and you
reflexively print the enforced number next to whatever the runtime
claimed.

---

## The concept

There are three genuinely different questions hiding behind "how many
CPUs do I have", and runtimes answer whichever one their author had in
mind:

1. **How many logical CPUs does the machine have?** `/proc/cpuinfo`,
   `sysconf(_SC_NPROCESSORS_ONLN)`. Not namespaced, so inside a container
   this is the *host's* answer, always.
2. **Which CPUs am I allowed to run on?** `sched_getaffinity(2)`. This
   moves when `cpuset.cpus` is set, and only then.
3. **How much CPU time may I consume per period?** `/sys/fs/cgroup/cpu.max`.
   This moves when `--cpus` / `limits.cpu` is set — and it is *invisible*
   to both of the questions above.

Question 3 is the one that is usually enforced in production and the one
fewest APIs answer. That is the whole matrix. A runtime that answers (2)
looks container-aware — it is right under `cpuset`, wrong under quota, and
the wrongness is silent.

There is also a fourth question people forget to ask separately: **how
much memory may I use?** `/proc/meminfo` reports the host, `memory.max`
reports the truth, and the same split repeats — see
[7.6](../06-memory-the-limit-that-kills-you-without-a-traceback/README.md).

Ground truth, for every row of every table below:

```
cat /sys/fs/cgroup/cpu.max            # "150000 100000" -> 1.5 CPUs
cat /sys/fs/cgroup/cpuset.cpus.effective
nproc                                 # affinity-aware, quota-blind
```

---

## How each language actually gets there

All six. This is the topic where the contrast *is* the content.

**Python — nothing in the standard library reads your CPU quota.** This
is the load-bearing sentence for the production stack this lab is built
around. Three calls, three answers to a question you did not ask:

- `os.cpu_count()` → host logical CPUs. Ignores affinity, ignores quota.
- `len(os.sched_getaffinity(0))` → the affinity mask. Correct under
  `cpuset.cpus`, **blind to `cpu.max`**. Linux-only.
- `os.process_cpu_count()` (3.13+, the cross-platform replacement for the
  above, honours `PYTHON_CPU_COUNT` and `-X cpu_count`) → still the
  affinity mask. **Still blind to `cpu.max`.**

Under the overwhelmingly common case — `--cpus=2` on an 8-core host — all
three say 8. You must read `/sys/fs/cgroup/cpu.max` yourself, or take the
number from the same place you set the limit. The env-var route is the
more honest engineering: the deployment already knows the answer, so
making it say so out loud beats making the process guess.

**Node — mostly fixed, in two places, at two different times.**
`os.cpus().length` still returns host cores. `os.availableParallelism()`
(v18.14+) delegates to libuv's `uv_available_parallelism`, which since
libuv 1.49 factors in the cgroup CPU quota on Linux. The modern call is
right and the old one is wrong, which is a nasty trap in old code that
looks identical at review time. Print `process.versions.uv` in the probe
rather than trusting any version number in a README, including this one.
Memory is Node's better half: it passes the cgroup limit to V8 via
`uv_get_constrained_memory`, so the default old-space heap is
container-aware. And `UV_THREADPOOL_SIZE` (default 4) is a separate number
entirely, read once at process start — setting `process.env.UV_THREADPOOL_SIZE`
from inside your own code is usually too late.

**Go — fixed as of 1.25 (August 2025), and worth knowing precisely.**
`runtime.NumCPU()` reports logical CPUs constrained by the affinity mask —
question (2). `runtime.GOMAXPROCS(0)` is the number that decides how many
OS threads actually run your goroutines, and since 1.25 it defaults to the
minimum of logical CPUs, affinity, and the cgroup bandwidth limit
(`cpu.max` on v2, `cpu.cfs_quota_us`/`cpu.cfs_period_us` on v1), rounding
fractional limits **up**, never below 2 unless the machine has fewer, and
re-checking up to once a second so a live limit change is picked up.
`GODEBUG=containermaxprocs=0` restores the pre-1.25 behaviour;
`GODEBUG=updatemaxprocs=0` disables the periodic re-check; setting
`GOMAXPROCS` explicitly disables both. Before 1.25 the standard fix was
Uber's `automaxprocs` — that import in a service today is redundant, not
wrong. Note the asymmetry: Go fixed CPU and left memory alone. There is
still no cgroup-aware `GOMEMLIMIT` default; verify that on your own
toolchain before quoting it.

**Rust — the standard library got there first.**
`std::thread::available_parallelism()` returns a `NonZeroUsize` and
accounts for the affinity mask *and* the cgroup CPU bandwidth limit on
Linux, which is the only call in this entire matrix that answers question
(3) without you asking for it by name. Because tokio's `multi_thread`
worker count and rayon's pool both derive from it, an idiomatic Rust
service is quota-sized by accident. The older `num_cpus` crate is the trap:
`num_cpus::get()` is affinity/quota-aware on Linux but `get_physical()` is
a different question again (physical cores, ignoring SMT), and code that
picks one for the other is common.

**C++ — no answer at all, and permitted to say so.**
`std::thread::hardware_concurrency()` is a *hint*: the standard allows it
to return 0, it reports host logical CPUs on every mainstream
implementation, and nothing in the standard library knows what a cgroup
is. OpenMP's default team size has the same problem. So C++ is the one
that is reliably wrong — and, for exactly that reason, the one that shows
the mechanism most clearly, because the fix is to call
`sched_getaffinity(2)` and `open("/sys/fs/cgroup/cpu.max")` yourself, with
no runtime in the way, and see that this is all any of the others are
doing.

**Java — container-aware since long before it was fashionable.**
`Runtime.getRuntime().availableProcessors()` derives from the cgroup CPU
limit when `UseContainerSupport` is on, which it has been by default since
8u191 / JDK 10. `-XX:ActiveProcessorCount=N` overrides it;
`-XX:-UseContainerSupport` turns the awareness off, which is the flag the
probe uses to print the "before" answer the way Go's `GODEBUG` does. The
consequence is larger than the number: `ParallelGCThreads`, G1's
concurrent workers, JIT compiler threads and `ForkJoinPool.commonPool()`'s
parallelism all derive from that single call, so it is simultaneously the
most correct answer in the matrix and the one with the widest blast radius
if you override it wrongly. Memory has the parallel story:
`Runtime.maxMemory()` under `MaxRAMPercentage` reads `memory.max`.

---

## The experiment

Write one small probe per language that prints **every** available answer
and the ground truth side by side, then run all six in the *same* container
spec at `cpus: "1.5"` on a host (or Docker Desktop VM) with at least 4
CPUs.

Then run the whole matrix again under `cpuset: "0,1"` instead of a quota.
The point of the second column is that it is a *different* column: calls
that track affinity move, calls that track bandwidth do not, and the two
sets are not the same set.

Two cells are deliberately interesting:

- **Go's rounding at 1.5.** `GOMAXPROCS` rounds fractional limits up, so
  the runtime intentionally runs slightly more threads than the quota can
  keep continuously busy. Predict that cell before you look.
- **Python's newest API.** `os.process_cpu_count()` is the modern,
  cross-platform, obviously-correct-looking call. Predict whether it
  helps under `cpus: "1.5"` before you run it.

## How to run

```bash
cd 01-machine/07-inside-a-container/03-ask-three-runtimes-how-big-the-machine-is

# all six, in one container spec, both columns
./docker/run_7_3.sh
./docker/run_7_3.sh --only python,go      # a subset
CPUS=2.5 ./docker/run_7_3.sh              # a different quota

# individually, inside a Linux container with the toolchain present
python3 python/cpuinfo.py
node nodejs/cpuinfo.js
(cd golang && go run cpuinfo.go)
(cd golang && GODEBUG=containermaxprocs=0 go run cpuinfo.go)   # the pre-1.25 answer
(cd rust/cpuinfo && cargo run --release)
g++ -O2 -std=c++17 -pthread -o /tmp/cpuinfo cpp/cpuinfo.cpp && /tmp/cpuinfo
(cd java && javac CpuInfo.java -d /tmp/javabuild && java -cp /tmp/javabuild CpuInfo)
java -XX:-UseContainerSupport -cp /tmp/javabuild CpuInfo   # the "before" answer
#   ^ Linux-only flag. On a macOS JDK it aborts with "Unrecognized VM option";
#     the probe prints UseContainerSupport as n/a there for the same reason.
java -XX:ActiveProcessorCount=2 -cp /tmp/javabuild CpuInfo # the override

# one probe under one knob, if you only want a single cell:
docker run --rm --cpus=1.5        -v "$PWD:/w" -w /w python:3.13-slim python python/cpuinfo.py
docker run --rm --cpuset-cpus=0,1 -v "$PWD:/w" -w /w python:3.13-slim python python/cpuinfo.py
```

**Run these inside a Linux container, not on the Mac host.** On macOS every
cgroup reading is `None`/absent — correctly, since Darwin has no cgroupfs —
so the matrix collapses to a single column and there is nothing to compare.
The probes should print `n/a` there rather than a zero, and say why.

## Predict, then record

Predict every cell before running. The prediction is the exercise.

| Call | under `cpus: "1.5"` | under `cpuset: "0,1"` | ground truth |
|---|---|---|---|
| py `os.cpu_count()` | | | |
| py `os.process_cpu_count()` | | | |
| py `len(sched_getaffinity(0))` | | | |
| node `os.cpus().length` | | | |
| node `os.availableParallelism()` | | | |
| go `runtime.NumCPU()` | | | |
| go `GOMAXPROCS(0)` | | | |
| go, `containermaxprocs=0` | | | |
| rust `available_parallelism()` | | | |
| c++ `hardware_concurrency()` | | | |
| c++ `sched_getaffinity` count | | | |
| java `availableProcessors()` | | | |
| java, `-XX:-UseContainerSupport` | | | |

**Broken, not merely surprising.** If `os.cpu_count()` already equals your
quota, you are not in a container — or, on macOS, Docker Desktop's Linux VM
genuinely has that few CPUs, in which case raise it to 4+ or the whole
effect vanishes. If Go's `GOMAXPROCS(0)` matches `NumCPU()` under a quota,
check the toolchain is ≥1.25 and that nothing in the image sets
`GOMAXPROCS`. If Java's two rows are identical, `UseContainerSupport` was
already off (or on) in both — print the flag's value, don't infer it. If
every language agrees under `cpuset` but disagrees under `cpus`, that is
not a bug: it is the result.

## Answer before moving on

1. Sort the thirteen calls into the three questions from *The concept*.
   Which question does each answer — and which calls change their answer
   depending on the *kernel version* or *runtime version* rather than the
   container spec?
2. `available_parallelism()` and `availableProcessors()` both read the
   quota, so both must round a fractional limit somewhere. Argue for
   rounding 1.5 CPUs **up** to 2 and then argue for rounding it **down**
   to 1, in terms of what 7.2 measured. Which would you want as a
   language default, and does your answer change for a batch job versus a
   latency-sensitive service?
3. Python will not fix this in the standard library. Given that, which is
   the better engineering — a 20-line `read_quota()` helper in your app, or
   a `WEB_CONCURRENCY` environment variable set by whatever also sets the
   CPU limit? Give the failure mode of each.
4. A library you depend on sizes its internal thread pool from
   `os.cpu_count()` at import time. You cannot patch it. Name two ways to
   make it see the right number from outside the process, and say what
   each costs. (Hint: one of them is a knob from 7.1.)

## Next up

[7.4 — Sizing a Python web service in a container, properly](../04-sizing-a-python-web-service-in-a-container/README.md).
You can now read the enforced numbers. Next: the three separate ceilings
that turn one of those numbers into a worker count, and why the one you
are tuning is usually not the one that binds.
