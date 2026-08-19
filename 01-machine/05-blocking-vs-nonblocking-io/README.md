# Layer 1 · Topic 5 — Blocking vs non-blocking IO, and what epoll is doing

### The takeaway (read this first)

**The one idea:** there is exactly one real mechanism underneath every
async IO framework in every language in this lab — make sockets
non-blocking, register them with the OS's readiness API (`epoll` on
Linux), and let one thread ask "which of these are ready?" instead of
parking one thread per connection.

**Why it matters in practice:** this demystifies "async" from something
that feels like a language-specific trick into one specific, learnable
piece of OS mechanism that Python, Node, Go, Rust, Java, and hand-rolled
C++ are all just wrapping differently. It also tells you exactly when
async IO helps (waiting on slow, IO-bound dependencies) and when it
doesn't (CPU-bound work still needs real parallelism, not more waiting
efficiently).

**You'll know it landed when:** you can explain what `epoll_wait` is
doing in one sentence, and predict — correctly, before running
anything — roughly how much a workload will speed up from going
concurrent based on whether it's IO-bound or CPU-bound.

## The concept

A **blocking** read on a socket does exactly what it sounds like: the
calling thread asks the kernel for data, and if none is ready yet, the
kernel suspends that thread entirely — it burns no CPU, but it also can't
do anything else, including handle any other connection, until data
arrives. If you want to serve 10,000 slow connections this way, you need
10,000 threads, each mostly idle, each still costing a stack and a kernel
scheduling entity.

**Non-blocking** IO flips this: you ask the kernel "is this ready?", and if
not, you move on to check something else. Doing that by hand (polling in a
loop) wastes CPU, so real systems use a **readiness API** instead —
`epoll` on Linux (`kqueue` on BSD/macOS, IOCP on Windows). You register a
whole set of file descriptors with the kernel once, then make a single
call — `epoll_wait` — that blocks *your one thread* until *any* of them
becomes ready, and tells you which. One thread, one syscall, thousands of
connections. This is the actual mechanism underneath every async runtime
in this lab: asyncio's event loop, libuv (Node), Go's netpoller, and
tokio's reactor are all, at the bottom, a loop that calls `epoll_wait` and
dispatches to whoever's waiting on the descriptor that became ready.

## How each language actually gets there

**Python** — a plain `socket.recv()` is a genuinely blocking syscall; the
OS parks the calling thread. `asyncio.open_connection()` instead creates a
non-blocking socket (`O_NONBLOCK`) and registers it with the event loop's
selector, which uses `epoll` on Linux under the hood (via the `selectors`
module). The `await` doesn't poll in a spin loop — it suspends the
coroutine and returns control to the loop, which is sitting in
`epoll_wait` waiting for the kernel to say "this fd has data."

**Node.js** — there's no synchronous socket API in normal use at all;
every `net.Socket` is non-blocking and event-driven from the start, wired
into libuv's event loop, which itself wraps epoll on Linux. This is why
the "serial" version of this experiment isn't really testing OS-level
blocking IO the way Python's or Go's does — it's testing what happens when
*your code* chooses to await one request before starting the next, not
what the IO layer forces on you. Worth noticing: in Node, "serial vs
concurrent" is a scheduling decision you make, not a constraint the
runtime imposes.

**Go** — `net.Conn.Read`/`Write` *look* like classic blocking calls from
the goroutine's point of view: you call them, execution appears to just
stop until data's ready. Underneath, the runtime has already made the
socket non-blocking and registered it with its internal netpoller
(epoll-based on Linux); when your goroutine calls `Read` and nothing's
ready, the runtime parks that goroutine specifically (not the OS thread it
was running on) and lets the thread go do other work, resuming the
goroutine when epoll reports the fd is ready. You get to write
straight-line, synchronous-looking code and still get non-blocking IO
underneath — arguably Go's single best piece of ergonomic design.

**C++** — this is the one language in the lab that talks to epoll
directly, with nothing hidden. `io_demo.cpp`'s "concurrent" path does
exactly what every other language's runtime does somewhere underneath: put
every socket in non-blocking mode (`fcntl(fd, F_SETFL, O_NONBLOCK)`),
register all of them with one `epoll_create1` instance, and loop on
`epoll_wait` — a single syscall that blocks until *any* registered
descriptor is ready, returning the set of ones that are. Everything else
in this lab's async story (asyncio's selector, libuv's event loop, Go's
netpoller, tokio's reactor via `mio`) is a more ergonomic wrapper around
this exact loop. Writing it by hand once is the fastest way to stop
thinking of "the event loop" as magic.

**Java** — `java.net.Socket` is genuinely blocking, the same as Python's
raw sockets. `java.nio`'s `SocketChannel` + `Selector` is Java's own
abstraction over the OS readiness API, and on Linux it's backed by
`sun.nio.ch.EPollSelectorImpl` — meaning `Selector.select()` is, a few
layers down, calling the same `epoll_wait()` the C++ version calls
explicitly. This is the same pattern as asyncio's selector module: a
managed language exposing epoll through a portable, cross-platform API
name (`Selector`, `selectors`) rather than the OS-specific syscall name.

**Rust** — the two IO stacks are kept deliberately separate and visible.
`std::net::TcpStream` is genuinely, unapologetically blocking — no hidden
runtime, no magic, a `read()` call blocks the OS thread exactly like raw C
would. `tokio::net::TcpStream` is a different type entirely: it registers
with `mio` (a thin cross-platform epoll/kqueue/IOCP wrapper) and integrates
with tokio's task scheduler, suspending the *task* rather than a thread.
Choosing which one you get is explicit at the type level — you can't
accidentally get async behavior from `std::net`, or accidentally block an
executor thread with `tokio::net` (short of literally calling a blocking
function inside an async fn, which is exactly the Topic 3 experiment).

## The experiment

A local TCP server replies after an artificial 100ms delay (a stand-in for
a slow downstream dependency), thread-per-connection (or goroutine/task-
per-connection) so it isn't the bottleneck. Each language's client hits it
20 times two ways: one request at a time, and all 20 "at once" using
whatever concurrency primitive that language uses for IO.

## How to run

```
python3 python/io_demo.py
node nodejs/io_demo.js
cd golang && go run io_demo.go
cd rust/io_demo && cargo run --release
g++ -O2 -std=c++17 -pthread -o /tmp/io_demo cpp/io_demo.cpp && /tmp/io_demo
cd java && javac IoDemo.java -d /tmp/javabuild && java -cp /tmp/javabuild IoDemo
```

## What I saw (N=20 requests, 100ms server delay each)

| Language | serial | concurrent | speedup |
|---|---|---|---|
| Python (asyncio) | 2.025s | 0.115s | 17.6x |
| Node (Promise.all) | 2.035s | 0.108s | 18.8x |
| Go (goroutine per request) | 2.026s | 0.103s | 19.7x |
| Java (java.nio Selector) | 2.043s | 0.123s | 16.6x |
| Rust (tokio tasks) | 2.014s | 0.103s | 19.6x |
| C++ (raw epoll) | 2.013s | 0.103s | 19.5x |

All six converge on the same shape, C++'s hand-rolled epoll loop included —
which is exactly the point: writing the raw version and getting the same
result as five higher-level runtimes is the strongest possible confirmation
that they're all doing the same thing underneath. Serial cost is almost
exactly N × delay (20 × 100ms = 2s, because each request genuinely waits
out the full round trip before the next one starts), and concurrent cost is
almost exactly one delay period plus a small constant (all 20 requests are
in flight at once, so the wall-clock cost is dominated by the single
slowest one, not the sum of all of them). This is the whole economic
argument for non-blocking IO in one number: ~20x throughput for IO-bound
work, for free, using the exact same server.

## Answer before moving on

1. If the local server in this experiment were CPU-bound instead of just
   sleeping (say, it did real work per request instead of an artificial
   delay), would the ~20x concurrent speedup still hold? What would limit
   it, and where would that limit actually live — client or server?
2. Go's blocking-looking `net.Dial`/`Read` and Python's `asyncio.open_connection`
   both end up calling `epoll_wait` somewhere underneath. What's the actual
   difference in what gets suspended when data isn't ready yet — the
   thread, or something smaller? Why does that difference matter at scale?
3. Node's "serial" number here isn't really testing OS-level blocking IO.
   What would you have to add to the Node script to demonstrate genuine
   OS-level blocking IO the way the other three languages did?

## Next up

File descriptors, and why "too many open files" is one of the most common
production failures.
