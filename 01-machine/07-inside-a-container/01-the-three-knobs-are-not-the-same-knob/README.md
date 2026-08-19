# 7.1 — The three knobs are not the same knob

### The takeaway (read this first)

**The one idea:** `cpu.weight` costs you nothing on an idle host.
`cpu.max` costs you exactly the same on an idle host as on a busy one.
They are different mechanisms wearing similar-looking numbers, and the
number in your Compose file or your Kubernetes manifest does not tell you
which one you got.

**Why it matters in practice:** "we gave it 2 CPUs" is ambiguous by
itself. `cpu_shares: 2048` and `cpus: "2.0"` are both *plausible* readings
of that sentence, and they produce opposite behaviour at 3am on an empty
box: one is invisible, the other freezes you. Kubernetes makes the same
distinction with `requests.cpu` and `limits.cpu`, and half the arguments
about whether to set CPU limits at all are really arguments about this
table.

**You'll know it landed when:** handed any container spec, you can say
which of the three knobs was actually written, predict whether the
container can be throttled on an *idle* host, and name the one file whose
contents would settle it in five seconds.

Namespaces and cgroups are also constantly confused with each other, and
that confusion is upstream of everything in this topic — so it is here
too, in *The concept*.

---

## The concept

**Namespaces hide things. cgroups limit things.** Your container's
isolated view of PIDs, mounts, hostname and network comes from namespaces.
Your resource limits come from cgroups — a completely separate kernel
mechanism that happens to be configured by the same `docker run` command.
This is why a process can be limited to half a CPU and still read `nproc`
and see 64. Nobody lied to it; nothing ever *told* it. `/proc/cpuinfo` and
`/proc/meminfo` are not namespaced, so unless someone bolted on lxcfs,
they report the host. Every "wrong CPU count" bug in this topic is
downstream of that one sentence.

Under cgroup v2 — the default on Amazon Linux 2023, Ubuntu 22.04+, RHEL 9+
and inside Docker Desktop's Linux VM — there are three CPU knobs:

| File | What it is | When it bites |
|---|---|---|
| `cpu.weight` (1–10000, default 100) | *Proportional share* under contention | Only when the CPU is actually contended. Never idles you |
| `cpu.max` = `"$QUOTA $PERIOD"` (default `max 100000`) | *Absolute ceiling.* Per `$PERIOD` µs of wall time you may consume `$QUOTA` µs of CPU time | Always. Even on a completely idle host |
| `cpuset.cpus` | *Which physical CPUs* you may run on | Always, but by narrowing, not by stopping |

How the tools you actually type map onto them:

| You write | Kernel gets |
|---|---|
| `docker run --cpu-shares 512` / Compose `cpu_shares: 512` | `cpu.weight` |
| `docker run --cpus=1.5` / Compose `cpus: "1.5"` | `cpu.max` = `150000 100000` |
| `docker run --cpuset-cpus=0,1` / Compose `cpuset: "0,1"` | `cpuset.cpus` |
| Kubernetes `resources.requests.cpu` | `cpu.weight` |
| Kubernetes `resources.limits.cpu` | `cpu.max` |

Derive the quota mapping yourself and it stops being something to
memorise: the period defaults to 100000 µs, and `--cpus=N` means "N CPUs'
worth of time per period", so `QUOTA = N × PERIOD`. `--cpus=1.5` →
`150000 100000`. `--cpus=0.5` → `50000 100000`. Read the file back and you
can invert it: `cat /sys/fs/cgroup/cpu.max` printing `150000 100000` means
someone asked for 1.5 CPUs.

The distinction that matters: **weight only costs you something when the
box is busy; quota costs you something whenever you are bursty, including
on an empty host.** A weight-limited container on an idle machine runs at
full speed — there is no one to lose a proportional fight to. A
quota-limited container on that same idle machine gets stopped dead at
every period boundary it exhausts, with the rest of the machine sitting
there unused. That is not a bug; it is what an absolute ceiling means.

`cpuset` is the third thing and the easiest to file wrongly. Pinning to one
CPU looks like a 1.0-CPU quota on a throughput chart, and produces
`nr_throttled = 0` forever, because narrowing *which* CPUs you may run on
is a different mechanism from *stopping* you at a period boundary. Two
configs with the same throughput, one of which shows throttling and one of
which structurally cannot, need different fixes — and the fix for the
second is never "raise the quota".

---

## How each language actually gets there

**Fewer than six here, on purpose: the mechanism is entirely the kernel's.
Docker writes three files, the kernel enforces them, and the observation
is a `cat` — six near-identical programs reading the same three paths
would teach nothing that one does not.** The experiment uses Python (the
harness's probe language) and a shell script; [7.3](../03-ask-three-runtimes-how-big-the-machine-is/README.md)
is where all six runtimes earn their place, because *there* the answer
genuinely differs per runtime.

There is one language-visible consequence worth naming before you get
there, because it explains why 7.3's matrix has two columns:

- Under **`cpuset.cpus`**, the *affinity mask* changes. Every runtime that
  sizes itself from affinity — Python's `sched_getaffinity`, Go's
  `NumCPU`, Rust's `available_parallelism`, Java's `availableProcessors`,
  C++'s `hardware_concurrency` on glibc — sees the smaller number and
  adjusts on its own. cpuset is the knob runtimes accidentally get right.
- Under **`cpu.max`**, nothing in the affinity mask changes at all. A
  runtime has to go read `/sys/fs/cgroup/cpu.max` deliberately, and most
  of the calls people reach for never do. Quota is the knob runtimes get
  wrong.

That asymmetry — not any property of the languages — is why quota is the
knob that produces mystery latency and cpuset is the one that just makes
things slower in an obvious way.

---

## The experiment

Run identical `/cpu` load against three configs of the *same* container,
twice each: once on an idle host, once while a second container burns
every core.

| Config | Compose | Should write |
|---|---|---|
| (a) share, no ceiling | `cpu_shares: 512`, no `cpus:` | `cpu.weight` |
| (b) quota | `cpus: "1.0"` | `cpu.max` = `100000 100000` |
| (c) pin | `cpuset: "0"` | `cpuset.cpus` = `0` |

The contended column comes from the harness's `hog` service
(`--profile contend`), which has `cpu_shares: 2` — a deliberately tiny
weight, so it loses every proportional fight and *still* costs config (b)
exactly nothing, which is the point.

Before any measurement, for every config, print what Docker actually
wrote:

```
cat /sys/fs/cgroup/cpu.weight /sys/fs/cgroup/cpu.max /sys/fs/cgroup/cpuset.cpus.effective
```

Do that first, always. Reading the enforced number before trusting the
measurement is the single habit this whole topic is trying to install.

The payoff is a 2×3 table: quota is the only row that can report a
throttled period at all, and it reports them on an **idle** host; cpuset
resembles quota on throughput but reports `nr_throttled = 0` because
`nr_periods` never advances without a quota to advance it against; weight
costs nothing in the idle column.

Read the weight row carefully rather than expecting it to collapse. The
`hog` is deliberately set to `cpu_shares: 2` — about `cpu.weight 1` once
Docker has translated it — against the api's 512, which lands near
`cpu.weight 59`. At 1-against-59 the api wins the proportional fight so
completely that the busy column looks almost like the idle one. That is
the demonstration, not a failed one: a weight only ever decides a *split*,
and a split you win 59:1 is not a limit. If you want to see weight bite,
raise `HOG_THREADS` and give the hog a weight comparable to the api's;
nothing else in the table changes.

## How to run

```bash
# the real thing -- needs a running Docker daemon
cd 01-machine/07-inside-a-container/01-the-three-knobs-are-not-the-same-knob
./docker/run_7_1.sh

# what it does, if you would rather drive it by hand, from ../00-harness:
docker compose up -d --force-recreate api        # after editing the knob
docker compose exec api cat /sys/fs/cgroup/cpu.max
docker compose --profile load run --rm --no-deps -e ENDPOINT=/cpu -e RATE=60 k6 run /scripts/steady.js
docker compose --profile contend up -d hog       # then re-run the k6 line
docker compose --profile contend stop hog

# --no-deps is not optional. k6 depends_on api, so without it `compose run`
# re-creates the api container -- with the DEFAULT limits -- immediately
# before the measurement, and the cgroup file you just cat-ed belonged to a
# container that no longer exists.

# WORKERS=4 for this experiment, not the harness default of 1: one uvicorn
# process is one runnable thread, and one thread cannot spend more than
# 100ms of CPU in a 100ms period, so it can never exhaust a 1.0-CPU quota.
# Config (b) with WORKERS=1 reports nr_throttled 0 by arithmetic.
```

**On macOS there is no host-side version of this.** Darwin has no
cgroupfs, so `cpu.weight`, `cpu.max` and `cpuset.cpus` do not exist to be
read or set. `run_7_1.sh` checks for the daemon and says so rather than
producing zeros. The fallback, clearly labelled as a userspace model and
not the kernel:

```bash
python3 python/three_knobs.py
```

Recreate, don't restart — `docker compose restart` reuses the old cgroup,
and you will measure the previous config while believing you changed it.

## Predict, then record

Commit to answers before running:

1. Which config has the worst p99 on an **idle** host?
2. Is `nr_throttled` nonzero under config (c)?
3. Config (a) has no ceiling at all. What limits its throughput in the
   contended column — and what does `cpu.weight` actually decide there?

| Config | idle p99 | contended p99 | nr_throttled/nr_periods | what `cat` showed |
|---|---|---|---|---|
| shares 512, no quota | | | | |
| cpus 1.0 | | | | |
| cpuset 0 | | | | |

**Broken, not merely surprising.** If `cat cpu.max` prints `max 100000`
under config (b), Compose never applied the limit — check `cpus:` (or
`deploy.resources.limits.cpus`) and that you *recreated* rather than
restarted the container; everything downstream of that reading is
meaningless. If all three rows are identical in both columns, your offered
load is too light to saturate anything — raise `RATE` until the no-ceiling
config is visibly working. Raise it too far and you get the opposite
failure, which looks like a result: every cell reports the same
multi-second p99 because even the *unlimited* config is saturated, and
what you are reading is queue growth, not the knob. `RATE × 0.015 s` is
the offered CPU demand; keep it below the no-ceiling config's capacity and
near the quota. Check `nr_periods` too: a container with no quota reports
`nr_periods 0` forever, so a weight or cpuset row showing nonzero
`nr_periods` means a quota is still set and you are measuring two knobs at
once.

## Answer before moving on

1. A container has `cpu_shares: 512` and no quota, alone on a 16-core
   host. Its p99 is terrible. Give two explanations that have nothing to
   do with either cgroup knob, and say which cgroup file you would read to
   rule the knobs out in one command.
2. Config (c) pins to one CPU and never reports a throttled period, yet
   its throughput matches config (b). Describe a workload where (b) and
   (c) would give *visibly different* latency distributions despite equal
   throughput — and say which you would rather deploy.
3. Kubernetes `requests.cpu` sets weight and `limits.cpu` sets quota. A
   well-known piece of advice is "set requests, never set CPU limits."
   Argue it from this table, then give the strongest counter-argument.
   (Hint: what does an unlimited container do to its neighbours' *weights*
   when the node fills up, and what does capacity planning look like when
   no workload has a ceiling?)
4. `cpu.max` defaults to `max 100000` — no quota, but a period is still
   configured. Why does the kernel keep a period at all when there is no
   quota to enforce against it?

## Next up

[7.2 — Throttled at 30% CPU](../02-throttled-at-30-percent-cpu/README.md).
You now know which knob stops you. Next is the arithmetic of *how* it
stops you, and why the stopping is invisible to every average-utilisation
dashboard ever built.
