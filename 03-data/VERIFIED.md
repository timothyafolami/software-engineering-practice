# Layer 3 — verification record

**Date:** 2026-08-19
**Verified by:** an independent pass that did not write this code. Every row below
was compiled and executed on this machine, from the `03-data` directory, using
the command in that topic's `How to run` block.

**What this file is.** It records that the code in this layer **executes on this
machine and produces the output it claims to produce**. It does not record that
anything was learned. The `Predict, then record` tables in every topic README are
still blank and are still yours — filling them in from this file would be reading
somebody else's answers, which is the one way to get nothing out of this layer.

Numbers quoted here are real because they were observed during this pass. They
are also **one run on one laptop**: several of these programs are variance-prone
by nature, and the point of each is the shape, not the digits.

## The machine

| | |
|---|---|
| OS | macOS 27.0 (Darwin 25.x, arm64 / Apple M1), 8 cores |
| PostgreSQL | **17.5** (Homebrew), local, socket `/tmp`, `max_connections = 100` |
| Python | 3.13.5 — psycopg 3, SQLAlchemy 2.0.39 |
| Node.js | 24.14.0 — `pg` 8.x installed per topic from the local `package.json` |
| Go | 1.24.5 darwin/arm64 — `pgx` v5 |
| Rust | 1.97.1 — *no Rust code in this layer, by design* |
| C++ | Apple clang 21.0.0, `-std=c++17`, libpq via `pg_config` |
| Java | JDK 21.0.2 — *no Java code in this layer, by design* |
| Docker | CLI 28.1.1 / server **29.5.3**, compose v5.1.4. Daemon DOWN on the first pass, **UP** on the 2026-08-19 unblock pass |
| Docker VM | linuxkit, `linux/aarch64`, **4 CPUs / 4.8 GB** — smaller than the host, and it is what every containerised number below was measured on |
| k6 | **not installed on the host**, and still is not. The compose `k6` service is what ran the load scripts |

**The host Postgres is 17.5, not the 18 the layer is written against.** On the
first pass that meant four things were verified only as far as their version
gate: skip scan, `Index Searches`, `BUFFERS`-by-default and `WAIT FOR LSN`.
**All four have since been measured** — the first three on the compose stack's
`postgres:18` (18.6), the fourth on a `postgres:19beta3` pair built for it. See
[Unblock pass](#unblock-pass--docker-daemon-up-2026-08-19). Rows below carry the
result and say which server produced it.

## Every program, and what happened

### `lab/`

| Program | Status | Notes |
|---|---|---|
| `lab/local/check_env.py` | RAN | Reports 17.5 and gates 8 capabilities with per-item unblock commands. Correct on every one. |
| `lab/local/setup_lab.py` | RAN | Idempotent. Seed present: 50,000 customers / 1,000,000 orders / 3,000,000 line items, 437 MB. |
| `lab/local/lab_db.py` | RAN (imported) | Exercised by every Python program in the layer. |
| `lab/local/teardown_lab.py` | BLOCKED | Deliberately not run: it drops `sep_lab_03_data`, the database every other row in this table was verified against. Unblock (when you are done with the layer): `python3 lab/local/teardown_lab.py` |
| `lab/docker/compose.yml` | **FIXED-THEN-RAN** | All five services brought up on 2026-08-19: `postgres-primary` (PG18.6), `postgres-replica` (streaming, `recovery_min_apply_delay = 2s`), `pgbouncer` (1.25.2), `api`, `k6`. Three defects had to be fixed first — see defects 10, 11 and 12; two of them made the file unusable rather than merely wrong. |
| `lab/docker/api/*.py` | **FIXED-THEN-RAN** | `serve.py` derives 5 workers from `cpu.max` at the compose default of 2.0 CPU and says so in its first log line; `main.py` serves `/healthz`, `/readyz`, `/orders` (7 queries for 3 rows — the N+1, visible), `/slow`, `POST /orders`, `/orders/{id}?read=primary\|replica\|lsn`, `/poolstats`. `worker_count.py` fixed, see defect 13. |
| `lab/docker/load/*.js` | **FIXED-THEN-RAN** (2 of 3 demonstrate their claim) | Run through the compose `k6` service, no host install. `bulkhead.js` and `stale_reads.js` demonstrate their claims outright. `pool_sweep.js` executes and reports, but **does not produce the knee** on this hardware — see "Experiments that run and do not demonstrate their claim". Defect 14 fixed in both sweep scripts. |

### 01 — Isolation levels *(pre-existing code, verified anyway)*

| Program | Status | Notes |
|---|---|---|
| `python/lockstep_psql.py` | RAN | SERIALIZABLE fails at B's `COMMIT`, not at B's `UPDATE` — captured verbatim from the server. |
| `python/write_skew.py` | RAN | Broken shifts at read committed and repeatable read, zero at serializable, zero with `FOR UPDATE`. Each row prints the isolation level it actually ran at. |
| `python/lost_update.py` | RAN | Single-statement variant loses nothing; read-modify-write does. |
| `python/serializable_cost.py` | RAN | `SIReadLock` granularity page/tuple with an index, `relation` without one. |
| `golang/write_skew` (`go run .`) | RAN | 100 broken / 99 broken / 0 broken, 702 × `40001`, 85 gave up after retries. |
| `sql/write_skew_session_*.sql` | BLOCKED | By design — the file header says "do NOT run this with `-f`". Two terminals, alternating steps; running them as scripts removes the interleaving that is the whole demonstration. |

### 02 — MVCC and vacuum *(pre-existing code, verified anyway)*

| Program | Status | Notes |
|---|---|---|
| `python/mvcc_lab.py` | RAN (imported) | No output of its own, as documented. |
| `python/xmin_horizon.py` | RAN | Terminated the offending idle-in-transaction session at 5s; dead tuples 252,597 → 0 after `VACUUM`. |
| `python/bloat_and_latency.py` | RAN | 2 min 08 s at the documented `DURATION_S=120 SAMPLE_S=20`. Dead tuples 0 → 3.2 M, table 23 MB → 306 MB, and `table_mb` moves only at `VACUUM FULL` — exactly as the README says. |
| `python/replication_slot_starvation.py` | RAN | Logical-slot phase reports BLOCKED (`wal_level = replica`) and skips rather than inventing it; the physical-slot phase runs and shows 505 MB of WAL pinned with no `xmin`. |

### 03 — Indexes

| Program | Status | Notes |
|---|---|---|
| `python/column_order.py` | **FIXED-THEN-RAN** | Four query shapes × three index configurations. Re-run on PG18.6 the `searches` column populates (config A: 1 / 2 / 1 / –; config B: 1 / 9 / 1 / 1). The demotion-to-`Filter` the notes claimed is **not there and never was** — see defect 16; the cost is in `Buffers` (26 vs 190 on the same index) and the file now says so. |
| `sql/01_skip_scan.sql` | **FIXED-THEN-RAN** ×2 | Defect 1 (first pass, PG17). Defect 15 (this pass, PG18.6): three of the file's claims were contradicted by the server the moment one could run it. |
| `sql/02_futile_index.sql` | RAN | `status = 'complete'` seq-scans; `enable_seqscan = off` is slower, which is the answer. |
| `sql/03_write_cost.sql` | RAN | 0/1/3/6 secondary indexes → 186k/165k/94k/59k rows/s and 122 → 598 WAL bytes/row on this run. HOT section: `hot_updates` 0 vs 5,000 and WAL 2,784 kB vs 483 kB for the same 5,000-row update. |
| Skip scan itself | **RAN** | Unblocked via `lab/docker/compose.yml` (PG18.6). `(status, customer_id)` + `WHERE customer_id = 4242`: **`Index Searches: 9`**, 47 buffers, 0.037 ms. `(customer_id, status)` + `WHERE status = 'failed'`: **`Index Searches: 1`** — it declined to skip — 10,326 buffers, 10.7 ms. And with a **range** on the trailing column, which the file said was impossible: `(status, created_at)` skips (`Index Searches: 5`, 1,185 buffers, 6.1 ms) while `(customer_id, created_at)` does not (parallel seq scan, 9,260 buffers, 33.1 ms). |

### 04 — Reading a query plan

| Program | Status | Notes |
|---|---|---|
| `python/plan_drill.py` | **FIXED-THEN-RAN** | Reports the **worst-estimated node**, not the root. Correlated predicates 8x low → repaired by `CREATE STATISTICS`; `lower(email)` 250x low → repaired by an expression index. Re-run on PG18.6 in 5.1 s, same shape. Defect 17: the `est` and `act` columns collided into `173,125138,889.0` on six-figure row counts. |
| `python/flip_threshold.py` | RAN | On `created_at` (correlation ≈ 1.0) the index scan never loses out to 60%; on `total_cents` it flips index → bitmap at 0.02% and bitmap → seq at 33.1%. At `random_page_cost = 4.0` both move. |
| `python/production_triage.py` | RAN | `pg_stat_statements` correctly reported BLOCKED, with the client-side substitute clearly labelled as a substitute. `LOAD 'auto_explain'` works with no restart, as claimed. |
| `golang/plan_cache` (`go run .`) | RAN | The generic-plan switch lands at execution six, and the plan then stops naming the value (`Index Cond: (status = $1)`). |
| `nodejs/protocol_plans.js` | RAN | Unnamed statements: custom plan every execution, zero server-side statements left behind. Named: same switch at six. |
| `cpp/plan_protocol.cpp` | **FIXED-THEN-RAN** | See defect 2. Builds clean with the README's `g++` line including `-Wl,-rpath`; the rpath is genuinely required here. |
| `sql/plans/*.sql` (10 files) | **FIXED-THEN-RAN** | See defect 3. All ten now also run standalone in `psql`, as the README claims. |
| `Index Searches: N` | **RAN** | Available and populated on PG18.6 — `lab_db.index_searches()` returns integers and `EXPLAIN (ANALYZE)` prints the field on every index node. Note for readers: topic 4's own drill table has no searches column; the field is read in topic 3's table and in `sql/01_skip_scan.sql`. All ten `sql/plans/*.sql` re-run clean on 18.6. |

### 05 — Locking and deadlocks

| Program | Status | Notes |
|---|---|---|
| `python/deadlock_ordering.py` | RAN | 19 deadlocks in argument order, 0 with `ORDER BY id`; p99 14 ms → 3,212 ms. The server's own deadlock report is captured verbatim and names all four waiting processes. |
| `python/migration_lock_queue.py` | RAN | Reads go to zero when the `ALTER` starts **waiting**; 5.25 s of dead time with no `lock_timeout`, bounded to 2.75 s and `55P03` with one. The live lock queue shows 7 of 8 requests ungranted. |
| `python/skip_locked_queue.py` | RAN | 2.5x throughput, 0 duplicates in both variants, and 15 s of churn takes the queue table 192 kB → 174 MB. |
| `sql/lock_queue.sql` | RAN | Returns zero rows when nothing is blocked, which is correct; it is written to be run in a second terminal with `\watch 1` while the program above is mid-flight. |

### 06 — Finding N+1

| Program | Status | Notes |
|---|---|---|
| `python/orm_lab.py` | RAN (imported) | Models map onto the existing lab tables and create nothing. |
| `python/query_counter.py` | RAN | 21 / 201 / 2,001 queries at `limit` 10 / 100 / 1000. The identity-map trap is demonstrated (N+2 instead of 2N+1 when scoped to one customer). The CI gate fails at 201 and passes at 2. |
| `python/lazy_vs_eager.py` | RAN | Honest negative result preserved: on this seed `joinedload` beats `selectinload` at every limit, and the nested `customers → orders → line_items` table (30,000 rows over the wire at 500 customers) still has `joinedload` ahead. The program says so instead of asserting the textbook answer. |
| `nodejs/dataloader_batching.js` | RAN | 501 / 2 / 501 queries at limit 500 — the third row has a correctly-configured DataLoader in it and batches nothing, because an `await` between `load()` calls ends the tick. |

### 07 — Connection pools

| Program | Status | Notes |
|---|---|---|
| `python/pool_lab.py` | **FIXED** (imported) | Open-loop generator, unchanged. Gained an optional `statement_timeout_ms`; see defect 6. |
| `python/pool_sweep.py` | **FIXED-THEN-RAN** | ~52 s. Throughput plateaus in the 50–60 req/s band from pool 10 while p99 climbs 2,335 → 4,477 ms, and `server: too many clients` appears at pool 100. Defect 5 (a truncated error column) fixed. |
| `python/bulkhead.py` | **FIXED-THEN-RAN** | ~29 s. Fast-endpoint p99 104 ms alone → 2,013 ms sharing a pool → 52 ms behind a bulkhead. Defect 5 fixed here too — the `statement timeout=69` errors were being cut off at the margin, and they are half the point of the third row. |
| `python/retry_storm.py` | **FIXED-THEN-RAN** | ~60 s. **Does not reproduce its headline claim on this machine** — see defect 6, which is the most important row in this file. |
| `python/worker_count.py` | **FIXED-THEN-RAN**, both paths | On Darwin it still reports BLOCKED and refuses to invent a quota. **Inside the container it now runs**, at all three quotas: `cpu.max` 50000/100000 → 0.50 cpu → 2 workers vs `os.cpu_count()`=4 → 9 workers (30 vs 135 connections, 1,050 extra across 10 replicas); 1.0 → 3 workers (900 extra); 2.0 → 5 workers (600 extra). The command it printed could not have produced any of that — defect 13. |
| `golang/pool_defaults` (`go run .`) | RAN | ~33 s. Default (unlimited) pool peaked at 150 connections and took 1,282 × SQLSTATE `53300` from the server. `MaxOpen=10` produces a client-side queue instead: 462 waits, 17 m of accumulated wait time. |
| `nodejs/pool_defaults.js` | RAN | 40 requests, 0 errors, 4.0 s wall, `pool.waitingCount = 20` mid-run and `/healthz` returning 200 the whole time. |
| Experiment 6 (cgroup quota) | **FIXED-THEN-RAN** | See `worker_count.py` above. |
| Experiment 4a (PgBouncer, large client pools) | **RAN** | `pool_sweep.py` against the docker PG18 direct, then the identical run through PgBouncer 1.25.2. Direct at `pool_size = 100`: 41 req/s, p99 4,635 ms, **`server: too many clients = 413`**, 88.0 mean active backends. Through PgBouncer at the same 100: 42 req/s, p99 4,399 ms, **zero** rejections, 15.7 active / 3.0 idle — `default_pool_size = 20` holding the line. |
| Experiment 4b (`max_prepared_statements`) | **RAN**, via a program that had to be written | The shipped programs could not measure this at all — see "What was missing". New `python/pgbouncer_prepared.py`: at 200, 300/300 executions complete; at 0, the run dies at execution 126 with `DuplicatePreparedStatement: prepared statement "_pg3_0" already exists`. psycopg 3.3.3, `prepare_threshold = 5`. |

### 08 — Replication lag

A **real streaming standby was built and used** for this verification —
`pg_basebackup` into `$TMPDIR/sep_lab_03_replica`, port 5433 — at both
`APPLY_DELAY=2s` and `APPLY_DELAY=0`, and torn down afterwards with
`--stop`. The replication slot is dropped and the directory is gone; the primary
is untouched.

| Program | Status | Notes |
|---|---|---|
| `sql/partition_orders.sql` | RAN | Builds `orders_part` (37 partitions, ~80 MB) and never touches the seeded `orders`. |
| `python/partition_pruning.py` | RAN | ~17 s. Planning 12 → 120 partitions: pruned query flat (0.024 → 0.030 ms), unpruned 0.065 → 4.270 ms — 66x for 10x the partitions. `ORDER BY key DESC LIMIT` does **not** prune. Runtime pruning shows 118 of 120 partitions `(never executed)`; on PG17 that is the only signal, since `Subplans Removed` is not reported, and the program counts both. |
| `scripts/start_replica.sh` | **FIXED-THEN-RAN** | See defect 4 — it did not start a standby on this machine at all before the fix. |
| `scripts/wait_for_replica.py` | RAN | Reports streaming state, byte lag from the primary and receive/replay LSNs from the standby. |
| `python/repl_lab.py` | **FIXED** (imported) | See defect 5. |
| `python/stale_reads.py` | **FIXED-THEN-RAN** | ~51 s (was **13+ minutes**; defect 7). At `APPLY_DELAY=2s`: 100% stale at every think time below the delay, 0% at 2,500 ms. At `APPLY_DELAY=0`: 96.5% stale at 0 ms think time, 0% at 50 ms — and 1.2% under write load, which is the staging-versus-production lesson in one number. |
| `python/lsn_token.py` | **FIXED-THEN-RAN** | ~8 s. See defect 8: the shipped experiment could not tell `sticky` from `lsn`. It now prints a second table where it can. |
| `python/lag_monitor.py` | **FIXED-THEN-RAN** | ~38 s. At `APPLY_DELAY=0` the central claim is measured exactly: over an idle 12 s, replay lag stayed at **0 bytes** while `seconds behind` grew **49.0 → 60.1**. Burst phase peaked at 17.8 MB of replay lag and drained. See defect 9 for what it used to print at `APPLY_DELAY=2s`. |
| `WAIT FOR LSN` (fix 3 of 3) | **FIXED-THEN-RAN** on `postgres:19beta3` | A PG19-beta primary + standby pair was built for it. The row is now measured: **0 stale, 100% of reads on the replica, p50 2,007.5 ms** — the only one of the four fixes that is correct *and* keeps the replica serving, and the latency it charges is exactly the standby's 2 s apply delay. Two defects fell out of the first execution of that code path, 18 and 19. Beta numbers, recorded as beta numbers. |

## Defects found and fixed

Nine on the first pass. Four of them were code that had never been executed.
**Ten more on the unblock pass**, listed in their own section further down —
bringing the total to nineteen, ten of which were code or commands that had never
run once.

**1. `03-indexes/sql/01_skip_scan.sql` — case A could not demonstrate skip scan,
on any Postgres version.** Case A builds `(status, customer_id)` and queries
`WHERE customer_id = 4242`, but the lab seed already carries
`idx_orders_customer` on exactly that column, so the planner served the query
from the seed index and the case under test was never exercised. The script's own
comment on the *third* case shows the author knew about this trap and did not
apply it to case A. Fixed by dropping the seed index for the duration of case A
inside a transaction that is then `ROLLBACK`-ed — DDL is transactional in
Postgres, so a failure mid-script cannot leave the lab without its index. Case A
now reads 1,083 buffers through the two-column index versus 23 through the seed
index, which is the pre-18 baseline the file says it is showing.

**2. `04-reading-a-query-plan/cpp/plan_protocol.cpp` — the results table
attributed one statement's plan to a different statement.** The
`PQexecPrepared (named)` row printed `GENERIC` three lines below a catalogue
dump reading `cpp_ps generic=0 custom=300`. The timing came from `cpp_ps` driven
300× with the *rare* value; the plan came from a SQL-level twin driven 6× with
the *common* one. A code comment disclosed the substitution; the output did not.
Fixed by printing both, labelled — which turns a contradiction into the better
finding: the server keeps the custom plan for the rare value and switches for the
common one, so **which parameter value arrives first decides the plan the others
get**.

**3. `04-reading-a-query-plan/sql/plans/06` and `07` did not run in `psql`,**
though the README says every file in that directory does. Both select from
`plan_drill_corr`, a table only `plan_drill.py` creates — and drops again on the
way out. Fixed by giving both files a `CREATE TABLE IF NOT EXISTS ... AS` block
before the `-- @explain` marker. All ten files now run standalone; the program
path is unchanged and still produces the 8x-low estimate and its repair.

**4. `08-replication-lag/scripts/start_replica.sh` did not start a standby on
macOS.** `pg_basebackup` succeeded, then `pg_ctl start` failed with
`FATAL: postmaster became multithreaded during startup` /
`HINT: Set the LC_ALL environment variable to a valid locale`. The primary is
started by `brew services`, which sets a locale; a standby started from an
interactive shell inherits whatever you have, which here is nothing. Fixed with
`export LC_ALL="${LC_ALL:-C}"` at the top of the script, with the Darwin reason
written down beside it. Verified end to end afterwards, twice, at two different
apply delays.

**5. `08-replication-lag/python/repl_lab.py` — the probe table did not exist on
the standby yet.** All three replica programs died on
`psycopg.errors.UndefinedTable: relation "repl_probe" does not exist`, because
`CREATE TABLE` on the primary was followed immediately by a read on a standby
running `recovery_min_apply_delay = 2s` — **DDL replicates through the WAL like
everything else**. Fixed by having `ensure_probe_table` wait for the DDL to be
*replayed* (not received) before returning, with a bounded timeout and an
unblock message. The failure is now impossible and the reason for it is in the
docstring, since it is the topic's own lesson arriving early.

**6. `07-connection-pools/python/retry_storm.py` does not reproduce
metastability on this machine — and could not have, as shipped.** This is the
most important line in this document, so it is stated plainly: **the README says
"the first should stay dead … and you will have watched it happen", and on this
machine it does not happen.** Across five runs at the shipped defaults and at
three harsher knob settings, both variants recovered every time except one, and
that one had it *backwards* (A recovered, B did not) — noise, since B refuses
retries and can only ever offer the server less load than A.

There is a mechanical reason, not just a tuning one. Retries can only sustain an
overload if a retried request costs the bottleneck something. In this program the
failure is a **client-side pool timeout**: the request never got a connection, so
the server never saw it, so the retry adds nothing to the server's load. A
work-conserving server drains the instant offered load falls below capacity, and
that is exactly what was measured. Fixes applied:

- a `statement_timeout` at 1.5x the measured service time, so queued queries are
  cancelled *after* burning server CPU — wasted work is the amplifier a retry
  storm needs, and `STATEMENT_TIMEOUT_MS=0` turns it off so the difference is
  visible;
- the closing prose now covers **all four** outcomes, names which one this run
  got, and never prints the textbook conclusion for a run that did not produce
  it. Previously it printed "if both recovered … if neither recovered", neither
  of which described what had happened;
- the topic README's `How to run` section now says what to expect here.

Even with the amplifier, this single machine — load generator, driver and
Postgres sharing eight cores — did not reach the metastable state. That is
recorded as a limit of the lab, not smoothed over.

**7. `08-replication-lag/python/stale_reads.py` ran for over thirteen minutes.**
200 request pairs × think times of 0/50/250/1,000/2,500 ms is 760 s of sleeping
before phase 2 begins. It was killed at 600 s during verification. Fixed by
sizing each row to a time budget (`ROW_BUDGET_MS`, default 8 s) instead of a
fixed count, and printing the sample size actually used per row — a rate over 6
samples and a rate over 200 are different evidence and the table now says which
you are reading. Runtime is 51 s. Two header lines that hard-coded "2 seconds"
now read the standby's actual `recovery_min_apply_delay`, because the file is
meant to be run at two different ones.

**8. `08-replication-lag/python/lsn_token.py` could not distinguish the fix it
argues for.** With the read fired immediately after the write, `sticky` and
`lsn` both scored 0% stale and **0% of reads on the replica** — identical, and no
reason visible for plumbing a token through an application. The measured table
contradicted the prose beneath it, which called `lsn` "correct AND it keeps
whatever share of reads the replica can actually serve". Fixed by adding a second
table with a `PAUSE_MS` (default 2,500 ms) gap between write and read — the
ordinary case of a user doing something else before refreshing. There the two
separate cleanly and the measured numbers now support the argument: **sticky 0%
of reads on the replica, lsn 100%, both at 0% stale**. The pause is paid once per
row rather than once per request, so the whole file still runs in 8 s.

**9. `08-replication-lag/python/lag_monitor.py` asserted a conclusion its own
numbers did not support.** Run against the default 2 s-delay standby, phase 1
printed `seconds behind 2.1 → 2.2` and then declared "the seconds figure grew
because no new transaction arrived" — but it had barely grown, and what pinned it
was the apply delay, not the idle primary. Fixed: the program now checks
`recovery_min_apply_delay`, and when it is non-zero it says the phase is BLOCKED
and prints the `APPLY_DELAY=0` commands, rather than narrating an effect it did
not measure. At `APPLY_DELAY=0` the effect is unmistakable and is now the
recorded result: **0 bytes of replay lag throughout, `seconds behind` 49.0 →
60.1 over twelve seconds**. The topic README's `How to run` now says which
program wants which delay.

## Unblock pass — Docker daemon up (2026-08-19)

Everything in this section was run **after** the Docker daemon came up, against
the compose stack rather than the host Postgres, on the same day as the pass
above. Ten more defects, and this time **six of them were in code and commands
that had never executed even once** — three of those made the Docker path
impossible to start at all.

### Environment for this pass

`postgres:18` resolves to **18.6**, `postgres:19beta3` to **19beta3**,
`edoburu/pgbouncer` to **PgBouncer 1.25.2**, `grafana/k6` to k6 in a container.
The Docker VM has **4 CPUs and 4.8 GB** — half the host's cores. Every
containerised latency figure below is a number about that VM, and two other
projects' compose stacks were resident on the same daemon throughout; where a
number is sensitive to that it says so.

Host ports 55432 and 55433 (the ports `lab/README.md` documents) were occupied by
another layer's stack, so this pass used a small `!override` port remap to
55532/55533. Nothing inside the compose network changed, and the remap is not
committed — it was an accident of this machine on this day.

Seeding the compose primary took **30.1 s**: 50,000 customers (7.7 MB),
1,000,000 orders (101.3 MB), 3,000,000 line items (328.1 MB). `check_env.py`
against 18.6 reports skip scan, `Index Searches` and `BUFFERS`-by-default as
`available`, and `WAIT FOR LSN` as the only server capability still BLOCKED —
correct on every line.

### What the newly-runnable experiments actually showed

**Skip scan is real and the file's description of it was wrong in three places.**
On 18.6, `(status, customer_id)` queried on `customer_id` alone reports
`Index Searches: 9` over 47 buffers in 0.037 ms. The same shape against a
50,000-value leading column reports `Index Searches: 1` — the scan descended
once and read the leaf level straight through, which is the planner declining to
skip. `1` versus `9` is the whole demonstration and it is crisp. See defect 15
for what the file said instead.

**PgBouncer converts a server rejection into a bounded server pool.** Identical
sweep, one substitution in `LAB_DSN`. Direct at `pool_size = 100`: 41 req/s,
p99 4,635 ms, **413 × `server: too many clients`**, 88.0 mean active backends.
Through PgBouncer: 42 req/s, p99 4,399 ms, **0** rejections, 15.7 active. The
application-side pool is unchanged in both; only what reaches Postgres differs.

**The cgroup quota experiment works and is stark.** At `cpus: 0.5`,
`os.cpu_count()` says 4 and `cpu.max` says 0.5 — 9 workers against 2, 135
possible connections against 30, and 1,050 connections' worth of difference
across ten replicas. Repeated at 1.0 and 2.0.

**Head-of-line blocking through a shared pool, measured over HTTP.** `bulkhead.js`
against the api at `POOL_SIZE=2 MAX_OVERFLOW=0`, fast traffic identical in both
runs: with slow traffic at 1 req/s the fast endpoint is p95 3.6 ms / p99 9.77 ms;
with slow traffic at 40 req/s it is **med 7.16 s, p95 17.72 s, p99 23.1 s**. The
fast query never changed.

**Read-your-own-writes, over HTTP, all three routings.** `stale_reads.js` against
a standby at `recovery_min_apply_delay = 2s`. Reading immediately after the
write: `replica` = 400/400 reads on the replica and **400/400 stale**; `primary` =
0 stale, 0 on the replica; `lsn` = 0 stale, **0 on the replica** — correct, and
buying nothing, because a replica 2 s behind cannot serve a read fired
microseconds after the write however you route it. With a 3,000 ms think time
(`THINK_MS`, added this pass): `lsn` = 0 stale and **390/390 on the replica**,
against `primary`'s 0. That second pair of runs is the argument for the token,
and the script could not express it before.

**`WAIT FOR LSN` exists on PG19beta3 and does what topic 8 says.** Direct probe:
a `WAIT FOR LSN` naming a position past the standby's replay point blocked
**1,854 ms** and the just-written row was then visible. In `lsn_token.py`'s own
table, at a 2 s apply delay, the four fixes finally separate:

| fix | stale | % on replica | p50 |
|---|---|---|---|
| none | 200/200 | 100% | 0.6 ms |
| sticky | 0/200 | 0% | 0.5 ms |
| lsn | 0/200 | 0% | 0.9 ms |
| **wait** | **0/25** | **100%** | **2,007.5 ms** |

`wait` is the only one that is correct *and* keeps the replica serving, and it
charges exactly the apply delay to do it. Beta numbers from a beta server.

One trap worth carrying: a token captured a statement too early named a position
the standby had *already* passed, so `WAIT FOR LSN` returned in 1 ms and the read
was still stale. The token has to come from the write's own transaction.

### Experiments that run and do not demonstrate their claim

Stated plainly, because a clean-looking run is the failure mode here.

**`lab/docker/load/pool_sweep.js` does not produce the knee.** It executes,
reports, and its `constant-arrival-rate` executor is right. But the endpoint it
drives is a 20-row join that costs Postgres very little, so the Python process
saturates before the database does and there is no queue inside Postgres to
find. Swept at `pool_size` 2 / 5 / 10 / 25 / 50 with `max_overflow = 0` and
600 req/s offered, every row delivered ~600 req/s and p99 came out 47.6 / 117.2 /
21.8 / 3,370 / 41.5 ms — not a curve, noise with one outlier. The host-side
`07-connection-pools/python/pool_sweep.py` uses a deliberately CPU-bound
aggregate for exactly this reason and does produce the knee; the k6 script now
carries a note saying which of the two answers which question. Making the k6 path
show the knee needs a database-CPU-bound endpoint on the `api` service, which
does not exist yet — that is a change to the lab's HTTP service, not a fix, and
it was not made.

**`bulkhead.js` at the shipped defaults shows nothing**, and used to look clean
while doing it: five workers × (pool 5 + overflow 10) is 75 connections and the
slow traffic needs 20, so nothing ever waits. It demonstrates its claim
emphatically once the pool is small enough to run out, which is now the first
thing its header says.

### What was missing, and was written

**Topic 7 experiment 4's second half had no program that could measure it.**
Pointing `pool_sweep.py` at PgBouncer and flipping `max_prepared_statements`
between 0 and 200 returns the same numbers both ways — 47 vs 46 req/s, p99 2,671
vs 2,651 ms — which reads like "no effect" and is actually "not measured": every
request in that program runs inside a transaction, and a transaction-mode pooler
pins one server connection for the whole transaction, so a prepared statement can
never land on a connection that lacks it. The failure needs autocommit traffic.
`07-connection-pools/python/pgbouncer_prepared.py` (new) generates it: at
`max_prepared_statements = 200`, 300/300 executions complete; at `0`, the run
dies at execution 126 with `DuplicatePreparedStatement: prepared statement
"_pg3_0" already exists`. That is the topic's "this advice is obsolete on
PgBouncer 1.21+" claim, measured in both directions. It refuses to run against a
direct Postgres, where it would prove nothing.

## Defects found and fixed on the unblock pass

**10. `lab/docker/compose.yml` mounted the Postgres data volume at a path
Postgres 18 does not use.** `postgres:18` moved `PGDATA` to
`/var/lib/postgresql/18/docker` and declares `/var/lib/postgresql` as its
`VOLUME`; the file mounted `primary-data:/var/lib/postgresql/data`, the pre-18
path. The named volume therefore held nothing and the cluster landed in an
anonymous volume — silently, with the file's own macOS note about named volumes
satisfied on paper and defeated in fact, and the 30-second seed thrown away on
any `down`. Both services now mount the parent, with the reason beside them.

**11. PgBouncer never became healthy and nothing could reach it.** The image
listens on 5432 unless told otherwise; compose published `6432:6432` and
healthchecked `127.0.0.1:6432`. So the published port mapped to a closed port,
every host connection was refused (`server closed the connection unexpectedly`),
and the container sat in `health: starting` failing its check forever. Fixed with
`LISTEN_PORT: "6432"`, which keeps the host port `lab/README.md` documents.

**12. The primary would not accept a replication connection, so topic 8's Docker
path could not start at all.** The postgres image's entrypoint appends exactly one
rule — `host all all all scram-sha-256` — and `all` there means all *databases*,
which does not include the `replication` pseudo-database; the only replication
rules `initdb` writes are for `127.0.0.1` and `::1`. A standby in another
container is neither, so `pg_basebackup -h postgres-primary` failed with
`FATAL: no pg_hba.conf entry for replication connection from host "172.21.0.5"`
and the `postgres-replica` service **exited before it had a data directory**.
Fixed with `lab/docker/postgres/init/00_replication_hba.sh`, scoped to the
replication role and left on scram rather than trust. Verified from a genuinely
empty volume afterwards: one `docker compose up -d postgres-replica` now clones
and reaches `streaming`.

**13. The documented unblock command for the cgroup experiment could not change
the quota.** `docker compose run --rm -e CPUS=0.5 api python worker_count.py`
sets `CPUS` *inside the container*. Compose interpolates `${CPUS}` into the
service's `cpus:` limit while parsing the file, so the value has to be in
*compose's* environment. Run as documented, `cpu.max` reads `200000 100000` at
every setting and all three rows of the experiment print identical numbers —
which looks exactly like the experiment working. Correct form is
`CPUS=0.5 docker compose … run --rm api python worker_count.py`; fixed in
`worker_count.py` (both copies) and in the topic 7 README.

**14. The same class of bug in the k6 sweep, twice over.** `pool_sweep.js`
documented `docker compose run --rm -e POOL_SIZE=25 k6 run …` — which sets
`POOL_SIZE` in the *load generator*, a process that never reads it, while the api
keeps its default pool. Worse, and this one bites even when the variable is in
the right place: **`docker compose run` starts the target's dependencies and
re-creates `api` from whatever compose can see at that moment**, so a `POOL_SIZE`
set on an earlier `up` is reverted to the default before the load starts.
Measured directly: after `-e POOL_SIZE=25`, `docker compose exec api env` reports
`POOL_SIZE=5 MAX_OVERFLOW=10`. A sweep run this way measures one pool size five
times. Both k6 script headers now carry the correct commands and the
`exec api env` check to confirm them.

**15. `03-indexes/sql/01_skip_scan.sql` made three claims PG18.6 contradicts.**
(a) "Four probes is a skip scan doing its job" — the server reports **9** on an
Index Scan and 5 on an Index Only Scan for the same four distinct values.
(b) Case B's prose says high cardinality is "why the planner refuses and reads
the table sequentially instead", and the `enable_seqscan = off` block below it
promises a comparison — but the two candidate plans here cost within ~1% of each
other (bitmap 15,280–15,525 against parallel seq scan 15,487), so which one
appears **flips between `ANALYZE` runs on unchanged data**, and when the bitmap
wins, `enable_seqscan = off` reprints the same plan and compares it against
itself. Now three plans, and the invariant named: `Index Searches: 1` whenever
the index is used, whatever node it is wearing. (c) "skip scan is EQUALITY-only …
If a skip scan appeared in case A and not here, that is the rule, measured" —
it appeared. `(status, created_at)` with `created_at > …` skips: `Index
Searches: 5`. The block is now the low- vs high-cardinality comparison it should
always have been, and the topic README's "skip scan does not apply to ranges"
line is corrected with it. The README's own experiment 2 had it right all along
and the SQL file contradicted it.

**16. Topic 3's central `Cond` vs `Filter` signal does not exist**, on 18.6 or on
17.5. The README says `WHERE a = 1 AND c = 3` on `(a,b,c)` shows "a large
`Rows Removed by Filter` under an index scan", and `column_order.py` closed with
"everything after [a range] in the column list can only be a filter". Measured on
both servers: every qual lands in `Index Cond` and `rows removed` is **0** in
every row of every configuration — a B-tree evaluates a qual on any indexed
column inside the index, and `Filter` is for quals the index cannot evaluate at
all, which is why the only `Filter` in the whole table sits under a `Seq Scan`.
The rule the topic teaches is still right; the *evidence* for it is `Buffers` —
26 for `eq + range` against 190 for `range + eq` on the same index — and both
files now point there.

**17. `plan_drill.py`'s table collided its two most important columns.** `est`
and `act` were 9 wide, so six-figure row counts with thousands separators ran
together: `173,125138,889.0`, `416,667333,333.33`. Widened, and `act` rounded to
one decimal.

**18. `lsn_token.py`'s `WAIT FOR LSN` strategy had never been executed and did
not work.** `Router.write()` captured the LSN token only `if self.strategy ==
"lsn"`, but `wait` hands the same token to `WAIT FOR LSN`. On the first PG19
server that could reach that branch, the row died with
`invalid input syntax for type pg_lsn: "None"`. One-line fix; the row is now the
most interesting one in the table.

**19. `lsn_token.py` took 6 min 53 s on PG19**, against the ~8 s the notes
record, because `wait` blocks for the standby's whole apply delay on every one of
200 requests — 400 s in one row. Sized separately (`WAIT_REQUESTS`, default 25)
and the table now prints the sample size **per row**, so a rate over 25 samples
is never read as a rate over 200. Runtime 1 min 02 s.

## Still blocked, honestly

| Item | Why | What it would need |
|---|---|---|
| `lab/local/teardown_lab.py` | Deliberately not run: it drops `sep_lab_03_data`, the database most rows above were verified against. | `python3 lab/local/teardown_lab.py`, when you are done with the layer. |
| `01-isolation-levels/sql/write_skew_session_*.sql` | Blocked by design, not by environment. The header says "do NOT run this with `-f`" — running them as scripts removes the interleaving that is the entire demonstration. | Two terminals and a human alternating the steps. |
| k6 on the host | Still not installed, and deliberately not installed. | `brew install k6` — unnecessary: the compose `k6` service ran every load script this pass. |
| The knee, via `lab/docker/load/pool_sweep.js` | Its endpoint is too cheap to saturate Postgres before the API process saturates. | A database-CPU-bound endpoint on the `api` service. The host-side `pool_sweep.py` answers the same question today. |
| PG19 as anything but a probe | `postgres:19beta3` was used for `WAIT FOR LSN` only, with a tiny unseeded schema and a second seeded pair for `lsn_token.py`. | Nothing, for this layer's purposes — but every PG19 number here is a beta number and should be re-taken at GA. |

## Coverage

Every topic has code, and every topic has code in every language its
`How each language actually gets there` section covers.

| Topic | Languages the README commits to | Present |
|---|---|---|
| 01 | Python, Go | Python ×4, Go ×1 ✓ |
| 02 | Python | Python ×4 ✓ |
| 03 | Python (+ SQL) | Python ×1, SQL ×3 ✓ |
| 04 | Python, Go, Node, C++ | all four, + 10 SQL files ✓ |
| 05 | Python | Python ×3, SQL ×1 ✓ |
| 06 | Python, Node | Python ×3, Node ×1 ✓ |
| 07 | Python, Go, Node | Python ×6, Go ×1, Node ×1 ✓ (one added: `pgbouncer_prepared.py`) |
| 08 | Python | Python ×5, SQL ×1, scripts ×2 ✓ |

Rust, C++ and Java are absent from seven of the eight topics on purpose, and each
topic states its reason: the mechanism is in the server, and a second client
would print the same table twice. Topic 4 is where a client's own behaviour *is*
the finding, and there all four of its languages are present and were run.

**No topic is incomplete.** Topic 7 was, until the unblock pass: experiment 4's
second half had no program that could measure it. It has one now.

## Portability

Nothing in this layer reads `/proc`, includes `<sys/epoll.h>`, or touches a
cgroup path on the host. The one cgroup-dependent experiment
(`07-connection-pools/python/worker_count.py`) detects Darwin, refuses, and
prints the container command — which is the correct behaviour and was verified,
and the command it prints has since been corrected (defect 13) and run at all
three quotas inside a container. The one genuine macOS incompatibility found is
defect 4, and it is fixed. Nothing in this layer's Docker path depends on the
host being macOS; the three defects that stopped it starting (10, 11, 12) were
version and configuration errors, not platform ones.

## No invented numbers

The `Predict, then record` tables in all eight topic READMEs were checked and are
**blank**; the only digits inside them are row labels (`| 0 |`, `| 12 |`,
`| lazy | 1000 |`). The one README that shows an output table
(`02-mvcc-and-vacuum`) uses `<yours>` placeholders. No topic README states a
measured result. **Re-checked after the unblock pass**, including the topic 3 and
topic 7 READMEs that were edited during it: every answer cell in all eight tables
is still empty, and the only digits inside them are row labels.

Every number in this file was observed on 2026-08-19 — the host-Postgres rows on
the machine described at the top, the containerised rows in a 4-CPU Docker VM on
that same machine, and the `WAIT FOR LSN` rows on a Postgres 19 **beta**. They
belong to this machine, not to yours.
