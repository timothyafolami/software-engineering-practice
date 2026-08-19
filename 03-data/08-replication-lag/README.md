# Layer 3 · Topic 8 — Replication lag, read-your-own-writes, and the one-way doors

### The takeaway (read this first)

**The one idea:** the moment you add a read replica you have a distributed
system, and "I just wrote this and now it's gone" becomes a *supported behaviour*
of your architecture rather than a bug in it.

**Why it matters in practice:** adding a read replica is the standard answer to a
latency problem, which makes it the standard way to introduce a new class of bug
into a service that already had a latency problem. Know the failure and the fixes
*before* you reach for it, because afterwards you will be debugging under load
with a user insisting the data was there a second ago.

**You'll know it landed when:** you can name three fixes for read-your-own-writes
and the exact cost of each, and explain why a shard key is a one-way door in
terms of specific queries that stop being possible.

## The concept

**Streaming replication** ships WAL to a standby, which replays it. It is
asynchronous by default, so the replica is behind by milliseconds usually and by
minutes occasionally. Measure lag in **bytes, not seconds**:
`pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)` from `pg_stat_replication` on
the primary. Seconds-based measures are derived from timestamps in the WAL and
mislead badly on an idle primary — with no new writes, the replica is perfectly
caught up and the "seconds behind" number grows anyway.

**Read-your-own-writes** breaks when a user POSTs (to the primary) then GETs
(from a replica) inside the lag window. Three fixes, three costs:

1. **Route reads to the primary for N seconds after a write.** Cheap, coarse, and
   wrong at both edges — you either over-route (losing the replica's benefit
   entirely for those N seconds) or under-route (still stale).
2. **LSN token.** After the write, `SELECT pg_current_wal_lsn()`; carry that token
   in the session; on read, compare it against the replica's
   `pg_last_wal_replay_lsn()` and fall back to the primary if the replica is
   behind. Correct and precise. The cost is plumbing a token through every layer
   of your application, including the ones that do not know they are in a
   distributed system.
3. **`WAIT FOR LSN`** — new in **Postgres 19** (in beta as of mid-2026): the
   replica blocks until it has replayed to your LSN. MySQL has had
   `SOURCE_POS_WAIT()` for years, and its arrival in Postgres genuinely changes
   the standard advice here. The cost: you pay the lag as *latency* instead of as
   *staleness*, and you need a timeout, because "block until caught up" on a
   badly lagging replica is an outage with good intentions.

**A finding that defeats naive reasoning.** Jepsen's April 2025 analysis of
[Amazon RDS for PostgreSQL 17.4](https://jepsen.io/analyses/amazon-rds-for-postgresql-17.4)
found **Long Fork** and G-nonadjacent anomalies violating *snapshot isolation* on
Multi-AZ clusters with read-only secondaries — under healthy conditions, with no
faults injected, on every version tested from 13.15 through 17.4. The cause is
worth understanding rather than filing: the primary orders visibility using an
in-memory lock, while secondaries derive it from WAL order, and the two disagree
about transaction ordering. RDS Multi-AZ therefore offers something closer to
*parallel* snapshot isolation than to the thing the name implies. The mitigations
are to route safety-critical transactions to the writer endpoint and to ensure
every safety-critical transaction contains at least one write.

**The lesson generalises well past RDS: the isolation level you configured is a
property of the topology you deployed, not just of your `SET TRANSACTION`
statement.** Everything you learned in [Topic 1](../01-isolation-levels/README.md)
was about one node.

**Partitioning, then sharding, then the door.** Declarative partitioning (RANGE
by `created_at`, automated with `pg_partman`) splits one table across many
partitions on **one node**. The payoff is cheap bulk deletion — `DROP` an old
partition instead of running a `DELETE` that generates millions of dead tuples
for [Topic 2](../02-mvcc-and-vacuum/README.md)'s vacuum to chase — plus partition
pruning. The sharp edges: pruning happens only when the **partition key is in the
`WHERE` clause** and comparable at plan time; every unique constraint and primary
key **must include the partition key**; and planning time grows with partition
count.

**Sharding** distributes across *nodes* (Citus is the mature Postgres option).
The shard key is a one-way door because it is not a performance decision — it is
a statement about which queries remain possible. Cross-shard joins, global unique
constraints, global `ORDER BY ... LIMIT` and cross-shard transactions all become
much harder or much slower. Pick high cardinality and even distribution
(`tenant_id`, `user_id` — not `country`, which in this lab's own seed is ~38% one
value), and note that re-sharding means moving all of your data while serving
traffic.

**When not to use a relational database, honestly.** The consensus has moved
*toward* "just use Postgres": extensions cover full-text search, vectors
(`pgvector`), time-series, JSON and queues well enough that a second datastore's
operational cost usually loses. The honest exceptions are sustained writes beyond
what one node's WAL can absorb; working sets that are genuinely a cache (a
session store in Postgres puts vacuum in your login path); analytical scans over
billions of rows, where columnar storage wins by an order of magnitude that no
index closes; and vector search past the scale where recall and latency need a
dedicated ANN engine. Move on a *measured* requirement, not in anticipation of
one.

## How each language actually gets there

**Python only.** Lag is a property of the topology, and the three fixes are
routing decisions in application code — a Go or Node implementation would be the
same `if` statement in different syntax. The one client-shaped detail worth
stating without a program: whichever language you use, the routing decision must
live **below** your ORM session, or half your code paths will bypass it. In
SQLAlchemy that is a `Session` bound to a routing engine or a
`get_bind()` override; the equivalent hook exists in most ORMs, and reaching for
"I'll just use the replica engine here" at each call site is how a service ends
up with three different consistency behaviours nobody documented.

## The experiment

Needs two Postgres instances. Use the Docker path in
[`lab/README.md`](../lab/README.md) — `postgres-primary` and `postgres-replica` —
or a second local cluster on another port.

1. **Deterministic lag.** Bring up the streaming replica with
   `recovery_min_apply_delay = '2s'`. This makes the lag *deterministic* rather
   than flaky, which is the entire reason to do it this way — a bug you can
   reproduce on demand is a different object from one you can only observe.
2. **Break it.** `POST /orders`, then immediately `GET /orders/{id}` against the
   replica; record the stale-read rate. Then set the delay to 0 and raise write
   volume until lag appears naturally — and record how much harder the same bug
   is to see. That comparison is the staging-versus-production lesson in one
   measurement.
3. **Fix it three ways** and measure each: sticky-to-primary-for-5s; LSN token
   with primary fallback; and, if you build a PG19 beta container, `WAIT FOR
   LSN`. Record stale reads, **the share of read traffic that still reached the
   replica** — that is how much of the replica's value each fix costs you — and
   p99.
4. **Monitor properly.** Sample `pg_stat_replication` byte lag on the primary and
   `pg_last_xact_replay_timestamp()` on the replica into a CSV, and explain in
   your notes why the second is misleading on an idle primary.
5. **Partition pruning.** Convert `orders` to monthly RANGE partitions. Run a
   query filtering on `created_at` (should prune — check `Subplans Removed: N` in
   the plan) and one filtering only on `id` (should touch every partition). Add
   an `ORDER BY created_at DESC LIMIT` query and check whether pruning survives
   it. Measure planning time at 12, 36 and 120 partitions.
6. **The one-way door, written not run.** Pick a shard key for your *real*
   production schema and write down: (a) three current queries that become
   cross-shard, (b) the unique constraint you can no longer enforce globally, and
   (c) the migration plan to change the key later. Two pages. The roadmap's
   writing layer is not separate from this one.

## How to run

Experiment 5 needs **one** Postgres and runs here as-is:

```
psql -q -d sep_lab_03_data -f 08-replication-lag/sql/partition_orders.sql
python3 08-replication-lag/python/partition_pruning.py
```

Experiments 1–4 need **two**. Either bring up a local standby on port 5433 —

```
bash 08-replication-lag/scripts/start_replica.sh          # APPLY_DELAY=2s by default
export LAB_REPLICA_DSN="postgresql://?host=/tmp&port=5433&dbname=sep_lab_03_data"
python3 08-replication-lag/scripts/wait_for_replica.py
python3 08-replication-lag/python/stale_reads.py
python3 08-replication-lag/python/lsn_token.py
python3 08-replication-lag/python/lag_monitor.py
bash 08-replication-lag/scripts/start_replica.sh --stop   # when you are done
```

— or use the Docker stack in [`lab/README.md`](../lab/README.md), which pins
Postgres 18 and gives you `postgres-primary` and `postgres-replica`.

`start_replica.sh` is a **script you run**, not something a program does to your
machine: `pg_basebackup` copies the whole cluster and `pg_ctl start` launches a
second daemon, and neither belongs in a side effect. Everything it creates lives
under `$TMPDIR/sep_lab_03_replica`, and `--stop` removes it and drops the
replication slot.

**Without `LAB_REPLICA_DSN` the three replica programs refuse to run** and print
the unblock command. That refusal is deliberate: a version that silently read
from the primary would report zero stale reads and teach you the opposite of the
truth. They also check `pg_is_in_recovery()` on whatever you point them at, for
the same reason.

`python/repl_lab.py` holds the shared helpers and is imported, not run — its
docstring is the one to read before the results, because it states the two
distinctions everything else depends on: **bytes not seconds**, and **replayed
not received**.

`sql/partition_orders.sql` does the partitioned-table build separately from the
measurement so you can read the DDL as its own artifact. Both it and
`partition_pruning.py` build their own tables (`orders_part`,
`orders_part_bench`) rather than converting the seeded `orders` — Topics 3, 4 and
6 read that table, and repartitioning it underneath them would silently change
every plan they teach.

Knobs: `LAB_REPLICA_DSN`, `APPLY_DELAY`, `REPLICA_PORT`, `REPLICA_DIR` for the
replica; `REQUESTS`, `MIN_REQUESTS`, `ROW_BUDGET_MS`, `THINK_MS`, `WRITE_BURST`,
`STICKY_MS`, `PAUSE_MS`, `WAIT_TIMEOUT_MS` for the routing experiments;
`PHASE_S`, `WRITE_RATE`, `BURST_ROWS`, `LAB_OUT` for the monitor;
`PARTITION_COUNTS`, `REPEATS`, `KEEP` for pruning.

**Run the three replica programs at two different `APPLY_DELAY` settings, and
not the same one.** `stale_reads.py` and `lsn_token.py` want the default
`APPLY_DELAY=2s`: the lag has to be larger than a request to produce a stale
read on demand. `lag_monitor.py` wants `APPLY_DELAY=0`, because its whole
finding is the *idle* primary — byte lag pinned at zero while the seconds-behind
figure grows about a second per second — and a deliberate apply delay pins the
seconds figure at the delay instead and hides it. Run it the wrong way round and
the program says so rather than letting you read the delay as the result:

```
bash 08-replication-lag/scripts/start_replica.sh --stop
APPLY_DELAY=0 bash 08-replication-lag/scripts/start_replica.sh
python3 08-replication-lag/python/lag_monitor.py
```

`stale_reads.py` sizes each row of its table to a time budget rather than a
fixed count (`ROW_BUDGET_MS`, default 8s) — 200 pairs at a 2,500ms think time is
eight minutes of sleeping for one line — and prints the sample size it used per
row. `lsn_token.py` prints two tables: with the read fired immediately after the
write, `sticky` and `lsn` are indistinguishable (both correct, both sending 0%
of reads to the replica); the second table pauses `PAUSE_MS` between write and
read, which is where the token earns its keep and the sticky window does not.

**Two honest limits on this machine.** Experiment 3's third fix needs Postgres
19, which is in beta — build it as a separate container and treat its numbers as
beta numbers, or skip it and record why. And experiment 5's partition counts make
planning time the thing being measured; run it on an otherwise idle machine or
the noise is larger than the effect.

## Predict, then record

Before running: the stale-read rate at 2s lag with an immediate follow-up read.
How much read traffic sticky-for-5s pushes back to the primary. Whether pruning
survives `ORDER BY ... LIMIT`. Whether planning time from 12 → 120 partitions is
linear or worse.

| Fix | stale reads | % reads on replica | p50 | p99 |
|---|---|---|---|---|
| none |  |  |  |  |
| sticky 5s |  |  |  |  |
| LSN token |  |  |  |  |
| WAIT FOR LSN (PG19) |  |  |  |  |

| Lag source | byte lag | seconds lag | stale-read rate |
|---|---|---|---|
| recovery_min_apply_delay = 2s |  |  |  |
| natural lag under write load |  |  |  |
| idle primary |  |  |  |

| Partitions | plan time (pruned) | plan time (unpruned) | exec time |
|---|---|---|---|
| 12 |  |  |  |
| 36 |  |  |  |
| 120 |  |  |  |

**Broken experiment, not wrong prediction, if:**

- **Zero stale reads at `recovery_min_apply_delay = '2s'`.** Your reads are not
  reaching the replica. Confirm with `pg_is_in_recovery()` *inside the read path*,
  not from a psql session you opened by hand.
- **100% stale.** The replica is not replaying at all. Check
  `pg_stat_wal_receiver` before concluding anything about lag.
- **The LSN-token fix still shows stale reads.** Either you captured the LSN
  *before* commit rather than after, or you are comparing against
  `pg_last_wal_receive_lsn()` — received — instead of
  `pg_last_wal_replay_lsn()` — applied. That distinction is the entire bug, and
  it is the one people ship.
- **Pruning appears to work on the `id` query.** Check `Subplans Removed` in the
  plan rather than trusting timing; a fast query over 12 partitions proves
  nothing.
- **Byte lag stays at zero throughout.** The write volume is too low to produce
  WAL faster than it ships. That is a healthy system, not a measurement.

## Answer before moving on

1. Give the precise definition of what your users can observe under "eventually
   consistent," in terms of two specific operations and a time window.
2. Jepsen found a primary and its secondaries disagreeing on transaction order.
   Why does "ensure every safety-critical transaction includes a write" help,
   mechanically?
3. Partitioning gives you cheap deletes. Connect that to Topic 2 and name exactly
   which cost disappears.
4. Argue *against* sharding a system at 5 million orders and 20 million line
   items. Then state the one measurement that would change your mind.

## Further reading

- [PG18 docs §27, High Availability and Replication](https://www.postgresql.org/docs/18/high-availability.html)
- [PG18 docs §5.12, Table Partitioning](https://www.postgresql.org/docs/18/ddl-partitioning.html)
- [Jepsen: Amazon RDS for PostgreSQL 17.4](https://jepsen.io/analyses/amazon-rds-for-postgresql-17.4) — read it in full
- [Brandur: scaling Postgres with read replicas and WAL](https://brandur.org/postgres-reads) — the LSN-token pattern, still the clearest explanation of it
- [Citus: partitioning and sharding in Postgres](https://www.citusdata.com/blog/2023/08/04/understanding-partitioning-and-sharding-in-postgres-and-citus/)
- Petrov, *Database Internals*, Part II — replication, failure detection and consensus, which is the layer under everything above

## Next up

That is the layer. The natural continuation is
[Layer 4 — Distributed systems](../../04-distributed/README.md), which starts
exactly where this topic ends: you now have two nodes that disagree, and the rest
of that layer is about what you can still promise anyone.
