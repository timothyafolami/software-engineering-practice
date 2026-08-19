# Layer 1 · Topic 4 — Races, deadlocks, atomicity, and memory visibility

### The takeaway (read this first)

**The one idea:** shared mutable state touched by more than one thread
without synchronization doesn't reliably fail — it corrupts silently,
sometimes, some of the time, in a way that "it worked when I tested it"
gives you zero evidence against.

**Why it matters in practice:** races are the root cause of an outsized
share of the bugs that vanish when you add a print statement, run under a
debugger, or run on a different machine — because all of those change the
exact timing the bug depends on. They're also one of the few bug classes
where "it passed all my tests" is closer to meaningless than for almost
any other kind of defect.

**You'll know it landed when:** you can spot a read-modify-write on shared
state with no lock or atomic around it on sight, in any language, and
treat it as a bug immediately rather than waiting for it to misbehave
first.

## The concept

`counter += 1` looks like one operation. It compiles to at least three:
load the current value, add one, store it back. If two threads do this on
the same shared variable with no synchronization, both can load the same
value before either stores, and one increment vanishes. This is a **lost
update**, the simplest member of a family of bugs (races, torn reads,
stale reads from missing memory barriers) that all stem from the same
root cause: sharing mutable state between threads without a rule for who
gets to see what, when.

The honest version of this section includes something most explanations
skip: **races are timing-dependent, and "reliably demonstrating one" is
itself an experiment that can fail in surprising, language-specific ways.**
Two of the four languages here did not show a lost update in the naive
"many threads incrementing a shared counter" version, for two completely
different and instructive reasons. Both are documented below rather than
papered over, because the *reasons why* are more valuable than a clean
number would have been.

## How each language actually handles (or doesn't handle) this

**Python** — the GIL means only one thread runs bytecode at any instant,
but it does not protect a multi-bytecode *statement*. In principle a
thread switch can land between the load and the store of `counter += 1`.
In practice, on this sandbox's CPython 3.11, the bare increment race did
not reproduce even under an aggressive switch interval (see results
below) — CPython's eval-breaker (the "should another thread run now?"
check) tends to land at the loop's backward jump rather than mid-statement
for a loop this small, so the read-modify-write triplet usually completes
as a unit by coincidence of instruction timing, not by any guarantee. The
lesson isn't "Python is safe here" — it's "don't trust a race not to
happen just because it's hard to trigger on your machine, today, with
this workload." The **check-then-act** experiment (`if key not in cache:
cache[key] = compute()`) proves the underlying danger is real: it races
reliably, because `compute()` contains an actual blocking call
(`time.sleep`), which is a guaranteed GIL-release point, giving the race a
wide-open window instead of a lucky one.

**Node.js** — ordinary Node code never has this problem for its own
variables, because there's only one thread ever touching them. The moment
you reach for `SharedArrayBuffer` plus real `worker_threads` for
parallelism, though, you're exposed to a genuine hardware-level race,
identical in kind to Go's or Rust's, and `Atomics.add`/`Atomics.load`/
`Atomics.store` exist specifically to give you back safe access to that
shared memory.

**Go** — `counter++` on a plain shared variable across goroutines is
undefined by the language and reliably shows lost updates in practice (see
results). Go also does something neither Python nor Rust does: its
built-in `map` type is explicitly documented as unsafe for concurrent
access, and the runtime actively **detects** concurrent read+write on a
map and crashes the process immediately with `fatal error: concurrent map
writes` — and critically, this is a *fatal* error, not a regular `panic`,
so `recover()` cannot catch it. That's a deliberate design choice: fail
immediately and unrecoverably rather than let a program limp along with an
internal data structure that might now be corrupted in ways nobody can
reason about.

**C++** — the language most directly comparable to Rust here, and the
contrast is the whole point: C++'s memory model has the identical rule
(unsynchronized concurrent access to ordinary data is undefined behavior)
but *none* of Rust's compile-time enforcement. There's no `unsafe` keyword
required, no `Send`/`Sync` trait to lie to — you just write `counter++`
across threads and it compiles cleanly, silently carrying UB. And true to
that shared memory-model rule, this experiment's C++ result came back
identical to Rust's: 0 lost updates, unchanged even at 20 million
increments per thread instead of 300,000 — the same signature that
pointed at optimizer involvement in the Rust writeup, not a narrow race
window. The practical lesson: C++ gives you Rust's exact hazard with none
of Rust's guardrail, which is a large part of why "modern C++" guidance
leans so hard on RAII wrappers (`std::lock_guard`), `std::atomic`, and
static analysis / thread-sanitizer tooling to claw back some of what
Rust's type system provides for free.

**Java** — no undefined-behavior escape hatch for the compiler here. The
Java Memory Model specifies precisely what's guaranteed for unsynchronized
access (not much — visibility isn't guaranteed without `volatile`, and
compound operations like `counter++` are never atomic without
`synchronized` or `java.util.concurrent.atomic`), but it doesn't grant the
JIT license to assume racy code "can't happen" and optimize on that
assumption the way C++/Rust's UB model does. That difference shows up
directly in the results below: Java's bare increment race reproduces
reliably and dramatically, unlike C++'s and Rust's.

**Rust** — safe Rust will not let you write this bug at all. `&mut T`
cannot be shared across threads unless the type proves synchronization
(`Mutex<T>`, `Atomic*`), enforced by the `Send`/`Sync` traits at compile
time. To even reproduce a genuine data race for this experiment, the code
has to explicitly reach for `unsafe`, wrap a raw pointer in a type, and
manually (and questionably) assert `unsafe impl Send`/`Sync` on it — which
is exactly the point: in Rust, "this code could race" becomes a visible,
grep-able, code-reviewable signal (the `unsafe` keyword) instead of a
silent possibility sitting in otherwise ordinary-looking code.

## The experiments

- **`race.*`** (Python, Go, Rust, C++, Java) — N threads/goroutines
  increment a shared counter `INCREMENTS` times each with no
  synchronization, then with a mutex, then with an atomic op. Compare
  final counts to the expected total.
- **`race.js`** (Node) — the same idea using `SharedArrayBuffer` +
  `worker_threads`, comparing a plain read-modify-write to `Atomics.add`.
- **Python's `race.py`** additionally runs a **check-then-act cache fill**:
  N threads all request the same uncached key; count how many times the
  "expensive" `compute()` function actually runs (should be 1).
- **Go's `cache_stampede/main.go`** runs the same check-then-act pattern
  against a plain Go `map`, specifically to surface the runtime's
  concurrent-map-write crash.

## How to run

```
python3 python/race.py
node nodejs/race.js
cd golang && go run race.go
cd golang/cache_stampede && go run main.go     # try it a few times -- outcome varies
cd rust/races && cargo run --release
g++ -O2 -std=c++17 -pthread -o /tmp/race cpp/race.cpp && /tmp/race
cd java && javac Race.java -d /tmp/javabuild && java -cp /tmp/javabuild Race
```

## What I saw

**Bare increment race** (8 threads/goroutines x 300,000 increments,
expected total 2,400,000):

| Language | unsafe result | lost updates | fixed (lock/atomic) |
|---|---|---|---|
| Python | 2,400,000 | **0** (see note below) | 2,400,000 |
| Node (SharedArrayBuffer) | 2,139,855 | 260,145 | 2,400,000 |
| Go | 1,708,938 | 691,062 | 2,400,000 (both mutex and atomic) |
| Java | 1,733,978 | 666,022 | 2,400,000 (both synchronized and AtomicLong) |
| Rust (unsafe raw pointer) | 2,400,000 | **0** (see note below) | 2,400,000 (both mutex and atomic) |
| C++ (plain, no `unsafe` needed) | 2,400,000 | **0** (see note below) | 2,400,000 (both mutex and atomic) |

Node, Go, and Java all show the race exactly as expected — real,
substantial, lost work, with Java's 666,022 lost updates landing in the
same range as Go's 691,062. Python, Rust, and C++ all came back clean, for
related but distinct reasons:

*Python:* re-tested with `sys.setswitchinterval(0.00001)` (far more
aggressive than the ~0.005s default) across multiple thread counts — still
0 lost updates on this box. The check-then-act experiment right below it
is the proof the danger is real; it is not a coincidence that it needs an
actual function call (and therefore a real yield point) to manifest
reliably.

*Rust and C++:* both re-tested at 300,000 *and* 20,000,000 increments per
thread with identical (clean) results both times — a strong hint that the
race window itself didn't get wider with more iterations, which is what
you'd expect if the optimizer partially transformed the loop under the
assumption that unsynchronized access "can't" happen (both languages'
memory models permit aggressive optimization of code that violates their
no-data-race rule, since by definition well-defined programs don't
violate it). This is worth sitting with: code that breaks these rules
doesn't just risk "wrong numbers sometimes" — the optimizer is allowed to
transform it in ways that can make the very bug you're trying to observe
*less* visible, not more. That's a stranger and arguably scarier failure
mode than a clean lost-update count would have been, and C++ reproducing
it too (with no `unsafe` keyword standing between you and the bug) is the
sharpest illustration in this lab of what Rust's compile-time enforcement
is actually buying you: not "C++'s hardware behaves differently," but
"Rust makes you spell `unsafe` before you can reach this exact hazard, and
C++ hands it to you by default."

**Check-then-act cache fill** (8 threads/goroutines racing for one key,
`compute()` should run exactly once):

| Language | unsafe | safe |
|---|---|---|
| Python | 8 calls (every thread computed) | 1 call |
| Go (plain map) | 8 calls, **or** `fatal error: concurrent map writes` (both observed across repeated runs) | 1 call |

**Go race detector** (`go run -race`) on a minimal 4-goroutine version
caught the race immediately and printed the exact two goroutines and lines
in conflict — this is worth running on real code you're unsure about; it's
far more reliable than trying to observe a race by eye.

## Answer before moving on

1. Why did the Node and Go bare-increment races produce large, obviously
   wrong numbers so reliably, while Python and Rust's versions of "the same
   bug" didn't show up as cleanly? What's actually different about how
   each of those four languages executes a tight loop?
2. Go's concurrent-map crash is a *fatal* error, not a catchable `panic`.
   Why might the Go team have decided that this specific failure mode
   shouldn't be recoverable, when so much else in Go is?
3. What would you have to do to make the Python or Rust bare-increment
   race reproduce reliably on this machine? (You don't have to implement
   it — just describe the change and why it would work.)

## Next up

Blocking vs non-blocking IO at the OS level — what a real socket read
looks like in each language, and what's actually happening underneath
"await".
