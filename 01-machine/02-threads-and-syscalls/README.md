# Layer 1 · Topic 2 — Processes, threads, and what a syscall costs

### The takeaway (read this first)

**The one idea:** crossing from your program into the kernel costs real,
measurable time (roughly 300-700ns here, regardless of language) — and
"spawn a thread" vs. "spawn a process" are wildly different costs because
of exactly how much the kernel has to duplicate to create each one.

**Why it matters in practice:** this is the actual reason "just spawn a
process per request" doesn't scale the way "spawn a thread per request"
does, and why some runtimes (Go's goroutines, Java's virtual threads)
exist specifically to give you concurrency *without* paying a real OS
thread's cost per unit of work. It's also why "too many open files" isn't
the only kernel-resource ceiling you'll hit in production — thread and
process counts have limits too, and each one costs more to create than
the last.

**You'll know it landed when:** given any two operations, you can guess
which one crosses into the kernel and which doesn't, and you have a rough
mental ladder of cost — syscall < thread < process — that you can reach
for before reaching for a profiler.

### The concept

Your program doesn't talk to hardware directly. Every time it needs
something the kernel controls — read a byte, open a socket, ask for the
time, allocate memory beyond what's already mapped — it has to cross from
**user mode** into **kernel mode** and back. That crossing is a syscall,
and it is not free: the CPU has to save your program's state, switch page
tables (or at least switch privilege rings), validate the arguments the
kernel is about to trust, do the actual work, and switch back. On Linux
x86-64 that round trip runs somewhere in the 100–1000+ ns range depending
on the syscall and the kernel's mitigations (Spectre/Meltdown mitigations
made this measurably more expensive across the industry) — hundreds of
times slower than a userspace function call, which is often ~1 ns.

Threads and processes are built out of the same primitive (the kernel has
to create a schedulable entity either way) but cost very different amounts:

A **thread** shares its parent's address space, open file descriptors, and
most other process state. Creating one still means the kernel allocates a
`task_struct`, a kernel stack, and registers it with the scheduler — but
nothing about memory mappings needs to be duplicated.

A **process** (via `fork`+`exec`, or your language's higher-level wrapper)
needs page tables either copied (fork, with copy-on-write to soften the
blow) or built fresh (`exec` loading a new binary), a new file descriptor
table, and a new address space entirely. It's a fundamentally heavier
operation, and the experiments below show by how much.

## How each language actually gets there

**Python** — `os.read`/`os.open` call directly into `read(2)`/`open(2)`
via thin C wrappers; there's very little between your Python call and the
real syscall, which is why the per-call overhead you measure is close to
the syscall's true cost plus CPython's own call overhead. `threading.Thread`
maps 1:1 onto a real OS thread (`pthread_create` under the hood) — Python
has no green-thread concept in the standard runtime. `multiprocessing.Process`
does a real `fork()` (on Linux) plus bookkeeping to reconnect stdio and set
up IPC, which is why it's an order of magnitude more expensive than a
thread here.

**Node.js** — `fs.readSync`/`fs.openSync` go through libuv, which on Linux
still ultimately calls the same `read(2)`/`open(2)` — but there's a layer
of C++ binding and argument marshalling between your JS call and the
syscall, which is part of why Node's per-call syscall cost measures higher
than Go's or Rust's despite hitting the same kernel function. The bigger
surprise is `worker_threads`: a `Worker` isn't a lightweight OS thread with
your existing code attached — it boots an entirely separate V8 isolate
(its own heap, its own JIT, its own event loop) inside that OS thread. That
initialization cost is why spinning up a worker measured barely cheaper
than spawning a whole child process in the experiment below. Worker
threads exist for offloading sustained CPU-bound work, not for cheap,
frequent, short-lived concurrency the way goroutines or even Python
threads are used.

**Go** — goroutines are Go's own invention: a ~2KB (growable) stack managed
entirely by the Go runtime, not the kernel. Creating one means allocating
that stack and putting a struct on a run queue — no syscall involved at
all, which is why it's roughly 1000x cheaper than a process here and
often cheaper than a Python thread by two orders of magnitude. The
`runtime.LockOSThread` experiment tries to force a *real* OS thread per
goroutine, but the result reveals something important on its own: Go's
runtime aggressively caches and reuses OS threads across goroutines rather
than tearing them down and recreating them, so even "forcing" a dedicated
OS thread here doesn't show a real thread's true creation cost — you're
measuring thread *handoff*, not thread *creation*. That thread-caching is
deliberate: it's exactly what makes goroutines cheap to use liberally
without the runtime constantly paying the kernel's thread-creation tax.

**Rust** — `std::thread::spawn` always creates a genuine 1:1 OS thread.
There is no runtime hiding this cost from you the way Go's does — if you
want cheaper concurrent units in Rust, you reach for an async runtime
(tokio's tasks, which behave much more like goroutines: user-space
scheduled, cheap to create by the thousands). `std::fs::File::open` is a
near-direct wrapper over `open(2)`/`read(2)`, so Rust's numbers here are
close to Go's — both are compiled, both have thin syscall wrappers, and
the difference you'll see between them is mostly noise plus whatever the
optimizer decided to do with the surrounding loop.

**C++** — the closest thing in this lab to "no wrapper at all." `read()`
and `open()` are called directly from POSIX headers with zero intervening
runtime. The process experiment here does something the other languages'
higher-level APIs hide: it separates **`fork()` alone** from **`fork()`
followed by `exec()`**. A bare `fork()` just copy-on-write duplicates the
calling process's address space — genuinely cheap, because nothing is
actually copied in memory until either process writes to a shared page.
Every other language's "spawn a process" API (Python's `multiprocessing`,
Go's `os/exec`, Java's `ProcessBuilder`) does fork *then* exec: throw that
freshly-duplicated address space away and load an entirely different
binary from disk in its place. That second step is where the real cost
lives, and the experiment measures it directly — see the results below for
just how much of "process creation cost" is actually "loading a new
program image cost."

**Java** — `FileInputStream.read()` on `/dev/zero` does reach the same
`read(2)` syscall eventually, but by a longer road than C++ or Go: a JNI
native method call, a transition out of the JVM's managed execution
context, argument marshalling in both directions. That's real, measurable
overhead layered on top of the same kernel call, part of why Java's
syscall numbers land closer to Node's than to Go's or Rust's despite Java
being JIT-compiled to native code for its own logic. `Thread` is a genuine
OS thread (the JVM has offered lighter-weight virtual threads since Java
21 — see Topic 3's README for why that belongs to the concurrency-model
discussion more than this one). `ProcessBuilder` does a real fork+exec
under the hood, same as Go's `os/exec`, which is why their ratios land in
a similar range.

## The experiments

Each language has two scripts:

- **`syscall_cost.*`** — call `read()` on `/dev/zero` (about the cheapest
  real syscall there is: no disk, no network, the kernel just zeroes your
  buffer) N=500,000 times, and compare the per-call time to an equivalent
  pure in-language loop doing comparable "work" with zero syscalls. The
  ratio tells you how many nanoseconds of pure computation a single syscall
  is worth in that language.
- **`thread_vs_process.*`** (Go's version adds a third tier, goroutines) —
  spawn N units of concurrency that do nothing (`noop`), join/wait on each,
  and time the total. This isolates *creation* overhead from any actual
  work.

## How to run

```
python3 python/syscall_cost.py && python3 python/thread_vs_process.py
node nodejs/syscall_cost.js && node nodejs/thread_vs_process.js
cd golang && go run syscall_cost.go && go run thread_vs_process.go
cd rust/syscall_cost && cargo run --release
cd rust/thread_vs_process && cargo run --release
g++ -O2 -std=c++17 -pthread -o /tmp/syscall_cost cpp/syscall_cost.cpp && /tmp/syscall_cost
g++ -O2 -std=c++17 -pthread -o /tmp/thread_vs_process cpp/thread_vs_process.cpp && /tmp/thread_vs_process
cd java && javac SyscallCost.java ThreadVsProcess.java -d /tmp/javabuild && \
  java -cp /tmp/javabuild SyscallCost && java -cp /tmp/javabuild ThreadVsProcess
```

## What I saw (sandbox run, 2 vCPUs)

**Syscall cost** — `read(/dev/zero)` vs. an equivalent pure loop:

| Language | syscall (ns/call) | pure loop (ns/iter) | ratio |
|---|---|---|---|
| Python | 439.7 | 45.8 | 9.6x |
| Node   | 723.4 | 5.9  | 122.4x |
| Go     | 422.1 | 0.9  | 467.3x |
| Java   | 421.3 | 5.8  | 72.6x |
| Rust   | 339.1 | 1.6  | 206.1x |
| C++    | 308.3 | 0.3  | 1028.5x |

Notice the syscall's *absolute* cost (roughly 300–720 ns) is in the same
ballpark across all six languages — it's the same kernel doing the same
work regardless of who's asking. What changes wildly is the ratio, because
each language's baseline "cost of doing nothing" is wildly different. C++
posts the most extreme ratio of the lab specifically because `-O2`
auto-vectorized the pure loop into something close to free, which makes
the syscall look a thousand times more expensive by comparison — same
lesson as the memory-locality experiment: a slow baseline doesn't just
make everything slower, it compresses the *visible gap* between cheap and
expensive operations, and a fast baseline stretches that gap back open.

**Thread vs process spawn+join/wait** (N=200, except Node's worker
comparison at N=100 because worker startup is slow enough to make 200 slow
to run):

| Language | cheapest unit | cost | heavier unit | cost | ratio |
|---|---|---|---|---|---|
| Python | thread | 224.8 us | process (fork) | 2279.7 us | 10.1x |
| Node   | worker_thread | 49,533.9 us | child process | 58,288.2 us | 1.2x |
| Go     | goroutine | 0.6 us | process (fork+exec) | 1,465.0 us | 2,327x |
| Java   | Thread | 314.4 us | process (fork+exec) | 2,873.6 us | 9.1x |
| Rust   | OS thread | 83.3 us | process (fork+exec) | 1,340.6 us | 16.1x |
| C++    | std::thread | 66.6 us | bare fork() / fork()+exec | 375.8 us / 1,479.1 us | 5.6x / 22.2x |

Go's goroutine number is not a typo: goroutines really are roughly
three-and-a-half orders of magnitude cheaper than a process on this
machine, and about two orders of magnitude cheaper than a Python thread.
Node's worker_threads, on the other hand, barely beat a full process —
because a worker *is* almost as heavy as a process, just without the
kernel-level isolation.

C++'s two process numbers are the most instructive pair in this table: a
bare `fork()` with no `exec()` (child just exits immediately, still
running the exact same binary) costs 375.8 us — only 5.6x a thread. Add
`exec("/usr/bin/true")` — load a completely different program image from
disk — and the cost nearly quadruples to 1,479.1 us, 22.2x a thread. Every
other language's process-spawning API in this table does fork+exec
(or the platform equivalent), which is why their ratios land closer to
C++'s *second* number than its first. "Spawning a process is expensive"
is really two separate costs bundled together, and C++ is the only
language here low-level enough to pull them apart.

## Answer before moving on

1. Why is Node's `worker_thread` almost as expensive as a `child_process`,
   when a thread is supposed to be the cheap option? What is a Worker
   actually bringing with it that a Python or Rust thread doesn't?
2. Go's "locked OS thread" number came back nearly identical to its plain
   goroutine number. Does that mean OS threads are free in Go? If not,
   what did this experiment actually measure, and what would you change to
   measure real OS thread creation cost in Go?
3. Python's `multiprocessing.Process` and Java's `ProcessBuilder` both
   showed process costs much closer to C++'s fork+exec number than to its
   bare-fork number. Given what fork+exec actually does, why would neither
   of those higher-level APIs offer you a "just fork, don't exec" option
   the way raw C++ can?

## Next up

The concurrency model of each runtime — what actually happens when one
"synchronous" call shows up inside async code. Say the word when ready.
