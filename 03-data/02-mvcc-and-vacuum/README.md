# Layer 3 · Topic 2 — MVCC, and what vacuum falling behind does to latency

### The takeaway (read this first)

**The one idea:** Postgres never updates a row in place — an `UPDATE` writes a
new version and leaves the old one for cleanup — and cleanup is blocked, for
*every* table in the cluster, by the oldest transaction still running anywhere.

**Why it matters in practice:** this is the canonical "the database got slowly
slower and nobody deployed anything" incident. One connection stuck
`idle in transaction`, or one replication slot whose consumer died, pins the xmin
horizon; autovacuum runs and reclaims nothing; every scan starts walking past
corpses. If your latency problem has no obvious cause in your code, check this
before you check your code.

**You'll know it landed when:** given a p99 rising over six hours on flat
traffic, `n_dead_tup` is one of the first three things you ask for, and you can
name three distinct things that hold back the xmin horizon — and say which column
of `pg_stat_activity` distinguishes them.

## The concept

Every row version carries `xmin` (the transaction that created it) and `xmax`
(the one that deleted or superseded it). A snapshot decides visibility by
comparing those against the transactions that were in flight when the snapshot
was taken. That is the whole trick, and it is why readers never block writers:
a reader is looking at versions, not at locks.

The cost is that dead versions must be reclaimed, and only versions that no
*possible* current snapshot could still need. That bound is the cluster's **xmin
horizon**, and it is held back by:

- a **long-running or idle-in-transaction session** — with an important
  qualification, below;
- a **replication slot** whose consumer is behind or gone;
- **`hot_standby_feedback = on`** with a standby running long queries;
- **prepared (two-phase) transactions** that were never committed or rolled back.

Any one of these, anywhere in the cluster, stalls cleanup **everywhere** — in
tables the offending session has never touched.

Concretely, what that does to latency, in three separate mechanisms:

1. The table bloats, so sequential scans read more pages for the same rows.
2. Index entries pointing at dead tuples remain, so index scans fetch heap pages
   only to discard what they find there.
3. The **visibility map** stops being all-visible, so **index-only scans quietly
   stop being index-only.** This is the sneaky one: the plan does not change,
   `EXPLAIN` still says `Index Only Scan`, and the `Heap Fetches:` line goes from
   0 to millions.

**Recently changed, and it matters.** Postgres 17 replaced vacuum's dead-TID
array with `TidStore`, removing the 1GB `maintenance_work_mem` ceiling that used
to force multiple index passes on big tables — "keep tables small enough that
vacuum fits in 1GB" is now stale advice. Postgres 18 adds **eager freezing**
(ordinary vacuum proactively freezes all-visible pages, governed by
`vacuum_max_eager_freeze_failure_rate`), spreading anti-wraparound work out
instead of saving it for one emergency, plus `autovacuum_vacuum_max_threshold`,
a hard cap on dead tuples so that huge tables get vacuumed on an absolute count
rather than on a percentage that never trips.

## How each language actually gets there

**Python only.** Every mechanism here is server-side — the xmin horizon, the
visibility map, vacuum's cutoff — and a second client would issue identical SQL
and print identical numbers. What *is* client-shaped is the **cause**, so that is
what the per-language notes cover; none of them needs its own program.

**Python** is where this bug is easiest to write, and the reason is a default.
SQLAlchemy begins a transaction **lazily on the first statement, including a
read**, so a `Session` that only ever `SELECT`s still holds a transaction open
until it is closed. Add an HTTP call inside `with session.begin():`, a Celery
task holding a session across a retry sleep, or Django's `ATOMIC_REQUESTS = True`
wrapping an entire request — including that three-second third-party API call —
in one transaction, and you have built the incident.

**Go** makes it rarer by making it explicit: `Commit()` and `Rollback()` are
statements you write and can `defer`, and `database/sql` will not open a
transaction behind your back. It reappears the moment a `context` has no
deadline, because then "explicit" only means "explicit about the happy path."

**Node** has the pool-shaped version: `pool.connect()` without
`finally { client.release() }`, or a `BEGIN` issued on a client that an exception
path never returns.

In all three the real fix is the same, and it is **a server-side guardrail, not
discipline**: `idle_in_transaction_session_timeout` plus a per-role
`statement_timeout`. Any fix that depends on every future engineer remembering
something is not a fix.

## The experiment

| File | What it does |
|---|---|
| `python/mvcc_lab.py` | Shared table (`mvcc_orders`) and sampling helpers. Not a program. |
| `python/xmin_horizon.py` | Which idle sessions actually stall vacuum and which do not — three holders, side by side — then the guardrail that kills the bad one, with vacuum's own notices as the evidence. |
| `python/bloat_and_latency.py` | The timed incident: continuous churn under a held horizon, sampling dead tuples, table and index size, `Heap Fetches` and read latency into a CSV; then recovery with `VACUUM` and `VACUUM FULL`. |
| `python/replication_slot_starvation.py` | The same starvation caused by a replication slot with no consumer — the version that leaves `pg_stat_activity` looking perfectly healthy. |

`mvcc_orders` is Topic 2's own table so that wrecking it does not disturb the
`orders` table Topics 3 and 4 read. **Its `status` column is indexed on purpose:**
an `UPDATE` to an indexed column can never be a HOT update, and HOT updates —
cleaned up on the same page, no index maintenance — are exactly what makes this
experiment do nothing at all.

The five phases, which the programs above implement between them:

1. **Baseline.** Record p50/p99 for a point lookup and a range scan,
   `pg_total_relation_size('mvcc_orders')`, `n_dead_tup`/`n_live_tup` from
   `pg_stat_user_tables`, and `Heap Fetches` from `EXPLAIN (ANALYZE)` of a query
   that should be an index-only scan.
2. **Starve it.** Hold the horizon in a second session and keep the churn
   running, sampling the same metrics on a fixed interval into a CSV.
3. **Watch the horizon.** `SELECT pid, state, backend_xid, backend_xmin,
   xact_start FROM pg_stat_activity ORDER BY backend_xmin` and
   `SELECT slot_name, xmin, catalog_xmin FROM pg_replication_slots`. Confirm
   *which* session holds it — not just that something does.
4. **The second cause.** Kill the holder; create a slot with no consumer and show
   identical starvation from a cause that nothing in `pg_stat_activity` reveals.
5. **Recover.** `VACUUM (VERBOSE, ANALYZE)`, then `VACUUM FULL`. Record how much
   space comes back at each step — and how much does not.

### The finding that contradicts the folklore

"An idle-in-transaction session blocks vacuum" is true for only two of the three
cases `xmin_horizon.py` runs:

| Holder | `backend_xid` | `backend_xmin` | Stalls vacuum? |
|---|---|---|---|
| idle in txn, READ COMMITTED, read-only | — | — | no |
| idle in txn, READ COMMITTED, wrote first | set | — | yes |
| idle in txn, REPEATABLE READ, read-only | — | set | yes |

A `READ COMMITTED` transaction takes a fresh snapshot per statement and releases
it when the statement ends, so a read-only one sitting idle holds nothing at all.
It starts holding the horizon the moment it writes (it now owns a transaction id)
or the moment it runs at `REPEATABLE READ` or `SERIALIZABLE` (one snapshot for
the whole transaction — the direct consequence of Topic 1). All three look
identical in `pg_stat_activity.state`.

**Check `backend_xid` and `backend_xmin`, not `state`.** That is the operational
takeaway, and it is why the real incident is nearly always a handler that made a
slow call *after* its first `UPDATE`.

## How to run

Assumes [`lab/README.md`](../lab/README.md). From the `03-data` directory:

```
python3 02-mvcc-and-vacuum/python/xmin_horizon.py
DURATION_S=120 SAMPLE_S=20 python3 02-mvcc-and-vacuum/python/bloat_and_latency.py
python3 02-mvcc-and-vacuum/python/replication_slot_starvation.py
```

`bloat_and_latency.py` defaults to a two-minute run so it is runnable in a
sitting; the shape of the curve is the point of the longer one, so do at least
one pass at `DURATION_S=900 SAMPLE_S=30`. Other knobs: `ROWS`, `UPDATES`,
`CHURN_BATCH`, `PROBES_PER_SAMPLE`, `RANGE_SPAN`, `IDLE_TIMEOUT_S`, and `LAB_OUT`
for where the CSV lands.

Output shape — your numbers, not anybody's:

```
  phase                  time  dead tuples  table MB  index MB  heap fetches  point p99  range p50  range p99
  baseline                 0s   <yours>      <yours>   <yours>   <yours>       <yours>    <yours>    <yours>
  starved                 20s   ...
  after VACUUM            ...
  after VACUUM FULL       ...
```

Three columns to read closely. **`heap_fetches`** is the sneaky one — the plan
does not change, the visibility map does; `-1` there means the sample was not an
index-only scan at all, which under enough bloat is itself a finding.
**`table_mb` after plain `VACUUM`** does not move: plain vacuum returns pages to
the free space map, not to the OS. Only `VACUUM FULL` (an `ACCESS EXCLUSIVE`
lock, i.e. an outage) or `pg_repack` shrinks the file. **`heap_fetches` after
`VACUUM FULL`** goes back *up* from zero, because `VACUUM FULL` rewrites the
table and leaves the visibility map unset — index-only scans stay degraded until
something plain-vacuums the table again, and `ANALYZE` does not fix it.

### Blocked on this machine, honestly

`replication_slot_starvation.py`'s headline case needs `wal_level = logical`. A
default local install runs `replica`, so the program prints the unblock command
(`ALTER SYSTEM SET wal_level = 'logical';` plus a restart) and **skips that phase
rather than inventing it**. The physical-slot phase does run, and teaches the
distinction worth knowing: an abandoned **physical** slot pins WAL and fills your
disk, while an abandoned **logical** slot pins xmin and stalls vacuum. One view,
two failure modes, and `xmin` is the column that tells them apart.

Two more holders cannot be shown on a default local server: prepared two-phase
transactions (`max_prepared_transactions = 0` unless you raise it) and
`hot_standby_feedback` from a standby running long queries — for that one, bring
up the replica from [Topic 8](../08-replication-lag/README.md) first, or use the
Docker path in [`lab/README.md`](../lab/README.md).

## Predict, then record

Before running: how long until p99 doubles? Does table size or index size grow
faster? Does `Heap Fetches` go nonzero before or after p99 moves? Does plain
`VACUUM` restore the original file size — yes or no? The holder table above
gives one prediction away, so make this one instead: once
`idle_in_transaction_session_timeout` kills the holder, does dead-tuple count
start falling immediately, or only at the next autovacuum cycle — and how would
the CSV tell those two apart?

| Elapsed | n_dead_tup | table size | index size | Heap Fetches | p50 | p99 |
|---|---|---|---|---|---|---|
| baseline |  |  |  |  |  |  |
| 5 min |  |  |  |  |  |  |
| 15 min |  |  |  |  |  |  |
| after VACUUM |  |  |  |  |  |  |
| after VACUUM FULL |  |  |  |  |  |  |

**Broken experiment, not wrong prediction, if:**

- **`n_dead_tup` stays near zero.** Your `UPDATE`s are HOT updates, cleaned on
  the same page. Update an **indexed** column to disable HOT — this is what
  `mvcc_orders.status` is for.
- **Nothing moves in fifteen minutes.** The write rate is too low relative to
  table size. You need dead tuples in the millions before scans notice.
- **p99 rises with flat `n_dead_tup`.** You are measuring autovacuum competing
  for I/O — a different and also real effect. Check
  `pg_stat_progress_vacuum` before concluding anything.
- **The table shrinks after plain `VACUUM`.** Either you ran `VACUUM FULL`, or
  the dead space happened to sit at the end of the file — the one case plain
  vacuum can truncate.
- **The horizon never moves back after you kill the holder.** Something else is
  holding it. That is not a broken experiment so much as a second finding: run
  the `pg_stat_activity` and `pg_replication_slots` queries again and identify
  it.

## Answer before moving on

1. Readers do not block writers under MVCC. So why does a *read-only* long query
   make write-side latency rise?
2. `hot_standby_feedback = on` prevents query cancellation on the replica but can
   starve vacuum on the primary. State the tradeoff in one sentence, then pick a
   side for an analytics replica and defend it.
3. Why does an index-only scan need the visibility map at all, and what exactly
   goes wrong with it during starvation?
4. Anti-wraparound vacuum used to be a cliff. Explain how PG18's eager freezing
   changes the *shape* of that risk rather than removing it.

## Further reading

- [PG18 docs §25.1, Routine Vacuuming](https://www.postgresql.org/docs/18/routine-vacuuming.html) — the xmin horizon and freezing, from the source
- [Microsoft: PostgreSQL 18 vacuuming improvements explained](https://techcommunity.microsoft.com/blog/adforpostgresql/postgresql-18-vacuuming-improvements-explained/4459484)
- [Crunchy Data: managing transaction ID wraparound](https://www.crunchydata.com/blog/managing-transaction-id-wraparound-in-postgresql)

## Next up

[Topic 3 — Indexes](../03-indexes/README.md). Bloat made scans read more pages;
the next topic is about which pages a scan reads at all, and why every index you
add is a tax on every write forever.
