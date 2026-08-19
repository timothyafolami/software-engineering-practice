# Layer 1 · The machine underneath

Seven topics, each with working experiments in Python, Node.js, Go, Rust,
C++, and Java, and a detailed `README.md` explaining both the concept and
exactly how each language's runtime implements it under the hood.

| # | Topic | Folder |
|---|---|---|
| 1 | Memory: stack vs heap, cache locality | [`01-memory-locality/`](01-memory-locality/README.md) |
| 2 | Processes, threads, context switches, syscall cost | [`02-threads-and-syscalls/`](02-threads-and-syscalls/README.md) |
| 3 | The concurrency model of every runtime, precisely | [`03-concurrency-models/`](03-concurrency-models/README.md) |
| 4 | Races, deadlocks, atomicity, memory visibility | [`04-races-and-atomicity/`](04-races-and-atomicity/README.md) |
| 5 | Blocking vs non-blocking IO, and what epoll does | [`05-blocking-vs-nonblocking-io/`](05-blocking-vs-nonblocking-io/README.md) |
| 6 | File descriptors, and "too many open files" | [`06-file-descriptors/`](06-file-descriptors/README.md) |
| 7 | Inside a container: cgroups, CFS throttling, runtime sizing | [`07-inside-a-container/`](07-inside-a-container/README.md) |

The roadmap explicitly names the JVM's threading model alongside
Python/Node/Go for this layer — with Java now in the working language set,
Topic 3 covers it directly, including Java 21's virtual threads
(Project Loom), which turn out to be the closest thing in this lab to a
second implementation of Go's goroutine story.

## Why C++ and Java, and why added second

The first pass through Layer 1 used Python, Node.js, Go, and Rust — one
interpreted/GIL-limited language, one single-threaded-by-design language,
one M:N-scheduled compiled language, and one compile-time-enforced
compiled language. C++ and Java were added afterward to round the set out
in two specific ways worth knowing about before you read the topic READMEs:

- **C++** has none of Rust's compile-time safety net but the *same*
  undefined-behavior-on-data-race memory model — writing the same
  experiments in both is the sharpest way in this whole lab to see
  exactly what Rust's type system buys you (see Topic 4), and C++ is also
  the only language here that talks to `epoll` directly instead of
  trusting a runtime to be doing it somewhere underneath (see Topic 5).
- **Java** sits in a genuinely different spot from every other language
  here: JIT-compiled to native code like a compiled language, garbage
  collected and memory-safe like Python/Node/Go, and — since Java 21 —
  offering virtual threads, a second real answer (after Go's goroutines)
  to "how do you get cheap, massively-concurrent units of work without
  hand-rolling an event loop."

## The "you own this" test (from the roadmap)

> You can explain, at the OS level, why one synchronous call inside an
> async handler stalls every concurrent request on that process, and you
> can predict which of two implementations will be faster before
> benchmarking, and be right more often than not.

Topic 3 builds this exact scenario across all six languages — including
two, Go and Java, that specifically don't suffer from it, which is just as
important to be able to explain as the four that do. Topics 1, 2, 5, and 6
are all designed the same way: read the concept, predict the result, then
run it and check.
