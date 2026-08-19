# Layer 4 · Topic 3 — Clocks lie

### The takeaway (read this first)

**The one idea:** wall-clock timestamps from two machines are not comparable,
and a wall clock can move *backwards* on one machine; anything that orders
events or measures durations with `time.time()` is a correctness bug waiting for
an NTP step.

**Why it matters in practice:** two distinct live problems. Last-write-wins on
client timestamps silently discards writes — no error, no log line, just a value
that reverted and a support ticket that says "it did not save." And if any of
your latency instrumentation measures spans with wall-clock time, a single NTP
correction produces a negative or absurd sample, and one such sample poisons a
p99 for the whole window. Before chasing a latency spike, rule out that you
measured it wrong.

**You'll know it landed when:** given two timestamps from two machines you say
"I can conclude nothing about their order" without hesitating, and you reach for
a monotonic clock for durations and a database or logical clock for ordering
without having to decide.

## The concept

There are at least three different clocks in every operating system and they
answer different questions.

`CLOCK_REALTIME` is the wall clock. It is **settable** — by an administrator, by
a VM's host on resume, and routinely by NTP. NTP either *slews* it (adjusts the
rate so it converges smoothly) or *steps* it (jumps it, possibly backwards)
depending on how far off it is. It is the only clock that means anything across
machines, and it is the only one that can go backwards.

`CLOCK_MONOTONIC` never goes backwards and is meaningless across machines or
across reboots. It measures elapsed time since some unspecified point. On Linux
it also does not advance while the machine is suspended, which is what
`CLOCK_BOOTTIME` is for. **On Darwin there is no `CLOCK_BOOTTIME`** — the
equivalents are `CLOCK_MONOTONIC_RAW` (unslewed) and `CLOCK_UPTIME_RAW` (which
excludes sleep). If you find a `CLOCK_BOOTTIME` in code that has to run on this
machine, it either does not compile or it silently fell back to something else.

Different tools, different jobs, and this whole topic is one mistake: using the
wrong one.

**Skew and the error bound.** Ordinary VM clocks drift tens of milliseconds
apart as a matter of course. AWS's published figures for Amazon Time Sync with
a PTP hardware clock claim accuracy in the tens of microseconds, extended to
more EC2 instance types through 2026 via Precision Time Placement Groups. But
accuracy is not the number that matters. The number that matters is the **error
bound**, and the interface that gives it to you is **ClockBound**, which returns
an *interval* `[earliest, latest]` rather than a point. If two intervals
overlap, the two events are unordered and you must not pretend otherwise. That
is exactly Spanner's TrueTime idea and it is the only honest way to order events
with a physical clock. Everything else is a bet on your drift being smaller than
your event spacing.

**Logical clocks sidestep the physics entirely.** A **Lamport clock** — a
counter per node, bumped on every event and carried on every message, taking
`max(local, received) + 1` on receipt — gives a total order consistent with
causality, but it cannot tell you whether two events were *concurrent* or
genuinely ordered. A **vector clock** (one counter per participant) can detect
concurrency, at the cost of growing with the number of participants. That growth
is why nobody puts a vector clock in a column of a table with a million clients.

The practical middle, and the one you will actually ship: a per-row
monotonically increasing `version` with a compare-and-set —
`UPDATE ... SET version = $n+1 WHERE id = $1 AND version = $n` — or letting the
database be the single clock, since `now()` inside a transaction is the
transaction start time from one machine, consistently.

## How each language actually gets there

**All six.** Which clock a duration reads is a property of the runtime, not of
the problem, and the six runtimes here range from "the type system will not let
you write the bug" to "the API with `high_resolution` in its name is the one
that can go backwards." That spread is the lesson.

**Python — three functions, one of them wrong for durations.** `time.time()` is
realtime and steppable; `time.monotonic()` is monotonic; `time.perf_counter()`
is monotonic with the highest resolution the platform offers. Use the latter two
for every span you time. Also `datetime.utcnow()` is deprecated from 3.12 — use
`datetime.now(UTC)`, which at least carries a timezone so the bug is visible.
`time.clock_gettime(time.CLOCK_MONOTONIC_RAW)` works on Darwin;
`time.CLOCK_BOOTTIME` does not exist there and raises `AttributeError`.

**Node.js — three again, with a subtlety.** `Date.now()` is realtime;
`performance.now()` is monotonic with sub-millisecond resolution;
`process.hrtime.bigint()` is monotonic nanoseconds. The subtlety worth knowing:
`performance.timeOrigin` is a *wall-clock* anchor captured at process start, so
`timeOrigin + performance.now()` is a wall-clock estimate and inherits every
wall-clock hazard, while `performance.now()` on its own does not. Mixing the two
in one calculation is how a monotonic measurement gets contaminated.

**Go — right by default, until you touch it.** This is the most interesting
case in the set. A `time.Time` carries *both* a wall reading and a monotonic
reading, and `t2.Sub(t1)` silently uses the monotonic one, so idiomatic Go is
correct without the author knowing why. But the monotonic reading is
**stripped** by `t.Round()`, `t.Truncate()`, `t.UTC()`, `t.Local()`,
`t.AddDate()`, JSON marshalling and unmarshalling, and any `Time` produced by
`time.Parse`. A duration computed after any of those silently falls back to the
wall clock and can come out negative. The program here computes the same span
both ways to show the two paths diverging.

**Rust — the type system refuses the bug.** `Instant` is monotonic and there is
deliberately **no way to get a calendar date out of it**; `SystemTime` is the
wall clock and its `duration_since` returns a `Result`, because the answer can
legitimately be "that was in the future." You cannot accidentally subtract a
wall time and get a `Duration` — you get a `Result<Duration, SystemTimeError>`
and have to say, in code, what you want to happen when time went backwards.
Compare that with every other language here, where the same expression compiles
and returns a plausible lie.

**C++ — the clock with the reassuring name is the unsafe one.**
`std::chrono::system_clock` is wall time; `std::chrono::steady_clock` is
monotonic and exposes `is_steady == true` as a compile-time constant you can
print. `std::chrono::high_resolution_clock` is **implementation-defined** and is
a typedef for one of the other two — on some standard libraries it is
`system_clock`, meaning the clock most people reach for when they want precision
is the one that can step backwards. The program prints
`is_same_v<high_resolution_clock, system_clock>` on your toolchain so you find
out rather than assume. C++ is also the one that calls `clock_gettime` directly,
which is where the Darwin/Linux difference above stops being trivia.

**Java — two clocks, and an injectable one.** `System.currentTimeMillis()` is
wall time; `System.nanoTime()` is monotonic and comparable **only within one
JVM** — comparing `nanoTime` values across processes is meaningless even on the
same host. `Instant.now()` reads the wall clock, so a `Duration.between` of two
`Instant`s is wall-clock arithmetic wearing a type. Java's genuinely nice
answer is `java.time.Clock`: pass one in, and a test can supply
`Clock.fixed(...)` or a stepping clock, so the skew experiment needs no
`LD_PRELOAD` at all in this language.

## The experiment

**Part A — the six-language clock audit, no infrastructure.** Each program times
the same operation with that runtime's wall clock and its monotonic clock while
an offset is injected through the application's own `now()` function, then
prints the runtime-specific footgun in that list: Go's stripped monotonic
reading, Rust's `Err` from `duration_since`, C++'s `high_resolution_clock`
identity, Java's `nanoTime` scope, Node's `timeOrigin`, Python's deprecated
`utcnow`. Nothing here needs root, a container, or a modified system clock.

**Part B — lost updates from a 250ms skew.** Two application containers
(`writer-a`, `writer-b`) writing the same Postgres rows, with last-write-wins
resolved by a **client-generated** `updated_at`.

**Read this before building it — it is the Layer 1 lesson in miniature.** You
cannot skew one container's wall clock with a Docker flag. Linux time namespaces
(5.6+) virtualize `CLOCK_MONOTONIC` and `CLOCK_BOOTTIME` only; `CLOCK_REALTIME`
is deliberately not namespaced. And Docker Desktop on macOS is a single Linux
VM, so every container shares its clock regardless. Two honest options:
`libfaketime` via `LD_PRELOAD` (`FAKETIME=+250ms`) **inside a Linux container**,
or a `CLOCK_OFFSET_MS` environment variable read by a single `now()` function in
your application. **Prefer the app-level offset** — it is platform-independent,
identical on this Mac and in CI, and it makes the independent variable explicit
instead of magic.

Three variants: **v0** LWW on client timestamps with writer B offset `+250ms`,
counting writes accepted and then overwritten by a *logically older* write;
**v1** LWW on database-side `now()` under identical load, recounted; **v2**
compare-and-set on a `version` column, counting rejected CAS attempts (which are
correct behaviour, not errors) and the retry cost they add.

**Part C — the instrumentation half, which touches a live problem directly.** A
span-timing harness records the same operation twice, once with the wall clock
and once with the monotonic clock, while a background thread steps the
application's perceived wall clock mid-run. Compare the two p99s and count
negative samples.

## How to run

Part A needs nothing running:

```
python3 python/clock_audit.py
node nodejs/clock_audit.js
cd golang && go run clock_audit.go && cd ..
cd rust/clock_audit && cargo run --release && cd ../..
g++ -O2 -std=c++17 -Wall -Wextra -o /tmp/l4t3_cpp cpp/clock_audit.cpp && /tmp/l4t3_cpp
javac java/ClockAudit.java -d /tmp/javabuild && java -cp /tmp/javabuild ClockAudit
```

Each of the six prints the same four sections — clock inventory with **measured**
resolution, one span timed twice through a stepping application clock, that
runtime's own footgun, and a one-line summary you copy into the record table
below. None of them touches the system clock; the step is an offset inside the
application's `now()`, for the reason in [`../lab/`](../lab/README.md).

Part C — the instrumentation half — also needs nothing running:

```
python3 tools/clock_span_harness.py --step-ms 40000
python3 tools/clock_span_harness.py --step-ms 250 --steps 20 --workers 4 --window 50
```

It refuses to print a clean table if no step landed inside a span, because that
run proves nothing. The per-window p99 block is the part to read: over a whole
run a handful of poisoned samples cannot move a p99 by rank, and over a scrape
interval they own it. A dashboard plots the second one.

Part B has a local mode. It is **two writer threads against whatever Postgres is
listening**, not two containers under k6 — but the skew is application-level in
both versions (it has to be; see [`../lab/`](../lab/README.md)) and the loss
needs only two writers contending on one row, so the correctness result is the
same one. What it does not reproduce is network delay between writer and
database, which changes the *rate* of collisions and not their existence.

```
python3 python/lww_writers.py --variant v0 --offset-ms 250
python3 python/lww_writers.py --variant v0 --offset-ms 0      # the control
python3 python/lww_writers.py --variant v1 --offset-ms 250
python3 python/lww_writers.py --variant v2 --offset-ms 250
psql -d sep_lab_04_dist -f sql/topic3_lost_updates.sql
```

Run the `--offset-ms 0` control. Without it a lost-update count is a number with
nothing to compare against, and you cannot tell skew from ordinary contention.
Teardown for the whole layer: `python3 ../lab/local/teardown_lab.py`.

Part B under compose (blocked while the Docker daemon is down —
`python3 ../lab/local/check_env.py`):

```
CLOCK_OFFSET_MS=250 docker compose up -d --force-recreate writer-b
docker compose run --rm k6 run /scripts/topic3_lww.js
psql -d sep_lab_04_dist -f sql/topic3_lost_updates.sql
```

The `libfaketime` route, if you want a genuinely stepped `CLOCK_REALTIME` rather
than an application-level offset, is **Linux-only and therefore container-only**
on this machine:

```
docker compose run --rm \
  -e LD_PRELOAD=/usr/lib/aarch64-linux-gnu/faketime/libfaketime.so.1 \
  -e FAKETIME=+0.25 writer-b python3 -m app.writer
```

Two things about that command, both measured inside the container on
2026-08-19 rather than assumed:

- **`FAKETIME=+250ms` does not mean 250 milliseconds.** libfaketime's offset
  suffixes are `m`/`h`/`d`/`y`; it reads `250ms` as `250m` and ignores the `s`.
  Measured: `+250ms` and `+250m` both produce **+15000.0 s** — 250 minutes,
  60,000x the intended skew. A sub-second offset has to be written as a
  fractional number of seconds: `+0.25` measured **+0.25 s**.
- **The library is not at `/usr/lib/faketime/`** on a Debian arm64 image; it is
  under the multiarch directory, `/usr/lib/aarch64-linux-gnu/faketime/`. Use
  `$(ls /usr/lib/*/faketime/libfaketime.so.1)` if you want it portable.

Confirmed in the same run: under `FAKETIME=+0.25`, `time.time()` moves and
`time.monotonic()` does not. That asymmetry is the entire reason Topic 7's
lease timers use the monotonic clock.


## Predict, then record

**Predict first, in writing:** at a 250ms offset with two writers contending on
10 keys at roughly 50 writes/sec each, what fraction of writes is lost under v0?
Does v1 lose any? Under v2, what is the ratio of rejected CAS attempts to
successes, and does it depend on the offset at all?

| Variant | Offset (ms) | Writes issued | Lost updates | Rejected CAS | p99 write (ms) |
|---|---|---|---|---|---|
| v0 client-ts LWW | 0 | | | — | |
| v0 client-ts LWW | 250 | | | — | |
| v1 db `now()` LWW | 250 | | | — | |
| v2 version CAS | 250 | | | | |

| Span timing | p50 | p99 | max | negative samples |
|---|---|---|---|---|
| wall clock | | | | |
| monotonic clock | | | | |

| Runtime | monotonic clock used | resolution measured | footgun reproduced? |
|---|---|---|---|
| Python | | | |
| Node.js | | | |
| Go | | | |
| Rust | | | |
| C++ | | | |
| Java | | | |

**What would mean the experiment is broken, not the prediction wrong:**

- **Zero lost updates at a 250ms offset.** Your writers almost certainly are not
  colliding. Two writers spread over a large key space at a low rate may never
  touch the same row inside a 250ms window. Shrink to ~10 keys and raise the
  rate until collisions are frequent; only then does "zero" mean anything.
- **You tried a Docker or namespace trick to skew `CLOCK_REALTIME` and nothing
  changed.** That is not your setup failing. `CLOCK_REALTIME` is genuinely not
  namespaced, and Docker Desktop is one VM. Use the app-level offset, or
  `libfaketime` inside the container.
- **v1 loses updates too.** Your "database-side `now()`" is probably being
  computed in the application and passed as a parameter, or you used
  `clock_timestamp()` where you meant `now()`. Read the generated SQL, not the
  ORM call.
- **The wall-clock span harness shows no corruption.** The step has to land
  *inside* a span. Lengthen the spans or step more often, and confirm the offset
  hook applies to the clock the harness actually reads.
- **Go's stripped-monotonic demo shows identical durations both ways.** You are
  probably comparing two `Time` values that both still carry their monotonic
  readings. The strip has to happen between the two calls — round-trip one of
  them through `.UTC()` or JSON and compare again.

## Answer before moving on

1. Machine A logs `10:00:00.500`, machine B logs `10:00:00.400`. What can you
   conclude about the order of the two events, and what single extra piece of
   information would let you conclude something?
2. Why is a vector clock impractical as a column in a table with a million
   distinct clients, and what do real systems use instead?
3. Go: name three operations that silently strip the monotonic reading from a
   `time.Time`, and describe the bug that follows in the code that runs next.
4. Rust makes `duration_since` return a `Result` and C++ names the unsafe clock
   `high_resolution_clock`. Which design produces fewer bugs in a large
   codebase, and what does the safer one cost the person writing it?
5. Your p99 dashboard shows a 40-second spike at the same time each week. What
   do you check about clocks and instrumentation *before* you look at
   application code?

## Next up

[Topic 4 — Consistency models and the replica lag you already have](../04-consistency-models-and-replica-lag/README.md):
what a reader is actually allowed to observe, and the replica somebody added for
performance that quietly changed the answer.
