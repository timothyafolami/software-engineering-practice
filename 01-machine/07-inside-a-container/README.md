# Layer 1 · Topic 7 — The machine inside a container

A container does not give your process a smaller machine. It gives it the
*whole* machine, plus a kernel accountant that periodically stops it dead.
Every runtime you use sizes itself from the machine it can see, the
accountant enforces a number the runtime never read, and the gap between
those two numbers is where your p99 lives.

Topics 1–6 taught you what the machine does. This one is about the fact
that in production you have never once run on that machine — you have run
on a *quota-shaped shadow* of it, and almost every default your runtime
picked was picked for the machine, not the shadow.

Seven sub-topics, one shared harness.

| # | Sub-topic | Languages |
|---|---|---|
| — | [Shared harness](00-harness/README.md) — the stack all seven drive | Python, k6, shell |
| 1 | [The three knobs are not the same knob](01-the-three-knobs-are-not-the-same-knob/README.md) — weight vs quota vs cpuset | Python, shell |
| 2 | [Throttled at 30% CPU](02-throttled-at-30-percent-cpu/README.md) — the headline failure, and four fixes | all six |
| 3 | [Ask six runtimes how big the machine is](03-ask-three-runtimes-how-big-the-machine-is/README.md) — the CPU-count matrix | all six |
| 4 | [Sizing a Python web service, properly](04-sizing-a-python-web-service-in-a-container/README.md) — four ceilings, one worker count | Python |
| 5 | [The sync driver inside the async endpoint](05-the-sync-driver-inside-the-async-endpoint/README.md) — under a quota | Python |
| 6 | [Memory: the limit that kills you without a traceback](06-memory-the-limit-that-kills-you-without-a-traceback/README.md) | all six |
| 7 | [Free-threaded Python, honestly, in 2026](07-free-threaded-python-honestly-in-2026/README.md) | Python |

## Everything here runs inside Linux containers

cgroups and CFS bandwidth control are Linux kernel features. **macOS has no
`/sys/fs/cgroup`** — `cpu.weight`, `cpu.max`, `cpuset.cpus`, `memory.max`
and `cpu.stat` do not exist on Darwin to be read or written, and there is
no host-side substitute. On this machine (macOS 27, arm64) every experiment
runs inside Docker Desktop's linuxkit VM, where those files are real.

Two consequences worth fixing before you start: give that VM at least 4
CPUs in Docker Desktop's settings, or the host-vs-quota gap this whole
topic is about will be too small to see; and remember that the "host" your
runtime reports is the VM, not your Mac. Confirm both with
`docker info | grep -i "cgroup\|CPUs"`.

The Darwin fallbacks in [`00-harness/local/`](00-harness/README.md) model
the same accounting rule in userspace and print a FALLBACK banner. They are
not the kernel, and no row they produce belongs next to a real `cpu.stat`.

## Why the language set varies by sub-topic

Six languages is the lab default — Python, Node.js, Go, Rust, C++, Java —
but the [repo's policy](../../README.md) is to pick the ones that make the
mechanism visible and say so when using fewer.

Where the **runtime** is the subject, all six earn their place: 7.2, 7.3
and 7.6 are exactly the topics where each runtime's answer to "how big is
this machine, and what happens when I exceed it" genuinely differs, and
that contrast is the content. Where the mechanism lives **outside** the
language — 7.1's three cgroup files, 7.4's uvicorn/SQLAlchemy/Postgres
arithmetic, 7.5's Starlette thread limiter, 7.7's CPython-only GIL — fewer
is correct, and each of those pages states its one-line reason up front.

## The "you own this" test

> You can be handed any container spec — `cpus: "2"`, 4 workers, a pool
> size of 10 — and say, before running anything, roughly what its
> throughput ceiling is, which of the three CPU knobs is actually binding,
> what its worst-case Postgres connection count is, and which single
> number in `/sys/fs/cgroup/cpu.stat` would confirm or kill your
> hypothesis in ten seconds.

Every sub-topic is built the same way: read the concept, commit to a
prediction, run it, and record what actually happened. The record tables
ship blank on purpose.

## Sources

The roadmap's Layer 1 books — OSTEP and CS:APP — are still right for
Topics 1–6, but both predate cgroup v2 and have nothing on containers. For
this topic, read primary sources:

- [cgroup v2 kernel docs](https://docs.kernel.org/admin-guide/cgroup-v2.html)
  — authoritative for every file named in these pages.
- [CFS Bandwidth Control](https://docs.kernel.org/scheduler/sched-bwc.html)
  — quota, period, slices and burst, from the source.
- Dave Chiluk, [*Unthrottled: How a Valid Fix Becomes a Regression*](https://engineering.indeedblog.com/blog/2019/12/cpu-throttling-regression-fix/)
  — the best incident writeup on this failure mode, and the origin of the
  Linux 5.4 fix. Read it for the debugging method.
- [Container-aware GOMAXPROCS](https://go.dev/blog/container-aware-gomaxprocs)
  plus the [`runtime` docs](https://pkg.go.dev/runtime) for exact semantics.
- [Python free-threading HOWTO](https://docs.python.org/3/howto/free-threading-python.html)
  — the honest official caveat list, better than any blog post on it.
- [FastAPI deployment docs](https://fastapi.tiangolo.com/deployment/server-workers/)
  — note what the current version no longer recommends.
