# Layer 1 · The machine underneath

## Topic 1: Memory — stack vs heap, cache locality

### The takeaway (read this first)

**The one idea:** the physical cost of reading memory depends on the *order* you read it in, not just how many reads you do — visiting the same amount of data sequentially vs. scattered can differ by 40-60x, with zero change to the algorithm's Big-O.

**Why it matters in practice:** this is the actual mechanism behind "an array can beat a linked list even with worse Big O," and it shows up anywhere data gets scattered across memory over time — linked structures, hash tables, object graphs, deserialized JSON turned into nested objects. Big-O counts operations; it doesn't count what each operation costs, and this is the single biggest hidden cost most engineers never learn to see.

**You'll know it landed when:** you can look at two pieces of code that do the same number of operations and correctly guess, before running either, which one will be slower — because you can see which one walks memory in order and which one jumps around.

Stack vs. heap (below) is necessary background for *why* heap data ends up scattered in the first place — it is not itself what the experiment measures. The experiment only ever touches heap memory; the variable is purely the order you visit it in.

### The concept

Two kinds of memory matter for how fast your code runs:

The **stack** holds local variables and function call frames. Allocation is just moving a pointer — push a frame, pop a frame. It's fast because it's predictable: the compiler knows the size and lifetime of everything on it at compile time (with some care in languages that let you take addresses of locals).

The **heap** holds everything with a lifetime the compiler can't predict statically — objects that outlive their creating function, things sized at runtime, anything you explicitly allocate. Every heap allocation costs real work: find free space, maybe ask the OS for more, maybe trigger a GC pause later. And critically, heap objects can end up *anywhere* in memory, laid out in whatever order they happened to be allocated.

That last part is why **locality dominates**. Your CPU doesn't read from RAM one byte at a time — it pulls a whole cache line (typically 64 bytes) into L1/L2/L3 cache whenever it touches an address, betting that you'll want the neighboring bytes soon. Walk through memory sequentially and that bet pays off constantly: the next value you need is already in cache. Walk through memory via pointers scattered randomly (which is exactly what a linked list of heap-allocated nodes tends to look like once it's been built incrementally) and every single step is a cache miss — a round trip to main memory that costs on the order of 100+ ns, while an L1 hit costs \~1 ns.

This is the concrete mechanism behind "an array can beat a linked list even with worse Big O": Big O counts operations, not the cost of each operation, and the cost of "read the next element" can differ by two orders of magnitude depending on where that element physically lives.

### The experiment

`python/locality.py`, `nodejs/locality.js`, `golang/locality.go`, `rust/locality/`, `cpp/locality.cpp`, and `java/Locality.java` all implement the exact same benchmark: **pointer chasing**. Build N nodes as parallel arrays (`values[i]`, `next[i]`). Two layouts of the same logical traversal (visit every node once per lap, same number of laps, same amount of arithmetic):

-   `sequential` — node i's successor is stored at index i+1. Walking it walks memory in physical order.
-   `shuffled` — node i's successor is a random other node. Walking it jumps to a random address every step, with no way for the CPU to prefetch, because each jump target depends on the value just read (a genuine dependency chain, not something the CPU can predict or pipeline around).

Both layouts do the identical number of additions. The only difference is memory access pattern. Run each and compare `ns/step` for sequential vs shuffled.

### How to run

```         
python3 python/locality.py
node nodejs/locality.js
cd golang && go run locality.go
cd rust/locality && cargo run --release
g++ -O2 -std=c++17 -o /tmp/locality cpp/locality.cpp && /tmp/locality
cd java && javac Locality.java -d /tmp/javabuild && java -cp /tmp/javabuild Locality
```

### Gotcha: "repeated runs get cheaper" — but not the part you're measuring

If you noticed `go run locality.go` and the `javac ... && java ...` pair feel faster the second and third time you run them, that's real, and it has nothing to do with cache locality — it's the toolchain warming up *around* the benchmark, not the benchmark itself. Confirmed by timing the whole command, not just the internal `ns/step` numbers the program prints:

```         
$ go clean -cache            # force a genuinely cold build
$ time go run locality.go    # attempt 1: 8.865s
$ time go run locality.go    # attempt 2: 0.772s
$ time go run locality.go    # attempt 3: 0.751s
```

That's `go run` recompiling the entire standard library your program pulls in (`fmt`, `math/rand`, `time`, the runtime itself) from scratch on the first invocation, then reusing Go's on-disk build cache (`$GOCACHE`, typically `~/.cache/go-build` or `~/Library/Caches/go-build` on macOS) on every subsequent run of the *same unchanged source* — an 11x difference here, entirely before your program's `main()` even starts. `cargo run` has the identical cache for Rust, for the same reason; this lab's other "how to run" commands for Go and Rust will show the same first-run tax the very first time you use them.

Java shows a smaller version of the same category of effect, but from a different mechanism: javac has no cross-invocation cache like Go's, so compilation cost is roughly constant, but the JVM's own launcher and the JDK's class/module files benefit from the OS's page cache once they've been read from disk once in a session:

```         
$ java -cp build Locality    # attempt 1: 1.080s wall time
$ java -cp build Locality    # attempt 2: 0.985s
$ java -cp build Locality    # attempt 3: 0.931s
$ java -cp build Locality    # attempt 4: 0.903s
```

Smaller effect than Go's (roughly 15-20% here vs. Go's 11x), because it's just "some files came from RAM instead of disk this time," not "we skipped recompiling anything." This is also why Python and Node don't show it noticeably (their interpreters are small and already close to fully warm after the first invocation of *any* script) and why Rust and C++ don't show it in this lab's instructions at all — we compile them once with `cargo build`/`g++` and run the resulting binary directly, so there's no recompilation step left to get cheaper on repeat.

The `ns/step` numbers the program itself prints, by contrast, stay stable run over run — check `/tmp/javaout.txt`-style captures across repeated runs above and you'll see 1.8-1.9 ns/step sequential every time, cold JVM or not. That's the actual thing this lab is trying to isolate, and it's worth building the habit now: when a number changes between runs, ask first whether you changed what's being measured, or just changed how long it took to *start* measuring it. Topic 2's syscall/process-cost experiments are exactly about that second category of cost.

### What I saw (sandbox run, N=2,000,000, 5 laps)

| Language | sequential   | shuffled      | slowdown |
|----------|--------------|---------------|----------|
| Python   | 67.9 ns/step | 592.7 ns/step | 8.7x     |
| Node     | 2.8 ns/step  | 64.2 ns/step  | 23x      |
| Go       | 1.9 ns/step  | 72.2 ns/step  | 38x      |
| Java     | 1.9 ns/step  | 84.9 ns/step  | 44.7x    |
| Rust     | 1.7 ns/step  | 111.0 ns/step | 65x      |
| C++      | 1.8 ns/step  | 107.7 ns/step | 59.8x    |

Run it yourself — absolute numbers depend entirely on your machine's cache sizes and memory speed, but the shape should hold.

### Answer before moving on

1.  **Why does this happen?** (two sentences, in your own words — what is the CPU actually doing differently between the two runs?)
2.  **What would break it** — i.e. what change to the benchmark would shrink or erase the gap between sequential and shuffled? (Think about what you'd have to change about N, or about the machine, or about the access pattern itself.)

### Common misconception worth clearing up

The natural first guess is "this is a stack vs heap experiment" — it isn't. **Both** `sequential` and `shuffled` allocate their arrays on the heap (a Python `list`, a Java `int[]`, a `Vec<i32>` — all heap objects). Nothing here ever touches the call stack. The stack/heap material earlier in this README explains *why* heap objects can end up scattered in memory in the first place; this specific experiment is entirely about what happens once you're reading from already-allocated memory, in two different *orders*.

It's also not about the CPU "randomly checking RAM for where memory is stored" — the CPU's behavior is identical and fixed in both runs: touch an address, and the hardware pulls in the 64-byte cache line containing it, on the bet that nearby addresses will be wanted next. What differs between the two runs is only the *data's own layout* relative to the order you visit it in. In `sequential`, address N+1 is right next to address N, so that same fetched cache line already contains your next several steps — free hits. In `shuffled`, the next address is unrelated to the current one, so almost every step needs a cache line that hasn't been fetched yet — a real trip out to main memory, roughly 100+ ns away instead of \~1 ns.

Garbage collection (Rust's absence of one, Python/Node/Java/Go all having one) is a real and interesting axis, but it's a different one: GC is about *who decides when memory gets freed and reused*, not about *whether today's layout is cache-friendly*. It's not entirely unrelated, though — worth knowing as a bonus fact: some collectors (Java's G1, Node's V8) are *compacting/copying* collectors that periodically move live objects closer together in memory during a collection pass, which can incidentally *improve* locality for long-lived data. That's a nice side effect, not the mechanism this experiment is measuring.

**On "what would break it":** running out of memory entirely is a different failure (allocation fails, or the OS kills the process) — not what makes shuffled slow. The thing that actually shrinks or erases the gap is *scale relative to cache size*: if `N` were small enough that the whole array fit inside L2/L3 cache, then even the shuffled order would already be sitting in cache after the first lap, and there'd be nothing slow about visiting it out of order. Try dropping `N` from 2,000,000 down to something like 10,000 in any of the four language versions and re-run — the gap should shrink dramatically, because you've stopped forcing trips out to main memory at all.

### Follow-up: dropping N to 10,000 in Java did something *weirder* than "shrink" — here's why

Real result from testing this prediction on Java, `N=10_000`, unmodified `LAPS=5`:

```         
sequential  total=249975000  time=0.001s  28.4 ns/step
shuffled    total=249975000  time=0.000s   4.4 ns/step
```

Shuffled came back *faster* than sequential — the opposite of the prediction, and it wasn't even consistent between runs (a repeat showed them roughly equal). Two real things are stacked on top of each other here, both worth understanding rather than shrugging off as "noise":

**First, the actual answer to the prediction is correct — the gap really did shrink, hard.** At `N=10,000`, both arrays together are 80KB, which fits comfortably inside L2 cache (typically 256KB-1MB) even though it's bigger than L1 (typically 32-48KB). Confirmed by scaling `LAPS` up to 1,000 (keeping the *same* `N=10,000`, just running enough laps that the measurement window is long enough to trust):

```         
N=10000 laps=1000 (total steps=10000000)
sequential  1.82-2.02 ns/step
shuffled    3.62-3.90 ns/step
```

Consistent, reproducible, and roughly a 2x gap — down from \~45x at `N=2,000,000`. That's exactly the prediction: shrink `N` enough to fit in a fast cache tier and the gap collapses. It doesn't go all the way to 1.0x because 80KB still doesn't fit in the *smallest, fastest* tier (L1), so shuffled still eats a bit more L2-latency traffic than sequential — but it's nowhere near the "trip to main memory" penalty anymore.

**Second, the original `LAPS=5` run at `N=10,000` wasn't measuring that 2x gap at all — it was measuring noise, and the inversion is the tell.** At `N=10,000, LAPS=5` the timed section is only 50,000 total steps, finishing in a handful of *microseconds*. At that duration, a single JIT compiler event, a young-generation GC pause, or the OS simply context-switching this thread out for a moment costs more time than the entire measurement — so whichever run happens to dodge one of those gets to "win," independent of which access pattern it was testing. This is a general rule worth keeping, in any language: **if shrinking your input also shrinks your benchmark's wall-clock duration down into the microsecond range, you're no longer measuring your algorithm — you're measuring your OS scheduler, your GC, and your JIT.** The fix is what the 1,000-lap version does: keep the total work large enough (tens of milliseconds or more) regardless of how small `N` gets, by scaling `LAPS` up to compensate. As a rule of thumb for this benchmark specifically: keep `N * LAPS` around 10,000,000 or more.

**The `OutOfMemoryError` at `N=2,000,000,000`** is a different, much simpler thing, and not a bug: 2 billion `int`s at 4 bytes each is 8GB, and this benchmark allocates *two* such arrays (`values` and `next`) — 16GB, comfortably past the JVM's default heap ceiling (usually a fraction of the machine's physical RAM, not all of it). This is the exact same shape of failure as Topic 6's "too many open files": a real, named resource limit (heap size here, file descriptor table there), hit exactly where the error message says it was hit, fixable by explicitly raising the limit if you actually need it (`java -Xmx20g -cp build Locality`, though that requires actually having 20GB+ free) rather than something to work around. For testing "does the gap keep growing at even larger N," a value like 200,000,000 (200 million, 1.6GB per array) will show you a much bigger array than the original without needing any JVM flags at all.

### The cross-language tell worth noticing

Look at the *ratio*, not just the absolute times. Python's slowdown is only \~8.7x, while C++'s is \~60x. That's not because Python has better cache behavior — it's because Python's per-step interpreter overhead (bytecode dispatch, refcounting, boxing) is already so large that a 100ns cache miss is a smaller fraction of the total. C++, Rust, and Go get so close to the raw hardware limit on the sequential run (\~2 ns/step, close to a single cache hit) that the cache miss cost shows up almost undiluted. This is itself a lesson from Layer 1: interpreter/runtime overhead doesn't just make things slower, it *masks* other effects you'd otherwise be able to see and reason about.

Java sits in an interesting middle position: 1.9 ns/step sequential — as fast as Go or C++ — because HotSpot's JIT compiles the hot traversal loop down to native machine code indistinguishable in kind from what g++ produces. This is the point of the warm-up pass in `Locality.java`: without it, you'd be timing the interpreter *and* the JIT's compilation pauses, not steady-state performance. C++ has no such warm-up requirement — it's native code from the moment `main` starts — which is its own lesson about the tradeoff Java is making (pay some warm-up cost and carry a GC, in exchange for not needing to know your deployment target's instruction set at compile time).

### Next up

Processes, threads, and context switches — what a syscall actually costs. Say the word when you're ready and we'll build that one.