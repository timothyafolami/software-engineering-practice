# Layer 1 · Topic 3 — The concurrency model of every runtime, precisely

### The takeaway (read this first)

**The one idea:** every runtime picks exactly one concurrency model
(one OS thread cooperatively shared, many OS threads, green threads
multiplexed onto a few real ones, or "whatever you build yourself"), and
one blocking call made in the wrong place freezes *everything else*
sharing that same resource — not just the caller.

**Why it matters in practice:** "the whole service just hangs sometimes"
is one of the most common vague bug reports in software, in every
language, and it is almost always this exact mechanism. Knowing your
runtime's specific model turns that vague report into a specific,
checkable hypothesis in seconds.

**You'll know it landed when:** given any runtime (even one not in this
lab), you can say precisely what happens if a blocking call gets made
inside its concurrency primitive, and name the specific fix that runtime
offers (offload to a thread pool, `spawn_blocking`, a worker thread, a
virtual thread — the name changes, the fix is always "get this off the
resource everything else needs").

This is the flagship experiment of Layer 1. The roadmap's "you own this
when" test for the whole layer is: *can you explain, at the OS level, why
one synchronous call inside an async handler stalls every concurrent
request on that process, and can you predict which of two implementations
will be faster before benchmarking.* This topic builds that test directly.

## The concept, per runtime

**Python (asyncio) — single OS thread, cooperative scheduling.** The GIL
already limits CPython to one thread executing bytecode at a time; asyncio
goes further and runs its entire event loop on a *single* thread by
design. Coroutines cooperate by choice: a coroutine keeps running until it
hits an `await` on something that isn't immediately ready, at which point
it hands control back to the loop, which polls the OS (`epoll_wait` on
Linux, wrapped by the `selectors` module) for what's ready and resumes the
matching coroutine. This only works if *every* piece of code in the chain
actually awaits instead of blocking. Call `time.sleep()`, a synchronous DB
driver, or do a long CPU-bound computation directly inside a coroutine, and
you've handed the one thread to code that will never yield it back until
it's done — the entire process, all "concurrent" requests included, stops.

**Node.js — single thread for your JS, event loop plus a hidden thread
pool.** Same single-thread constraint on your JavaScript, enforced more
strictly than Python's (there's no equivalent of Python's OS-thread-based
`threading` module for running JS in parallel — only `worker_threads`,
which are genuinely separate isolates, not shared-memory-by-default
threads). What makes Node usually *feel* less fragile than raw
single-threaded code is that libuv already runs a lot of things
off-thread for you: file IO, DNS lookups, and crypto functions like
`crypto.pbkdf2` are dispatched to a libuv-managed thread pool (default 4
threads) so they don't block your main thread even though they look
synchronous-ish in how you'd reach for them. But that safety net doesn't
extend to arbitrary CPU-bound JS you write yourself — a big `JSON.parse`,
a hand-rolled hash loop, `bcrypt.hashSync` — none of that yields, and it
blocks the loop exactly like Python's `time.sleep()` does.

**Go — goroutines, M:N scheduled onto OS threads by the runtime.** This is
the one built specifically to make this whole class of bug rarer. The Go
scheduler multiplexes many goroutines onto a smaller number of OS threads
(`GOMAXPROCS` of them by default, one per core). Two separate mechanisms
protect you here: the **netpoller** detects when a goroutine is about to
block on network IO or a sleep and parks just that goroutine, letting its
OS thread run something else — so blocking-looking code doesn't actually
block a thread. And since Go 1.14, **asynchronous preemption** means even a
goroutine stuck in a tight CPU loop with no function calls (previously a
real gap — goroutines used to only yield at function-call boundaries) gets
interrupted via an OS signal so other goroutines still get scheduled. The
experiment below deliberately sets `GOMAXPROCS=1` — one OS thread for
*everything* — specifically to try to force the failure mode, and it still
doesn't happen.

**C++ — no default at all, and no standard runtime crate either.** The
language gives you `std::thread` and synchronization primitives and stops
there; whatever scheduling policy you get is whatever you (or a library —
Boost.Asio, libuv, folly) build on top. The most common real-world shape
is a fixed-size thread pool, and a fixed-size pool has exactly the same
failure mode as Python's or Node's single thread the moment you shrink it
enough (or load it heavily enough): submit a blocking task to the same
pool your other work runs on, and that work waits behind it. This is
worth sitting with, because it means "use a compiled, low-level language"
buys you *nothing* here by itself — the failure mode is about scheduling
policy, not about the language's raw speed. The experiment below uses a
hand-rolled single-worker thread pool for exactly this reason: at pool
size 1, it's structurally identical to Python's single OS thread.

**Java — platform threads traditionally, virtual threads since Java 21.**
For most of Java's history, `Thread` meant a real 1:1 OS thread, and a
fixed-size `ExecutorService` pool has precisely the C++/Python/Node
failure mode: shrink it to one worker, submit a blocking call alongside
other work, and everything else waits. Java 21 (JEP 444, "Project Loom")
changes the story with **virtual threads** — cheap, JVM-scheduled threads
that get multiplexed onto a small pool of real "carrier" OS threads,
directly analogous to Go's goroutines. The mechanism is different from
Go's netpoller (the JVM specifically instruments blocking calls it knows
about — `Thread.sleep`, blocking IO, most `java.util.concurrent`
primitives — to **unmount** a virtual thread from its carrier when it
blocks, freeing that carrier for other virtual threads, then remount it
later, possibly on a different carrier, when it's ready to proceed) but
the payoff is the same: code that looks and reads exactly like blocking
code no longer costs you a scarce OS thread while it waits. This is the
one language in the lab where you can point at the *exact same* failure
mode and its fix using only a one-line change to which executor factory
method you call — see the two experiments below.

**Rust — no default at all; the runtime crate decides.** `async fn` in
Rust compiles to a state machine, but nothing executes it without an
executor, and the standard library doesn't ship one. Reach for tokio (as
these experiments do) and you choose a runtime "flavor": `current_thread`
runs every task cooperatively on one OS thread, exactly like Python's
asyncio, while the default `multi_thread` flavor runs a pool of worker
threads and can migrate tasks between them. Call `std::thread::sleep` (a
real blocking syscall, not `tokio::time::sleep`) inside a task on
`current_thread`, and you get precisely Python's failure mode. Rust's
answer is `tokio::task::spawn_blocking`, which hands the blocking call to a
separate, dedicated blocking-thread pool that every tokio runtime keeps
around regardless of flavor — structurally the same fix as Python's
`run_in_executor` and Node's `worker_threads` offload, just under a
different name.

## The experiment

A ticker fires roughly every 100ms and we record the real timestamp of
every tick. In the middle of a ~1.4s run, we introduce one blocking
operation lasting 1 second — implemented the "naive" way (directly inside
async/single-threaded code) in the `bad_*` version, and offloaded to a
separate thread in the `good_*`/fixed version. Then we look for a gap in
the ticker's timestamps around the blocking window.

## How to run

```
python3 python/bad_blocking.py && python3 python/good_offloaded.py
node nodejs/bad_blocking.js && node nodejs/good_offloaded.js
cd golang && go run ticker_survives_blocking.go
cd rust/bad_blocking && cargo run --release
cd rust/good_offloaded && cargo run --release
g++ -O2 -std=c++17 -pthread -o /tmp/cpp_bad cpp/bad_blocking.cpp && /tmp/cpp_bad
g++ -O2 -std=c++17 -pthread -o /tmp/cpp_good cpp/good_offloaded.cpp && /tmp/cpp_good
cd java && javac BadBlocking.java GoodOffloaded.java -d /tmp/javabuild && \
  java -cp /tmp/javabuild BadBlocking && java -cp /tmp/javabuild GoodOffloaded
```

## What I saw

| Language | bad (blocked) | good (fixed) | expected ticks over ~1.4s |
|---|---|---|---|
| Python | **4** ticks | 14 ticks | ~14 |
| Node   | **4** ticks | 14 ticks | ~15 |
| Go (GOMAXPROCS=1, blocking sleep) | 14 ticks | — (no fix needed) | ~14 |
| Go (GOMAXPROCS=1, CPU busy loop)  | 14 ticks | — (no fix needed) | ~14 |
| Java (fixed pool of 1 vs virtual threads) | 13 ticks, **max gap 1.00s** | 13 ticks, max gap 0.13s | ~14 |
| Rust   | 15 ticks, **max gap 1.10s** | 15 ticks, max gap 0.10s | ~14 |
| C++ (1-worker pool vs dedicated thread) | 13 ticks, **max gap 1.10s** | 13 ticks, max gap 0.10s | ~14 |

Python and Node show the failure directly in the tick count: roughly 10
ticks simply never happened, because the one thread that would have fired
them was busy for a full second. Go shows the opposite result on purpose —
even with only one OS thread available for the entire program, and even
for a pure CPU loop with no IO in it at all, the ticker keeps ticking. This
is Go's scheduler doing exactly the job it was designed for.

C++ and Java's "bad" runs both show the same pattern as Rust's, and for
the exact same reason: a task **queue** (the hand-rolled thread pool in
C++, `ExecutorService` in Java) buffers work rather than dropping it, so
once the one worker thread frees up, every queued tick task runs
back-to-back in a flash. The count recovers to nearly normal (13 out of an
expected ~14) while the *timing* doesn't — a 1.00-1.10 second gap where
nothing happened at all. This is a general property of anything built on
a task queue, not a tokio quirk: if you only monitor throughput or
completed-task counts on a real thread-pool-backed service, a queue that's
silently building up latency behind a stuck worker can look completely
healthy right up until it doesn't.

Java's "good" run is the most direct language-level parallel to Go's
result in this entire lab: swapping `Executors.newFixedThreadPool(1)` for
`Executors.newVirtualThreadPerTaskExecutor()` — one line — takes the max
gap from 1.00s down to 0.13s, because `Thread.sleep` inside a virtual
thread unmounts it from its carrier instead of blocking a scarce shared
resource. Two completely different runtimes (Go's compiled netpoller-based
scheduler, the JVM's bytecode-level thread virtualization) converging on
the same architectural answer is a strong signal that "don't let one
blocking task hold a resource everything else needs" isn't a language
trick — it's the actual shape of the problem.

Rust is the interesting one, and worth reading closely. The tick *count*
in the bad case looks fine — 15, same as the good case — which would
suggest nothing went wrong. It did: tokio's `interval()` defaults to
`MissedTickBehavior::Burst`, meaning a ticker that couldn't be polled for a
while doesn't lose those ticks, it fires all of them back-to-back the
instant it's polled again. The count recovers; the *timing* doesn't. That's
why this experiment also records each tick's timestamp and reports the
largest gap between consecutive ticks — 1.10s in the bad case (the entire
blocked window, showing up as one missing beat) versus 0.10s in the good
case (normal ticking, no stall). If you only checked the count here, you'd
conclude Rust doesn't have this problem. It does; you were just measuring
the wrong thing. That's arguably a more valuable lesson than the bug itself.

## Answer before moving on

1. In your own words: what specific thing does `run_in_executor` /
   `worker_threads` offload / `spawn_blocking` all have in common
   mechanically? (They're not the same API, but they're the same *idea*.)
2. Go didn't need a "good" version in this experiment. What would you have
   to write in Go to actually reproduce this failure mode on purpose? (Hint:
   the async-preemption safety net has known gaps — for instance, some
   tight loops with no function calls, or a goroutine that disables
   preemption, or truly massive `GOMAXPROCS`-exceeding goroutine counts
   fighting over few threads under sustained CPU load.)
3. Why does `MissedTickBehavior::Burst` exist as tokio's default at all —
   what's it protecting against, and when would you deliberately choose
   `MissedTickBehavior::Delay` instead?
4. Java's virtual threads and Go's goroutines solve the same problem with
   different mechanisms (JVM-level unmounting on known blocking calls vs.
   an OS-level netpoller plus async preemption). Can you think of a kind of
   blocking call that would defeat Java's approach but not Go's, or vice
   versa? (Hint: what happens in each if the blocking call is a JNI/cgo
   call into non-JVM/non-Go native code?)

## Next up

Races and atomicity — why `i++` is not atomic, and what each language's
type system does or doesn't do to stop you from writing that bug.
