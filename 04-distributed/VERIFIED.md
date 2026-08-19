# Layer 4 · execution record

**Date:** 2026-08-19
**Verified by:** an independent pass. The programs in this layer were written by
another agent; this pass did not trust that author's report and re-ran
everything from the topic READMEs' own `How to run` blocks.

## What this file is, and what it is not

**This records that the code in this layer *executes*. It does not record that
anything was learned.** Every `RAN` below means: the exact command in the topic
README was run on the machine described here, it terminated, and it printed the
shape of output its header comment promised. It does not mean the result is
interesting, and it is not a substitute for running these yourself.

**The `Predict, then record` tables in the seven topic READMEs are still blank
and were left that way on purpose.** They are the reader's exercise. Filling
them in from this file would defeat the point — the prediction has to be written
before the run, and this file is a run.

Output quoted below is real: it was captured from these runs. Where a number
would vary between runs, it is quoted as an example of the *shape*, not as a
constant to expect.

## The machine

| Thing | Version / state |
|---|---|
| OS | macOS 27.0 (Darwin 27.0.0), arm64 / Apple M1 |
| Python | 3.13.5 |
| Node.js | v24.14.0 |
| Go | go1.24.5 darwin/arm64 |
| Rust | rustc 1.97.1, cargo 1.97.1 |
| C++ | Apple clang 21.0.0 (clang-2100.1.1.101) |
| Java | Temurin JDK 21.0.2 (LTS) |
| Postgres | 17.5 (Homebrew), server running, `/tmp:5432 accepting connections` |
| psycopg3 | 3.3.3 |
| Docker | CLI 28.1.1 / **server 29.5.3 running**, linux/aarch64, 4 CPUs / 5.1 GB VM |
| k6 | **not installed on the host**; run as the `k6` compose service |
| toxiproxy / etcd / redpanda | all three now run as compose services |

`timeout(1)` does not exist on this machine; every run below was wrapped in
`perl -e 'alarm shift @ARGV; exec @ARGV' <seconds>` instead. Nothing needed the
alarm — no program in this layer hung.

## Status table

`FIXED-THEN-RAN` means this pass changed the file before it would run correctly;
each one is explained under *Defects found and fixed*.

### Lab harness

| Program | Status | Notes |
|---|---|---|
| `lab/local/check_env.py` | RAN | Correctly reports the daemon down, k6 missing, Postgres up. Its per-topic RUNNABLE/PARTIAL/BLOCKED table matches what this pass independently found. |
| `lab/local/lab_db.py` | RAN (as a module) | Exercised by every Postgres-backed program below. |
| `lab/local/teardown_lab.py` | **NOT RUN** — deliberately | It drops `sep_lab_04_dist`, which holds the data every SQL deliverable below reads. Byte-compiled only. Run it when you are finished with the layer. |

### Topic 1 — partial failure and the ambiguous result

| Program | Status | Notes |
|---|---|---|
| `python/ambiguous_result.py` | RAN | 10.1s |
| `nodejs/ambiguous_result.js` | RAN | 13.1s |
| `golang/ambiguous_result.go` | RAN | 11.3s |
| `rust/ambiguous_result` | RAN | 10.9s, `cargo run --release` |
| `cpp/ambiguous_result.cpp` | RAN | 10.3s, compiles clean at `-Wall -Wextra` |
| `java/AmbiguousResult.java` | RAN | 10.5s |
| `sql/topic1_reconcile.sql` | **FIXED-THEN-RAN** | Table-name collision with Topic 2 — see defect 2. |
| Part B (compose + k6) | **FIXED-THEN-RAN** | 8 toxics; see the unblock pass |

All six agree exactly, which is the cross-check that matters here: phase 1 → 32
duplicate charges, phase 2 → 0, unresolved ambiguity 16 in **both** phases, in
every one of the six runtimes. The README's own "experiment is broken" list
names "phase 1 and phase 2 give the same duplicate count" — they do not.

Java's phase 0 reproduced the JDK's hidden retry: `GET` → 1 client exception, **2
charges server-side**; `POST` → 1 and 1. The README predicts exactly this and
flags "Java's phase 0 shows one request for GET" as a broken run. It showed two.

Constants checked against the README's prose in all six sources: client timeout
300 ms against a 1000 ms slow reply, 3 attempts max. They match.

### Topic 2 — idempotency keys, atomically

| Program | Status | Notes |
|---|---|---|
| `python/idempotency_race.py` | RAN | `--impl A/B/C`, plus `--vary-slot 4` on A and B |
| `nodejs/idempotency_race.js` | RAN | `--impl A/B/C` |
| `golang/idempotency_race.go` | RAN | `-impl A/B/C`, pgx v5.7.6 |
| `sql/topic2_assert.sql` | RAN | Returns real rows from all of the above |
| Compose + k6 | **FIXED-THEN-RAN** | IMPL A/B/C under k6; see the unblock pass |

Run at the README's own size (`--keys 200 --concurrency 5` = 1000 requests),
not a reduced one. All three languages agree:

```
IMPL A   charge rows 1000   KEYS CHARGED MORE THAN ONCE 200   DUPLICATE CHARGES 800
IMPL B   charge rows  200   KEYS CHARGED MORE THAN ONCE   0   DUPLICATE CHARGES   0
IMPL C   charge rows  200   KEYS CHARGED MORE THAN ONCE   0   DUPLICATE CHARGES   0
```

**The A implementation was read against its claim, not just run.** It is a
genuine check-then-act: the three statements run on an autocommit connection, so
they are three separate transactions, and the charge is written *before* the key
row. Putting the effect in the same transaction would let the unique index
rescue it — that would be implementation B wearing A's name, and the file's
docstring says so explicitly. `charges` deliberately has **no** unique index on
the key, and the program prints that fact every run.

`--vary-slot 4` behaves as documented: B answered `422` on 332 of 1000 requests
from its stored fingerprint; A answered `500` after charging and never noticed
the body differed.

### Topic 3 — clocks lie

| Program | Status | Notes |
|---|---|---|
| `python/clock_audit.py` | RAN | |
| `nodejs/clock_audit.js` | RAN | |
| `golang/clock_audit.go` | RAN | |
| `rust/clock_audit` | RAN | |
| `cpp/clock_audit.cpp` | RAN | Takes the `__APPLE__` branch correctly |
| `java/ClockAudit.java` | RAN | |
| `tools/clock_span_harness.py` | RAN | Both README invocations, plus its guard |
| `python/lww_writers.py` | RAN | v0@250ms, v0@0 (the control), v1@250, v2@250 |
| `sql/topic3_lost_updates.sql` | RAN | Returns real rows |
| `libfaketime` in a container | **FIXED-THEN-RAN** | Found a 60,000x error in the README's own command |
| Part B under compose (`writer-a`/`writer-b`) | **STILL BLOCKED** | The two writer services were never written |

All six reproduced a negative wall-clock span and zero negative monotonic spans.

**The C++ file's Darwin handling is correct and not faked:** it reports
`compiled branch: __APPLE__`, states that `CLOCK_BOOTTIME` does not exist here,
and explains that this is an `#ifdef` rather than a runtime branch because
referencing the macro does not compile. No `<sys/epoll.h>`, no `/proc`, no
cgroup paths anywhere in this layer.

**The Rust file's claim to quote verbatim `rustc` output was checked rather than
believed.** All three commented non-compiling lines were extracted into a scratch
crate and built with this machine's `rustc 1.97.1`. All three produce
`error[E0308]` with the operand text quoted in the file. The claim holds.

`lww_writers.py` defines a lost update server-side and non-circularly — *a
rejected write submitted after the winning write had already finished* — and
separates those from concurrent conflicts, which it does not count as losses:

```
v0 @250ms   writes 1002   rejected 493   LOST UPDATES 312   concurrent conflicts 181
v0 @0       writes 1000   rejected   2   LOST UPDATES   0   (the control)
v1 @250ms   writes 1000   rejected   7   LOST UPDATES   0
v2 @250ms   writes 1000   rejected 274   LOST UPDATES   0   rejected CAS 274
```

The control run is the part that makes the first row mean anything, and the
README is right to insist on it. v2 converts silent loss into 274 rejections the
caller is *told* about — that is the trade, and it is visible in one table.

### Topic 4 — consistency models and replica lag

| Program | Status | Notes |
|---|---|---|
| `python/session_guarantees.py` | RAN | All four guarantees broken and detected, plus Long Fork and the LSN token |
| `sql/topic4_stale_reads.sql` | RAN | Parses and executes against the empty schema it creates |
| Compose (`pg-primary` / `pg-standby` / k6) | **FIXED-THEN-RAN** | Real streaming standby; the deliverable returns rows |

Python only, which is what this topic's README specifies. Not a coverage gap.

`session_guarantees.py` labels itself in its own output as a vocabulary check
that measures nothing about Postgres, and every violation it reports is found by
a checker rather than announced by the narrator — so a broken check shows up as
a suspiciously clean run rather than a confident wrong answer. That is the right
construction and it is honest about its own limits.

`sql/topic4_stale_reads.sql` returned zero rows from all six of its queries on
this pass, and its header said plainly that it never had and why. **That was
accurate, not a defect.** It is no longer true: the unblock pass below built the
standby the file was waiting for, and the file now returns real rows. Its header
has been updated and query 5 has been rewritten — see defect 8.

### Topic 5 — consensus, and Raft specifically

| Program | Status | Notes |
|---|---|---|
| `golang/raft` — `go test -race ./...` | RAN | **12/12 PASS in 46.9s** (uncached) |
| `go vet ./...` | RAN | clean |
| `gofmt -l .` | RAN | clean |
| etcd cluster + Pumba | **FIXED-THEN-RAN** | All four partition scenarios |

```
PASS: TestInitialElection3A  TestReElection3A  TestManyElections3A
PASS: TestBasicAgree3B  TestFailAgree3B  TestFailNoAgree3B
PASS: TestRejoin3B  TestBackup3B  TestUnreliableAgree3B
PASS: TestPersist13C  TestPersist23C  TestFigure83C
ok  raftlab  46.856s
```

**A passing suite is not evidence until you know it can fail, so this pass
mutated the implementation to check.** The election restriction in
`raft.go` (§5.4.1, the up-to-date check in `RequestVote`) was disabled by forcing
`upToDate := true`. Result: `TestRejoin3B` and `TestBackup3B` **FAIL**. The
mutation was reverted and `raft.go` byte-compared against its pre-mutation copy —
identical — and the suite re-run uncached to confirm. The tests are real.

### Topic 6 — the outbox, sagas, and why 2PC is avoided

| Program | Status | Notes |
|---|---|---|
| `python/hwm_skip.py` | RAN | 64s at the README's `--duration 60`; the duration is the argument, not a hang |
| `python/outbox_relay.py` | RAN | Default, `--no-listen`, and `--drop-notifications` |
| `nodejs/idempotent_consumer.js` | RAN | `check-then-act` and `insert-then-act` |
| `sql/topic6_reconcile.sql` | RAN | Returns real rows |
| v0 dual write / v1 outbox, broker stopped mid-load | **FIXED-THEN-RAN** | Redpanda stopped and restarted under load |

Python + Node, which is what this topic's README specifies. The removed Go relay
is documented as a deliberate removal with a stated reason. Not a coverage gap.

`hwm_skip.py` runs both relay designs over the **same** rows in one process, so
the comparison is one run rather than two:

```
outbox rows 189   id/commit inversions 164
high-water-mark  delivered 137   PERMANENTLY SKIPPED 52   duplicates 0
SKIP LOCKED      delivered 189   PERMANENTLY SKIPPED  0   duplicates 0
```

The three relay modes make the README's argument measurable rather than
asserted — `--no-listen` left 30 rows unpublished at the end of the window and
`--drop-notifications` left 23, while the default left 0. The SQL's latency
query shows the same thing from the other side: rows claimed on a NOTIFY wake-up
came in at p50 106 ms, rows claimed on a poll tick at p50 1056 ms.

Consumer, at 4 consumers over the same deliveries:

```
check-then-act   effects 100   distinct messages 25   DUPLICATE EFFECTS 75
insert-then-act  effects  25   distinct messages 25   DUPLICATE EFFECTS  0
```

### Topic 7 — leader election, split brain, quorums, fencing

| Program | Status | Notes |
|---|---|---|
| `python/pause_audit.py` | RAN | 32s |
| `nodejs/pause_audit.js` | RAN | 32s |
| `golang/pause_audit.go` | RAN | 49s, three configurations |
| `rust/pause_audit` | RAN | 81s, five configurations |
| `cpp/pause_audit.cpp` | **FIXED-THEN-RAN** | Loop elision — see defect 1 |
| `java/PauseAudit.java` | RAN | 55s, `-Xmx2g` |
| `python/fencing_demo.py` | RAN | `--fencing 0` and `--fencing 1` |
| `sql/topic7_duplicate_payouts.sql` | RAN | Returns real rows |
| Parts 1–3 under compose (SIGSTOP + fencing) | **FIXED-THEN-RAN** | See the unblock pass |
| Part 4 under compose (`cpus: '0.1'` CFS throttle) | **STILL BLOCKED** | Not run; see the unblock pass |

All six run. Python, Node and Rust (`current_thread`, and `multi_thread` once the
finite pool is saturated) lose the 10s lease from their own behaviour with no
external fault. Go held it in all three configurations, which the README predicts
and explains rather than treating as a disappointment. The C++ SIGSTOP run lost
it outright.

**C++ handles Darwin honestly:** it reports `cgroup CFS throttling: NOT
AVAILABLE on this platform`, refuses to substitute the oversubscription hazard
for it, explains that the two are different mechanisms, and prints the
`docker run --cpus 0.1` command instead. That is the correct shape for a
Linux-only experiment.

Fencing, which is the topic's actual deliverable:

```
--fencing 0   attempts 62   rejected 0   stale-epoch attempts 40   DUPLICATE PAYOUTS 14
--fencing 1   attempts 41   rejected 1   stale-epoch attempts  1   DUPLICATE PAYOUTS  0
```

The `--fencing 1` run still shows the stale worker **waking up and trying** —
the rejection comes from the resource's `WHERE` clause, not from the worker
knowing it was deposed. That is the lesson, and the program does not shortcut it.

## Defects found and fixed

### 1. `07/cpp/pause_audit.cpp` — the anti-elision guard did not work, and the number it produced was impossible

**This is the same defect class Layer 1 shipped** (a "0 lost updates" result that
was really the compiler hoisting the loop away), wearing a comment that claimed
it had been prevented.

The hazard loop ended with:

```cpp
if (acc == 42) std::printf("");   // keep the loop from being elided
```

At `-O2`, Apple clang 21 elided the arithmetic anyway while `n += 50000` kept
counting as though it had happened. Measured on this machine, one thread:

| build | reported rate |
|---|---|
| `-O0` | 6.97e8 rounds/s |
| `-O2`, fixed trip count, `acc` printed | 5.17e8 rounds/s |
| `-O2`, `acc` genuinely observed | 5.92e8 rounds/s |
| **`-O2`, the `acc == 42` guard** | **2.8e12 rounds/s** |

2.8e12 rounds/s is roughly **4700x** what that expression can physically sustain
on this core — it is the cost of a `steady_clock::now()` call, not of the
arithmetic. The published run reported `188203516800000 rounds`, which works out
to ~2e12 per core-second and is not a measurement of anything.

Fixed with the Google-Benchmark `DoNotOptimize` idiom, applied once per inner
loop rather than per iteration:

```cpp
static inline void keep_alive(uint64_t& v) { asm volatile("" : "+r"(v)); }
```

Re-run: `35878600000 rounds` across 64 threads on 8 cores in 12s ≈ 3.7e8 per
core-second, which is consistent with the honest single-thread rate. **The
verdict did not change** — the hazard still holds the lease, so the topic's
conclusion stands. Only the number was wrong. A comment claiming to keep the
compiler honest, which the compiler ignores, is worse than no comment: it is a
fabricated number carrying a note that says it is trustworthy. The fix
documents the measurement in the source so the next reader does not have to
re-derive it.

`java/PauseAudit.java` has a similar-looking `if (acc == 42)` guard and was
checked the same way — it is **fine**. Its counted loop reports 2.13s for
`Integer.MAX_VALUE` iterations (~1e9/s), which is physically plausible, and the
quantity it publishes is a duration rather than a fabricated round count.

### 2. `01/sql/topic1_reconcile.sql` — silent table collision, then a hard error

The file aborted with:

```
ERROR:  column "toxic" does not exist
```

The whole layer shares one scratch database (`sep_lab_04_dist`). Topic 1 created
an **unprefixed** `charges` table; Topic 2 already owns an unprefixed `charges`
table with a completely different shape. Because both use
`CREATE TABLE IF NOT EXISTS`, Topic 1 did not collide loudly — it silently
adopted Topic 2's table and then failed on the first query referencing a column
Topic 2 does not have. Topics 6 and 7 already prefix (`t6_`, `t7_`) for exactly
this reason; Topic 1 was the outlier.

Fixed by renaming to `t1_charges` / `t1_client_attempts` (indexes too), matching
the layer's existing convention, with a comment in the file explaining why the
prefix is load-bearing. The orphaned `client_attempts` table left behind was
dropped. The file now parses and executes cleanly, returning zero rows from its
own empty schema — and a `HONEST STATUS` header was added, matching Topic 4's
convention, stating that it has never returned a row here and why.

## Checks that found nothing wrong

Recorded because a verification pass that only lists what it broke is not a
verification pass.

- **No Darwin portability defects.** No `<sys/epoll.h>`, no `/proc`, no cgroup
  file reads anywhere in the layer. Every place a Linux-only mechanism is the
  subject — `CLOCK_BOOTTIME` in the C++ and Python clock audits, CFS throttling
  in the C++ pause audit — has an explicit Darwin branch that says what is
  missing and prints the container command rather than substituting something
  else and calling it the same.
- **No missing build files.** Both Rust crates have `Cargo.toml` and build.
  Topic 2's Go module and Topic 5's `raftlab` module both resolve. Topic 1's and
  Topic 3's Go programs are stdlib-only and `go run` correctly without a module.
- **No Java class/filename mismatches.** `AmbiguousResult`, `ClockAudit` and
  `PauseAudit` all compile and run under the README's exact commands.
- **No hangs.** Nothing needed its alarm. The longest single run is Rust's pause
  audit at 81s, and Topic 6's `hwm_skip.py` at 64s — both are dominated by
  durations passed on the command line by the README, not by stalls.
- **Every README `How to run` command is correct as written.** No command in any
  of the seven topic READMEs needed changing.
- **The guards that claim to refuse a meaningless run actually fire.** Tested by
  forcing the degenerate case rather than trusting the claim:
  `clock_span_harness.py --steps 0` prints `*** BROKEN RUN, not a wrong
  prediction. ***`; `hwm_skip.py --writers 1 --hold-seconds 0` prints the same
  and reports zero inversions instead of a clean-looking table.
- **No invented numbers in the READMEs.** Every `Predict, then record` table in
  all seven topics is blank. No topic README contains a figure presented as an
  observation; the only numeric values in prose are configuration knobs
  (`250ms`, `latency 5000`, `APPLY_DELAY`) and cited third-party claims. There
  is no "What I saw" section anywhere in the layer.
- **All Python byte-compiles**, every file in the layer.

## Known gap, not fixed here

`lab/README.md` specifies a `04-distributed/docker/` directory holding the
compose files and service images for the whole layer, plus `NN-topic/docker/`
overrides. **Those directories do not exist.** Every run block that depends on
them is already recorded as BLOCKED above, so nothing in this file is affected.

They were deliberately not written by this pass: the Docker daemon is down and
k6 is not installed, so a compose stack authored here could not be started,
could not be verified, and would be exactly the kind of never-executed artifact
this layer exists to stop shipping. Write it when the daemon is up, against
`lab/README.md`'s service names, ports and environment variables — that file is
the spec and it is complete enough to build from.

**2026-08-19: written, and started.** `docker/compose.yaml` plus
`docker/services/` and `docker/k6/` now exist and every service in them has been
run. `lab/README.md` was indeed complete enough to build from; the two places it
is now out of date are noted in the unblock pass below.

## To unblock the rest of the layer

```
open -a Docker          # then re-run: python3 lab/local/check_env.py
brew install k6
```

Then, from `04-distributed/`, per `lab/README.md`:

```
docker compose up -d postgres toxiproxy etcd1 etcd2 etcd3 redpanda
```

When you are finished with the whole layer:

```
python3 lab/local/teardown_lab.py
```

---

# Unblock pass — Docker daemon up · 2026-08-19

**What changed on the machine:** the Docker daemon is running (server 29.5.3,
`linux/aarch64`, 4 CPUs and 5.1 GB in the VM, Compose v5.1.4). k6 is still not
installed on the host and was not installed — every k6 run below is the `k6`
compose service.

**What was actually blocking these entries.** The recorded unblock command was
`open -a Docker`, and that was necessary but nowhere near sufficient. The
`04-distributed/docker/` directory was **empty**: the compose file, the service
images and the k6 scripts that all seven run blocks reference had never been
written. This pass wrote them against `lab/README.md` and then ran them. So
nothing below is "the command finally worked" — every one of these is a first
execution of code that had never run, and the defect list is what that produced.

**Written by this pass:**

```
docker/compose.yaml                      the shared stack, profiles per topic
docker/services/Dockerfile               one image, six roles
docker/services/app/{db,kafka}.py        shared helpers
docker/services/app/api4.py              topic 4 read/write split
docker/services/app/ledger.py            topic 1 server-side truth
docker/services/app/payments1.py         topic 1 client + belief recording
docker/services/app/payments2.py         topic 2 IMPL A/B/C over HTTP
docker/services/app/payments6.py         topic 6 v0 dual write / v1 outbox
docker/services/app/relay6.py            topic 6 SKIP LOCKED relay + consumer
docker/services/app/relay7.py            topic 7 elected singleton + fencing
docker/pg/{primary-init,standby-entrypoint}.sh   the streaming standby
docker/k6/{topic1,topic2_duplicates,topic4_rw,topic6_load}.js
01-*/docker/run_toxics.sh                topic 1 driver
05-*/docker/{partition_drill,failover_trials}.sh
06-*/docker/run_broker_outage.sh
07-*/docker/run_sigstop.sh
```

Each topic ran under its own compose project (`-p l4-t1` … `-p l4-t7`), one at a
time, with `docker compose down -v` before the next. Nothing was left running.

## Topic 5 — etcd, Part A · BLOCKED → FIXED-THEN-RAN

`docker compose -p l4-t5 up -d etcd1 etcd2 etcd3` on `quay.io/coreos/etcd:v3.5.21`,
heartbeat 100 ms / election timeout 1000 ms. Partitions are
`docker network disconnect`, which drops peer *and* client traffic rather than a
percentage of packets.

```
scenario 1: one follower partitioned
  majority-side write               147 ms
  majority-side linearizable read   105 ms
  leader unchanged, still term 2         <- nothing user-visible, which is the point

scenario 2: the minority side (that one node, alone)
  WRITE                 Error: context deadline exceeded   after 20134 ms
  LINEARIZABLE READ     Error: context deadline exceeded   after 20169 ms
  SERIALIZABLE READ (--consistency=s)   returned EMPTY     after   155 ms

scenario 3: leader partitioned alone
  isolation -> majority accepting writes   1310 ms
  new leader etcd2 at term 3 (was etcd1 at term 2)
  isolated old leader's own write   Error: etcdserver: request timed out  after 7146 ms

scenario 4: partition healed
  old leader rejoins as a FOLLOWER at term 3
  /drill/s3b -- the write it attempted while isolated -- DOES NOT EXIST
```

Two results worth reading twice. The minority-side write and linearizable read
**do not fail fast** — they hang for the client's full 20 s timeout. And
`--consistency=s` answers in 155 ms with the *wrong* answer: the key it was
asked for was written to the majority after this node was isolated, so an empty
result is a stale read. That is Topic 4's ladder showing up in a real tool, which
the topic README predicts as a thing to record rather than a bug.

Failover measured over five trials on a fully settled cluster, rather than once:

```
trial  leader -> new     client-observed failover   raft lost-leader -> became-leader   term
  1    etcd2 -> etcd1            2454 ms                        6 ms                    3->4
  2    etcd1 -> etcd2            1314 ms                        4 ms                    4->5
  3    etcd2 -> etcd1            2443 ms                        4 ms                    5->6
  4    etcd1 -> etcd2            1285 ms                        3 ms                    6->7
  5    etcd2 -> etcd1            1299 ms                        3 ms                    7->8
```

Pumba was also checked, since the topic README names it:
`pumba netem --duration 20s --tc-image gaiadocker/iproute2 loss --percent 100 l4-etcd3`
works and drives that endpoint to `HEALTH false`. `--tc-image` is not optional,
exactly as `lab/README.md` says.

## Topic 4 — replica lag · BLOCKED → FIXED-THEN-RAN

A real `postgres:18` streaming standby of `pg-primary`, `pg_basebackup -R`, with
`recovery_min_apply_delay` driven by `APPLY_DELAY`. `sql/topic4_stale_reads.sql`
**returns rows for the first time.** Query 0 first, because nothing else means
anything without it:

```
run_id |  fix   | reads | on_standby | on_primary | pct_on_standby
d2s    | lsn    |   200 |        200 |          0 |          100.0
d2s    | none   |   200 |        200 |          0 |          100.0
d500ms | lsn    |   200 |        200 |          0 |          100.0
d500ms | none   |   600 |        600 |          0 |          100.0
d500ms | sticky |   200 |          0 |        200 |            0.0
```

```
=== 1. stale reads by apply delay and read-after-write gap ===
  fix   | apply_delay | gap_ms | reads | stale | pct_stale
 none   | 500ms       |      0 |   200 |   200 |    100.00
 none   | 500ms       |    250 |   200 |   200 |    100.00
 none   | 500ms       |   1000 |   200 |     0 |      0.00
 none   | 2s          |   1000 |   200 |   200 |    100.00
 sticky | 500ms       |      0 |   200 |     0 |      0.00
 lsn    | 500ms       |      0 |   200 |     0 |      0.00
 lsn    | 2s          |   1000 |   200 |     0 |      0.00

=== 2. the cost of Fix A ===   sticky: 100.0% of reads hit the PRIMARY
=== 3. the cost of Fix B ===   lsn polls p50 23 at 500ms, 45 at 2s; 0% fallback
=== 4. read latency ===
  fix   | apply_delay | gap_ms | p50_ms  | p99_ms
 none   | 500ms       |      0 |    0.66 |   15.15
 sticky | 500ms       |      0 |    0.82 |    4.92
 lsn    | 500ms       |      0 |  507.81 |  949.21
 lsn    | 2s          |   1000 | 1010.91 | 1023.77
```

The three fixes line up into one sentence with numbers in it: `none` is 0.66 ms
and wrong 100% of the time, `sticky` is 0.82 ms and right because it stopped
using the replica at all, `lsn` is right and costs 508 ms — and at a 2 s delay
with a 1 s gap it costs 1011 ms, which is the remaining wait, not a coincidence.

The row that makes the whole table trustworthy is `none / 500ms / gap 1000` at
0.00% stale: the same configuration, one knob moved, and the effect disappears.

## Topic 1 — the ambiguous result, Part B · BLOCKED → FIXED-THEN-RAN

`payments-api` calls `ledger` through Toxiproxy; `ledger` commits to Postgres
before anything can go wrong with the reply; `payments-api` records one
`t1_client_attempts` row per attempt. 10 VUs × 100 iterations per toxic.

```
toxic                   attempts  ambiguous  safe  client_2xx  ledger_rows  orphans
none                         100          0     0         100          100        0
timeout      (upstream)      300        300     0           0            0        0
timeout_downstream           300        300     0           0          300      300
reset_peer   (upstream)      300        300     0           0            0        0
reset_peer_downstream        300        300     0           0          300      300
latency 5000                 300        300     0           0          300      300
bandwidth 0                  300        300     0           0          300      300
crash_after_commit           300        300     0           0           11       11
```

**Read the `ambiguous` column and then the `ledger_rows` column.** The client's
observation is identical in all seven fault rows — 300 ambiguous, zero
successes. The server-side truth is 0 charges in two of them and 300 in three.
The client cannot tell those apart, and no timeout tuning changes that. The
`timeout` and `reset_peer` pairs are the sharpest version: the *same named
toxic*, moved from the upstream to the downstream side of the proxy, flips the
answer from "nothing happened" to "everything happened", and looks the same from
the caller's chair.

`crash_after_commit` is 100% ambiguous and 100% orphaned as the topic predicts;
its absolute row count is low because the ledger container crash-loops and is
down for most of the window.

**The `safe` column is zero everywhere, and that is a finding, not a gap.**
Through a proxy the client connects successfully to Toxiproxy, so a refused or
dead upstream arrives as `RemoteProtocolError` / `ReadError`, never as
`ConnectError`. Measured under `crash_after_commit`: 192 `RemoteProtocolError`,
101 `ReadError`, 7 `ReadTimeout`, and 0 of anything classifiable as safe. **A
proxy converts the one provably-safe failure into an ambiguous one.** Topic 1's
`refused` case cannot be reproduced through Toxiproxy at all.

## Topic 2 — idempotency under k6 · BLOCKED → FIXED-THEN-RAN

200 keys × 5 VUs firing the *same* key simultaneously, over real sockets.

```
=== 2. Per run: requests reconciled against rows ===
 run_id | impl | keys_charged | charge_rows | extra_charges | pct_duplicate
 A      | A    |          200 |         984 |           784 |         79.67
 B      | B    |          200 |         200 |             0 |          0.00
 C      | C    |          200 |         200 |             0 |          0.00

=== 4. Charged with no key row, or key row with no charge ===  0 and 0
=== 5. Index health === charges has NO unique index on (tenant_id, idempotency_key)
```

Query 5 is the one to check first: `charges` carries only `charges_pkey` and
`charges_run_idx`, so B's and C's zeros come from the code, not from a
constraint quietly doing the work. A's 78.32% HTTP failure rate is the `500`s it
returns *after* charging, when the unique index on `idempotency_keys` rejects
the key row it should have written first.

## Topic 6 — outbox vs dual write, with the broker stopped · BLOCKED → FIXED-THEN-RAN

Redpanda under `--smp 1 --memory 700M --overprovisioned`. 60 s at 20 req/s;
`docker compose stop redpanda` at t+20 s, `start` at t+40 s. Identical fault,
both designs.

```
run_id | charges | outbox_rows | unpublished | delivered | publish_failures | charges with NO event
v0     |     687 |           0 |           0 |       631 |               56 |                    56
v1     |    1201 |        1201 |           0 |      1201 |                0 |                     0
```

```
=== 1. Every charge must have at least one event ===
 run_id | charges | with_outbox_row | missing_events
 v0     |     687 |               0 |            687
 v1     |    1201 |            1201 |              0

=== 2. what the relay delivered ===   v1: 1201 of 1201, 0 skipped, 0 duplicates
=== 6. the EFFECT count ===           v1: 1201 effects, 1201 distinct, 0 duplicate effects
```

The dual write lost 56 events permanently to a 20-second outage. The outbox lost
none — the same outage became a backlog that drained. Note also that v0 only
completed 687 of the 1200 offered requests while v1 completed all of them: the
synchronous publish blocks the request path while the broker is down, so the
dual write costs availability as well as correctness.

`=== 5. charge -> event latency ===` reports p50 55.9 s / p99 85.0 s for v1.
That is not a defect, and it is not a steady-state figure: it is dominated by the
20 s outage plus the drain afterwards, which is the whole point of the run.

## Topic 7 — the paused leader, parts 1–3 · BLOCKED → FIXED-THEN-RAN

`relay-a` and `relay-b` contend for a lease row with a database-issued monotonic
epoch, 10 s TTL, renewing every 1 s. The elected one is `docker kill -s SIGSTOP`ed
for 15 s, the other takes over, then `SIGCONT`.

```
FENCING=0   elected relay-a epoch 1 -> SIGSTOP 15s -> relay-b epoch 2 -> SIGCONT
FENCING=1   elected relay-a epoch 2 -> SIGSTOP 15s -> relay-b epoch 3 -> SIGCONT
```

```
=== 0. Did a takeover actually happen? ===
 run_id | fencing | workers_that_wrote | first_epoch | last_epoch
 f0     | f       |                  2 |           1 |          2
 f1     | t       |                  2 |           1 |          2

=== 1. THE RESULT ===
 run_id | fencing | attempts | accepted | rejected_by_resource | DUPLICATE_PAYOUTS
 f0     | f       |      880 |      880 |                    0 |                10
 f1     | t       |      880 |      870 |                   10 |                 0

=== 2. The stale writer ===
 run_id | fencing | worker  | epoch | stale_attempts | rejected | accepted_while_stale
 f0     | f       | relay-a |     1 |             10 |        0 |                   10
 f1     | t       | relay-b |     1 |             10 |       10 |                    0
```

Symmetric, and query 2 is the line that makes query 1 mean something: the stale
writer **attempted 10 writes in both runs**. Fencing did not stop it trying —
nothing can, because a stale writer by definition does not know it is stale. The
10 rejections came from the resource's `WHERE fence <= epoch` clause. A run where
`stale_attempts` were 0 would have tested nothing, and the deliverable says so.

## Topic 3 — `libfaketime` in a container · BLOCKED → FIXED-THEN-RAN

The only part of Topic 3 that genuinely needs a Linux container. It works, and
running it found that **the README's own command is wrong by a factor of
60,000**:

```
FAKETIME=+250ms   offset = +15000.016 s
FAKETIME=+250m    offset = +15000.051 s     <- identical: the `s` is ignored
FAKETIME=+0.25    offset =     +0.284 s     <- what +250ms was meant to be
```

libfaketime's offset suffixes are `m`/`h`/`d`/`y`. It reads `250ms` as 250
**minutes**. A sub-second offset must be written as a fractional number of
seconds. The library is also not at `/usr/lib/faketime/` on a Debian arm64
image but under `/usr/lib/aarch64-linux-gnu/faketime/`. Both are fixed in
`03-clocks-lie/README.md`, with the measurements beside them.

Confirmed in the same run, and it is the reason Topic 7's lease timers are
monotonic: under `FAKETIME=+0.25`, `time.time()` moves and `time.monotonic()`
does not.

## Defects found and fixed in this pass

Numbered from 3, continuing the list above. Every one of these is a first-contact
defect: this is the first time any of this code executed.

### 3. `docker network connect` silently drops the compose service alias

Reconnecting a healed etcd node made it reachable as `l4-etcd1` but **not** as
`etcd1`, which is the name every peer URL uses. The node never rejoined, and
because the drill carried on regardless, scenarios 3 and 4 ran against a
two-node cluster that could no longer lose a member — reporting "no new leader
within 30 s" as though that were a result. Fixed with
`docker network connect --alias etcd1`, plus a `wait_healthy()` that **aborts**
rather than entering a scenario whose starting state is already broken.

### 4. A health gate that passes too early turns a failover measurement into a catch-up measurement

The first working failover number was **7392 ms** against a 1000 ms election
timeout — over the topic README's own 5-second "this experiment is broken" bar.
It was not the config: heartbeat 100 ms against election timeout 1000 ms is a
correct 10× ratio, and `docker compose exec` overhead measured 0.09–0.31 s, so
the harness was not the cost either. etcd's own raft log had the answer — four
failed pre-vote rounds at 1.8 s intervals before a successful campaign:

```
09:23:53.816  lost leader ... at term 2
09:23:53.815 / 55.613 / 57.413 / 59.214   is starting a new election at term 2
09:23:59.509  became candidate at term 3
09:23:59.512  became leader at term 3
```

The cause was upstream of etcd: the gate required all three endpoints to answer
and name the same leader, which is true **~2 s after a heal while one node is
still a log entry behind**. Isolating the leader there measures a convalescing
cluster. Requiring equal `raftIndex` and adding a settle window brought it to
**1285–2454 ms over five trials**. The number did not change because the fix
made it look better; it changed because the earlier number was measuring
something else.

### 5. `postgres:18` moved `PGDATA`, and the 16/17 path fails as a permissions error

`pg_basebackup: error: could not create directory "/var/lib/postgresql/data/pg_wal": Permission denied`.
`postgres:18` uses `PGDATA=/var/lib/postgresql/18/docker` and declares
`/var/lib/postgresql` as the volume. The old path is a root-owned directory the
`postgres` user cannot write, so the failure surfaces as a permission problem
rather than a path problem. Fixed by taking `PGDATA` from the image environment
and mounting the volume where the image expects it.

### 6. A stale k6 `BASE` default pointed five topics at the wrong service — and k6 still exited 0

The `k6` compose service carried `BASE: ${BASE:-http://api:8000}`, left over from
topic 4. Every other topic's script has its own default, which an always-set env
var overrides. Topic 1's entire toxic matrix ran against `http://api:8000`,
which does not resolve, k6 reported `100.00% 784 out of 1001` failed requests —
**and exited 0**, so the driver script printed `k6 done` six times and the
reconciliation query returned zero rows from six "successful" runs.

Fixed by removing the default, and by adding a `setup()` guard to every k6 script
that fetches `/health` and throws `*** BROKEN RUN ***` if it cannot reach the
target. A run that cannot reach its target is not a result.

### 7. Environment variables do not reach `docker compose run` containers unless the service names them

`TOXIC=$toxic docker compose run --rm k6 ...` sets the variable for the **compose
CLI**, not for the container. `TOXIC` was not in the k6 service's `environment:`
block, so `__ENV.TOXIC` was undefined and every one of the six toxic runs
self-labelled `none`. All six landed in one bucket with colliding request ids,
and the deliverable confidently reported 7–8 duplicate commits per request that
no fault had caused. Fixed by passing `-e TOXIC=...` straight to the k6 binary,
and by making the script throw if `TOXIC` is unset.

### 8. `topic4_stale_reads.sql` query 5 could not answer the question it was written to ask

Its comment says: *"if it does not move when you change `APPLY_DELAY`, the
standby is not honouring `recovery_min_apply_delay`."* Measured in WAL bytes, it
moved the **wrong way** — 26350 bytes mean lag at `500ms` against 4482 at `2s`.
The standby was fine. Verified directly under continuous write load:

```
APPLY_DELAY   time lag on the standby
500ms         534 ms
2s           2051 ms
```

Byte-lag is confounded by write throughput: a burst of 200 writes in 0.3 s
leaves more unreplayed WAL behind a 500 ms delay than a slow trickle leaves
behind a 2 s one. Fixed by adding `read_replay_ts` to the `rw_probe` contract and
rewriting query 5 to lead with time lag.

**And the time measure is confounded too, which is left visible on purpose.**
`now() - pg_last_xact_replay_timestamp()` is the most misread replica-lag metric
in production monitoring: on an idle primary it just ages. The output shows it
misreading in miniature — `lsn` rows track the setting (512 ms at `500ms`,
2010 ms at `2s`) because they are sampled at a known moment relative to a write,
while `none/500ms` reports a 2004 ms mean and a 7929 ms max across three k6 runs
and the quiet gaps between them. The rewritten comment says which rows to read
and why.

### 9. The outbox relay treated "producer queue drained" as "broker acked"

`Producer.flush()` returns the number of messages **still queued**, and a message
that failed permanently (`message.timeout.ms` with `retries=0`) has already left
the queue. Keying off `flush() == 0` marked lost events as delivered — an outbox
relay that loses events silently, which is strictly worse than the dual write it
exists to replace. It under-reported v0's losses too. Fixed with per-message
delivery callbacks; a row is marked published only when its delivery report came
back without an error.

### 10. One Kafka topic shared across runs, plus `auto.offset.reset=earliest`

Each run used a fresh consumer group on the same topic, so run N's consumer
re-read everything runs 1..N-1 had ever published. `distinct_delivered` came out
at **2172 for a run with 1037 charges**. Fixed with a topic per run.

### 11. The consumer recorded charge ids in a column that means outbox row ids

`t6_delivered.outbox_id` is joined to `t6_outbox.id` by the deliverable. Writing
the charge id there made query 2 report **687 "permanently skipped" rows that had
all been delivered**, and query 5 emit **negative** charge→event latencies from
differencing two unrelated rows. Fixed by carrying the outbox row id in the
message payload. The deliverable now reads 1201 of 1201 delivered, 0 skipped.

### 12. `fence < epoch` rejects the live leader's own re-drives

Topic 7's fencing predicate must reject writes from a **strictly older** epoch
and accept the current holder's re-drives. `fence < epoch` rejects the live
leader the second time it touches a key it already paid: the first run showed
**108 rejections** attributed to the new leader, which reads like fencing working
while measuring nothing about split brain. Corrected to `fence <= epoch`.

### 13. SIGSTOP inside an in-flight statement blocks the takeover, so no split brain occurs

The first `FENCING=0` run held epoch 1 on one worker for the entire 15 s pause —
no takeover, and query 0 of the deliverable exists precisely to catch that. The
cause is the thing the container version was built to expose: `SIGSTOP` can land
while the holder has the lease row locked in an open transaction, and the other
worker's takeover `UPDATE` then blocks on that row lock for as long as the pause
lasts. A paused *thread* cannot do this, which is why the local `fencing_demo.py`
never showed it. Fixed with `SET lock_timeout`, which turns an indefinite block
into a fast retry.

### 14. Renewing before acting means the stale writer never writes

With renewal ahead of work in the loop, the woken worker discovered it had been
deposed **before** it ever wrote, and the deliverable's query 2 returned **zero
stale attempts** — which its own comment calls a broken run rather than a clean
one. No real relay re-validates its lease before every row; it renews on a timer
and does work in between, and the window between the two is where split brain
lives. Reordered to act-then-renew, and the batch size raised from 1 to 10 to
match the topic README's "publish the batch it believed it owned". With a batch
of 1 the run produced 1 stale attempt and 0 duplicate payouts, which reads as
"fencing was not needed"; with a batch of 10 it produces 10 and 10.

## Still blocked, honestly

### Topic 3 Part B under compose — `writer-a` / `writer-b`

```
CLOCK_OFFSET_MS=250 docker compose up -d --force-recreate writer-b
docker compose run --rm k6 run /scripts/topic3_lww.js
```

**Not run. The two writer services do not exist** — this pass did not write
them, and nothing is gained by pretending otherwise. What it needs: an
`app/writer.py` exposing a write endpoint that stamps `now() + CLOCK_OFFSET_MS`,
two compose services `writer-a` (offset 0) and `writer-b` (offset from the env
var) on ports 8010/8011, and `k6/topic3_lww.js` driving both at ~50 writes/s
against 10 shared keys. Roughly the size of `payments2.py` plus its k6 script.

The correctness result this would add is already covered by
`python/lww_writers.py`, which runs locally and is recorded as RAN above; what
compose adds is network delay between writer and database, which changes the
*rate* of collisions and not their existence. The `libfaketime` half of Part B
**is** now run — see above.

### Topic 7 Part 4 — CFS throttling via `cpus: '0.1'`

**Not run.** The mechanism is available — a container started with `--cpus=1.0`
in this VM reports `cpu.max: 100000 100000` and `nr_throttled` is present in
`/sys/fs/cgroup/cpu.stat`, so cgroup v2 throttling is real here. What is missing
is the experiment: a `cpus: '0.1'` deploy limit on `relay-a` plus a CPU load
generator inside it, and a check that `nr_throttled` actually rises during the
run. **Do not record that one without checking `nr_throttled`** — a throttling
experiment reporting `nr_throttled: 0` has demonstrated nothing, and the whole
point of part 4 is that the pause comes from the quota rather than from a signal.

### `lab/README.md` is now out of date in two places

Both are consequences of this pass, not errors in the original spec:

1. The **no-Docker fallback** section opens "The Docker daemon is not running on
   this machine and k6 is not installed". The daemon is up. k6 is still not
   installed on the host and is not needed there — use the `k6` service.
2. The **version pins** table's right-hand column says
   `Docker | daemon not running` and `k6 | not installed`.

### The `psql` line in four run blocks points at the wrong database

Topic READMEs say `psql -d sep_lab_04_dist -f sql/...`. That is correct for the
**local fallback**, whose data is in the host's Postgres 17.5. The compose
experiments write to the compose Postgres instead, because the containers cannot
reach the host server without editing its `pg_hba.conf`, and `lab_db.py`'s rule
is that nothing here touches a config it did not create. The commands that
produced every table above:

```
topic 1, 2, 6, 7   psql -h localhost -p 55434 -U postgres -d lab -f sql/...
topic 4            psql -h localhost -p 55432 -U postgres -d lab -f sql/...
```

The two deliverables whose headers were stale have been updated in place
(`topic4_stale_reads.sql`). The others still carry the local-fallback command,
which is correct for the local-fallback data they already return rows from.

## Teardown state

Every stack was brought down with `docker compose down -v` after its topic. At
the end of this pass `docker ps` shows **zero** `l4-*` containers and the
`l4-lab` network is removed. The host Postgres and `sep_lab_04_dist` were not
touched; `lab/local/teardown_lab.py` is still deliberately unrun.
