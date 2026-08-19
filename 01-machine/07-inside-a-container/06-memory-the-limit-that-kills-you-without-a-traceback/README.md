# 7.6 — Memory: the limit that kills you without a traceback

### The takeaway (read this first)

**The one idea:** `memory.max` does not make allocation fail. It makes
your process *disappear*. `memory.high` makes it slow instead — and slow
is the version you can actually debug.

**Why it matters in practice:** "the pod restarts sometimes and we can't
find the error" is one of the most durable mysteries in production, and
the reason it is durable is that there is nothing to find. SIGKILL is not
catchable: no `MemoryError`, no traceback, no `atexit` hook, no shutdown
log line, no last-gasp metric. The evidence exists, but it is in the
kernel's records rather than your application's, and you have to know
which three places to look.

**You'll know it landed when:** you can name the exit code and derive it,
say where the kernel wrote down that it killed you, explain why your
`try/except MemoryError` never fires, and configure the version of the
limit that degrades instead of dying.

---

## The concept

`memory.max` is a hard ceiling on the cgroup's charged memory. When a
charge would exceed it, the kernel first tries to reclaim (page cache,
clean pages, anything droppable). If reclaim cannot free enough, the
cgroup OOM killer picks a victim inside the cgroup and sends **SIGKILL**.

Three consequences follow, and all three are the reason this is hard to
debug:

- **SIGKILL cannot be caught, blocked or handled.** Not by a signal
  handler, not by a runtime, not by a `finally`. The process is simply
  gone between one instruction and the next.
- **Exit code 137.** Derive it rather than memorising it: a shell reports
  a signal-terminated process as `128 + signal`, and SIGKILL is signal 9,
  so `128 + 9 = 137`. Any 137 in a restart log is a killed process, and in
  a container it is nearly always this.
- **Your allocation succeeded.** Under Linux's default overcommit, `malloc`
  and friends hand back an address without the memory existing yet. The
  charge happens when you *touch* the page. So the failure lands on a
  memory write, arbitrarily far from the allocation, and no allocator
  error path ever runs.

Where the evidence actually is:

| Where | What it says |
|---|---|
| `docker inspect <c> --format '{{.State.OOMKilled}}'` | `true` |
| exit code | `137` |
| `/sys/fs/cgroup/memory.events` | `oom_kill` incremented (also `high`, `max`, `low` counters) |
| `dmesg` on the host | the kill decision and the victim's RSS |
| your application logs | **nothing at all** |

### The version you can debug

`memory.high` is a *throttling* threshold, not a killing one. Set it below
`memory.max` and when the cgroup crosses it the kernel puts allocating
tasks under heavy reclaim pressure — the process survives, gets slower,
`memory.events`'s `high` counter climbs and `memory.pressure` (PSI) rises.
That is a signal you can alert on before an incident instead of a corpse
to autopsy after one. Compose exposes only the hard limit, so
`memory.high` has to be written into the cgroup directly.

### And the runtime half

`/proc/meminfo` inside the container reports **host** memory — it is not
namespaced. Anything sizing a cache, a buffer, a heap or a worker count
from "available memory" therefore over-commits by the ratio of host to
limit. That is the same defect as
[7.3](../03-ask-three-runtimes-how-big-the-machine-is/README.md)'s CPU
count, on a different axis, and the runtimes have made *different* choices
about it than they did about CPU.

---

## How each language actually gets there

All six. Memory is where the runtimes diverge most sharply, because each
one has a different answer to "what happens when I cannot get another
page" — and only some of those answers involve you finding out.

**Python — there is no heap ceiling, so the OOM killer is your heap
limit.** CPython's allocator will keep asking the OS for arenas until the
OS stops answering, and inside a container the OS stops answering by
killing you. Nothing in the interpreter reads `memory.max`. You can get a
*catchable* `MemoryError` by setting an `RLIMIT_AS` — but that is a
different limit, enforced by a different mechanism, and if you see that
traceback in a container you were not OOM-killed. The other Python
specific: freed objects go back to pymalloc's pools and arenas, and arenas
are only returned to the OS when completely empty, so RSS is sticky.
A burst that briefly needs 300MB leaves a process that looks like it needs
300MB.

**Node — the only runtime here that is container-aware by default on
memory.** libuv's `uv_get_constrained_memory()` reads the cgroup limit and
V8 sizes its default old-space heap from it, so a Node process in a 256MB
container does not plan for the host's 32GB. When the JS heap ceiling is
hit you get a *fatal error with a stack trace* — "Reached heap limit,
allocation failed" — and the process aborts rather than being SIGKILLed.
That is a genuinely different failure with a different exit code, and
telling the two apart in a restart log is the practical skill: record
which code you actually get for each. Note that the JS heap is not the
whole process: Buffers, native addons and worker threads live outside it,
so it is entirely possible to be OOM-killed at 137 with plenty of V8 heap
headroom left. `--max-old-space-size` overrides the default.

**Go — `GOMEMLIMIT`, a soft limit, and the death spiral it can cause.**
Go 1.19 added `GOMEMLIMIT`: instead of dying, the collector works harder
as you approach it, which is the right shape of behaviour. But Go did
*not* extend its 1.25 container-awareness work to memory, so there is no
cgroup-derived default — you set it explicitly, conventionally to around
90% of `memory.max`, leaving headroom for the parts of RSS the Go heap
does not cover (stacks, mmap'd files, cgo). The failure mode to know: if
live data genuinely exceeds the limit, the GC runs continuously to satisfy
it and the process burns all its CPU collecting instead of working. That
is a soft limit doing exactly what it promised, and it looks like a CPU
problem — which is how it collides with everything in 7.2.

**Java — container-aware and the most misread of the six.** With
`UseContainerSupport` (default since 8u191/JDK 10), `MaxRAMPercentage`
sizes the heap as a percentage of `memory.max` rather than of host RAM;
the HotSpot default is **25%**, which surprises people twice — once when
they discover the JVM is using a quarter of the container, and again when
they set it to 100 and get OOM-killed anyway. The reason for the second is
that the Java heap is only part of the JVM's RSS: metaspace, the code
cache, thread stacks, direct `ByteBuffer`s and GC bookkeeping all live
outside `-Xmx`. `-XX:NativeMemoryTracking=summary` plus `jcmd VM.native_memory`
is how you see the rest. And Java is the one runtime that can show you
*both* failures cleanly: exceed the heap and you get a catchable
`OutOfMemoryError` with a stack trace; exceed the container and you get
137 with nothing.

**Rust — no GC, so RSS is exactly what you asked for, and failure is an
abort.** There is no collector to tune and no heap ceiling to configure:
allocation goes to the system allocator, and RSS is live data plus
fragmentation. When allocation genuinely fails, the default behaviour is
`handle_alloc_error` → **abort**, not an `Err` — which is why
`Vec::try_reserve` exists for the code paths that need to survive it. In
practice, under Linux overcommit, you rarely reach that path in a
container: you get SIGKILLed on a page touch long before the allocator
returns null. Rust's real contribution here is that it makes the *size of
your live set* honest — there is no "the GC will get it eventually" to
hide behind.

**C++ — the sharpest illustration on this page.** `new` is specified to
throw `std::bad_alloc` on failure, so the obvious defensive code is a
`try`/`catch` around the allocation. Under Linux's default overcommit that
handler will essentially never run: `malloc` returns a valid pointer, the
kernel commits nothing, and the cgroup charge lands when you first write
to the page — at which point you are SIGKILLed inside a `memcpy`, not
inside your `catch`. Writing the same experiment in C++ and reading the
empty `catch` block is the most direct way to internalise "the limit that
kills you without a traceback": the language *has* the error path, and the
kernel's policy means you never reach it. (`vm.overcommit_memory=2` changes
that policy and is worth knowing exists, but it is a host-wide setting
with host-wide consequences.)

The through-line: **container-awareness for memory splits the six
differently than CPU did.** Node and Java read the limit; Go gives you a
knob and no default; Python, Rust and C++ have nothing to read it with —
and in those three the kernel's SIGKILL *is* the error handling.

---

## The experiment

Give the API `mem_limit: 256m` and an endpoint that accumulates a large
result set — an unpaginated query, a big JSON serialisation, the realistic
shapes rather than a synthetic `[0] * huge`. Drive it until the container
dies, then collect all five pieces of evidence from the table above,
including the empty one.

Then set `memory.high` *below* `memory.max` (write it directly) and repeat.
The container should survive, the `high` counter should climb,
`memory.pressure` should rise, and throughput should degrade gradually.

The per-language versions do the same allocation under the same 256MB
limit and report what their runtime did about it: which of them printed
something before dying, which exit code you got, and what the JVM's
`OutOfMemoryError` looks like next to Python's silence.

## How to run

```bash
cd 01-machine/07-inside-a-container

# service version, from the harness. Read this one for what it is: the api
# service sits at ~45 MiB whatever load you offer it, so it survives 256m
# comfortably and memory.events stays all-zero. That is a correct reading of
# a service that does not allocate, NOT a demonstration that 256m is enough
# for a service that does. The rows that actually reach a limit are the
# per-language ones below.
cd 00-harness
API_MEM=256m docker compose up -d --force-recreate api
docker compose exec api cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory.events
docker compose --profile load run --rm --no-deps -e ENDPOINT=/mixed -e RATE=200 k6 run /scripts/steady.js
docker inspect --format '{{.State.OOMKilled}} {{.State.ExitCode}}' \
  "$(docker compose ps -q api)"

# the soft-limit version: no Compose key exists for memory.high. This points
# python/oom.py at it, not the api, because the soft limit has to be reached
# to be observed. It samples for a fixed window and stops: throttled hard
# enough with swap disabled, the allocation loop makes almost no progress and
# `docker exec` into the container does not return -- so every reading is
# taken from a host-side sidecar.
../06-memory-the-limit-that-kills-you-without-a-traceback/docker/run_7_6.sh --high 200m

# all six runtimes at 256m, with every piece of evidence collected
cd ../06-memory-the-limit-that-kills-you-without-a-traceback
./docker/run_7_6.sh
./docker/run_7_6.sh --only python,java
./docker/run_7_6.sh --high 200m            # the soft-limit version

# per-language, each in a Linux container with --memory=256m
docker run --rm --memory=256m --memory-swap=256m -v "$PWD:/w" -w /w   python:3.13-slim python python/oom.py            # then: echo $?   -> 137
docker run --rm --memory=256m --memory-swap=256m -v "$PWD:/w" -w /w   node:24-slim node nodejs/oom.js --heap           # -> 134, WITH a stack trace
docker run --rm --memory=256m --memory-swap=256m -v "$PWD:/w" -w /w   node:24-slim node nodejs/oom.js --buffer         # -> 137, silence
docker run --rm --memory=256m --memory-swap=256m -v "$PWD:/w" -w /w   golang:1.25 sh -c 'cp /w/golang/oom.go /tmp && cd /tmp && go run oom.go'
docker run --rm --memory=256m --memory-swap=256m -e GOMEMLIMIT=230MiB   -v "$PWD:/w" -w /w golang:1.25 sh -c 'cp /w/golang/oom.go /tmp && cd /tmp && go run oom.go'
docker run --rm --memory=256m --memory-swap=256m -v "$PWD:/w" -w /w   rust:1 sh -c 'cp -r /w/rust/oom /tmp/oom && cd /tmp/oom && cargo run --release'
docker run --rm --memory=256m --memory-swap=256m -v "$PWD:/w" -w /w gcc:14   sh -c 'g++ -O2 -std=c++17 -o /tmp/oom /w/cpp/oom.cpp && /tmp/oom --reserve-only && /tmp/oom'
docker run --rm --memory=256m --memory-swap=256m -v "$PWD:/w" -w /w   eclipse-temurin:21 sh -c 'javac /w/java/Oom.java -d /tmp/b &&
    java -XX:MaxRAMPercentage=75 -cp /tmp/b Oom --heap'          # -> caught OOME
```

`--memory-swap` equal to `--memory` disables swap for the container. Leave
it off and the kernel can swap instead of killing, which turns this into a
much slower and much less interesting experiment.

The host-side halves, which are honest on macOS because each program
imposes its own ceiling and says it stopped *itself*:

```bash
python3 python/oom.py --free                 # RSS is sticky: arenas
node nodejs/oom.js --buffer                  # heapUsed flat, RSS climbing
(cd golang && GOMEMLIMIT=64MiB go run oom.go -pointers)  # GC pressure, no container
(cd golang && go run oom.go -pointers)                   # same heap, no limit:
                                                         # compare the GC CPU column
(cd rust/oom && cargo run --release -- --try-reserve)
g++ -O2 -std=c++17 -o /tmp/oom cpp/oom.cpp && /tmp/oom --reserve-only
(cd java && javac Oom.java -d /tmp/javabuild && java -Xmx64m -cp /tmp/javabuild Oom --heap)
```

**Linux containers only, and this one matters more than usual.** macOS has
no cgroup memory controller, no `memory.events`, and no cgroup OOM killer —
running `oom.py` on the Mac host will page and swap and eventually annoy
you, which is a different experiment with a different lesson. Run each
under `docker run --memory=256m` inside a Linux image.

## Predict, then record

Predict the exit code, and predict for each runtime whether *anything* is
printed before the process ends.

| Config | survived? | exit code | memory.events oom_kill | memory.events high | p99 |
|---|---|---|---|---|---|
| memory.max 256m only | | | | | |
| memory.high 200m + max 256m | | | | | |

| Runtime (256m) | printed anything? | exit code | caught by the language? | peak RSS |
|---|---|---|---|---|
| Python | | | | |
| Node | | | | |
| Go (no GOMEMLIMIT) | | | | |
| Go (GOMEMLIMIT=230MiB) | | | | |
| Rust | | | | |
| C++ | | | | |
| Java (MaxRAMPercentage=75) | | | | |

**Broken, not merely surprising.** If you get a Python `MemoryError`
traceback, you hit a different limit than the one you meant to — an
`RLIMIT_AS`, or the allocator failing for another reason; a cgroup OOM
kill never produces one. If nothing dies at all, the kernel reclaimed page
cache successfully: allocate faster, or allocate something unreclaimable
(touched anonymous pages, not a mapped file). If `docker inspect` says
`OOMKilled: false` with exit code 137, something else sent SIGKILL — check
whether your own script's timeout did it. If the Go row shows no
difference with and without `GOMEMLIMIT`, confirm the value parsed (it
takes a byte count or a suffixed string, and a silently-ignored value
looks exactly like no effect).

## Answer before moving on

1. Your service is OOM-killed once a day at unpredictable times. You have
   the exit code and nothing else. List, in order, the four readings you
   would collect before changing any limit — and say what each one rules
   out.
2. Java with `MaxRAMPercentage=100` in a 512MB container gets OOM-killed
   while `OutOfMemoryError` never fires. Explain the mechanism, and name
   three consumers of RSS that `-Xmx` does not cover.
3. `memory.high` degrades instead of killing. Give the case *for* running
   production with only `memory.max` and no `high` at all — then say what
   metric you would need in place before you would be comfortable with
   that choice.
4. Go's `GOMEMLIMIT` converts a memory problem into a CPU problem. Under a
   CPU quota ([7.2](../02-throttled-at-30-percent-cpu/README.md)), describe
   the compound failure: what does the throttle ratio do, and what does an
   average-utilisation dashboard show while it happens?

## Next up

[7.7 — Free-threaded Python, honestly, in 2026](../07-free-threaded-python-honestly-in-2026/README.md).
The last sub-topic takes the most-hyped change to this lab's primary
language and asks the only question this topic cares about: does removing
the GIL change any of the numbers you have just measured?
