# Layer 1 · Topic 6 — File descriptors, and "too many open files"

### The takeaway (read this first)

**The one idea:** every open file, socket, and pipe holds a slot in a
finite per-process table, and leaking them — forgetting to close on an
error path, an unbounded connection pool — eventually fails loudly, but
almost always far away from the line that actually caused it.

**Why it matters in practice:** "too many open files" (or your language's
equivalent phrasing of `EMFILE`) is one of the most common real
production incidents there is, precisely because the symptom and the
cause are so far apart. Recognizing the *signature* immediately — instead
of treating it as some novel bug each time — turns hours of debugging
into a five-minute fix.

**You'll know it landed when:** you see this error (in any language) and
your first thought is "something isn't closing a handle on an error
path," not "what is this exotic new failure."

## The concept

Every open file, socket, pipe, and even some in-kernel objects (eventfds,
epoll instances themselves) consume a slot in your process's **file
descriptor table**. That table has a ceiling — `RLIMIT_NOFILE`, inspectable
and settable via `ulimit -n` — and once you hit it, every subsequent
`open()`, `socket()`, or `accept()` fails with `EMFILE`. This is one of the
most common real production incidents there is, precisely because the
cause and the symptom are usually far apart: a connection pool that never
closes failed connections, a file handle leaked in an exception path that
skips `close()`, an HTTP client library that doesn't reuse connections —
none of these look dangerous in code review, and the failure doesn't show
up until the leak has had enough time (or traffic) to actually exhaust the
table, at which point the error you see is "too many open files," nowhere
near the line that actually caused it.

The fix is almost always the same regardless of language: make sure every
`open` has a `close` that runs even on the error path — `with` in Python,
`defer` in Go, RAII (`Drop`) in Rust, `try/finally` in Node — and prefer
APIs that force this structurally over ones that leave it to discipline.

## How each language actually gets there

**Python** — `resource.getrlimit(resource.RLIMIT_NOFILE)` reads the limit
straight from `getrlimit(2)`, no parsing required — the cleanest of the
four here. `os.open()` is a thin wrapper over `open(2)`, and a raised
`OSError` carries the real `errno` (`errno.EMFILE`) so you can distinguish
"out of file descriptors" from any other failure precisely.

**Node.js** — there's no `getrlimit` binding in the standard library, so
this demo reads `/proc/self/limits` directly (Linux-specific, fine for
this sandbox, not portable to macOS/Windows without another approach).
`fs.openSync` throws an `Error` whose `.code` is `'EMFILE'` — Node
surfaces the same underlying `errno` as a string code rather than a raw
integer, which is the idiomatic way to check for it in JS.

**Go** — `syscall.Getrlimit` exposes the raw syscall directly in the
standard library, matching Python's approach. Go's `os.Open` wraps
`open(2)` and returns a typed `*PathError` you can inspect; comparing
against `syscall.EMFILE` (or using `errors.Is`) lets you handle this
specific failure distinctly from "file not found" or "permission denied."

**C++** — `getrlimit(2)` called directly via `<sys/resource.h>`, no
parsing required, matching Python's and Go's clean paths. `open()` returns
`-1` and sets `errno` to `EMFILE` on failure — checking `errno` by hand is
about as close to the raw kernel contract as this lab gets, with no
wrapping exception type standing between you and the actual failure code.

**Java** — no `getrlimit` binding here either, so this reads
`/proc/self/limits` like the Node and Rust versions. What's more notable
is what the failure looks like from the calling code's side:
`FileInputStream`'s constructor throws a `FileNotFoundException` whose
*message* happens to mention "Too many open files" — there's no distinct,
checkable exception type or error code for "out of file descriptors" the
way Python's `errno.EMFILE`, Go's `syscall.EMFILE`, or Rust's
`raw_os_error()` give you. Catching this specific failure reliably in Java
means matching against the exception message text, which is measurably
less robust (message wording isn't guaranteed stable) than every other
language's approach in this lab.

**Rust** — no `getrlimit` in `std` (would need the `libc` or `rustix`
crate for a direct binding), so this demo reads `/proc/self/limits` like
the Node version. `std::fs::File::open` returns a `Result<File,
std::io::Error>`, and the `io::Error`'s `.raw_os_error()` gives you back
the same `errno` value the other languages are inspecting — `24` for
`EMFILE` on Linux, which you can see directly in the error message Rust
prints by default (`Too many open files (os error 24)`).

## The experiment

Open `/dev/null` in a loop without ever closing it, catch the specific
"out of file descriptors" error when it arrives, report how many were
opened, and compare that to the process's actual `RLIMIT_NOFILE`. Then
close everything and confirm the process is healthy again — the failure is
recoverable the instant you stop leaking.

## How to run

```
python3 python/fd_limit.py
node nodejs/fd_limit.js
cd golang && go run fd_limit.go
cd rust/fd_limit && cargo run --release
g++ -O2 -std=c++17 -o /tmp/fd_limit cpp/fd_limit.cpp && /tmp/fd_limit
cd java && javac FdLimit.java -d /tmp/javabuild && java -cp /tmp/javabuild FdLimit
```

## What I saw (this sandbox's soft/hard limit: 20,000)

| Language | reported limit | fds opened before failure | error |
|---|---|---|---|
| Python | soft=20000, hard=20000 | 19,997 | `EMFILE` |
| Node   | 20000 (soft), 20000 (hard) | 19,980 | `EMFILE` |
| Go     | soft=20000, hard=20000 | 19,995 | `too many open files` |
| Java   | 20000 (soft), 20000 (hard) | 19,995 | `FileNotFoundException: ... Too many open files` |
| Rust   | 20000 (soft), 20000 (hard) | 19,997 | `os error 24` |
| C++    | soft=20000, hard=20000 | 19,997 | `errno=24 (Too many open files)` |

None of the four hit exactly 20,000 — every process starts with a handful
of descriptors already in use (stdin, stdout, stderr, plus whatever the
language runtime itself opened: shared library handles, an internal epoll
instance for the event loop, etc.), and that baseline explains the small
gap. It's a good habit to check for that gap in real debugging too: if a
service is failing at, say, 950 open files against a 1024 limit, the
"missing" ~70 are worth accounting for before assuming they're all leaked
application connections.

## Answer before moving on

1. Two processes with the exact same `RLIMIT_NOFILE` opened slightly
   different numbers of descriptors before failing (19,980 to 19,997
   here). What's actually consuming the difference, and how would you find
   out exactly what a real process already has open before you've leaked
   anything?
2. Raising `ulimit -n` is the common "fix" for this in production. When is
   that the right call, and when is it actually just delaying the same
   crash at a higher number while hiding a real leak?
3. Sockets and regular files share the same descriptor table and the same
   limit. Does that change how you'd think about sizing connection pools
   for a service that also needs to read config files, write logs, and
   open TLS certificate files at startup?
4. Java's `FileNotFoundException` message-matching for this specific
   failure is more fragile than the other languages' typed error codes.
   What would you actually do in a real Java service to detect "we're
   close to running out of file descriptors" *before* it happens, rather
   than trying to catch and parse the exception after the fact?

## Layer 1 complete

That's all six topics. Before moving to Layer 2 (the network), it's worth
re-reading the roadmap's own test for this layer: can you explain, at the
OS level, why one synchronous call inside an async handler stalls every
concurrent request on that process (Topic 3 answers this directly), and
can you predict which of two implementations will be faster before
benchmarking, and be right more often than not (every topic here handed
you a prediction to make before you ran the numbers — worth going back and
checking how many you got right *before* running, not after).
