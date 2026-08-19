# Layer 3 · Topic 5 — Locking, lock ordering, deadlocks, and the migration that took the site down

### The takeaway (read this first)

**The one idea:** locks are **queued**, and a lock request that has to wait
**blocks everything behind it in that queue** — including requests that would
have been perfectly compatible with the lock currently held.

**Why it matters in practice:** this is how a "safe" `ALTER TABLE ADD COLUMN`
that takes milliseconds causes a full outage. The `ALTER` needs `ACCESS
EXCLUSIVE`; a long-running `SELECT` holds `ACCESS SHARE`; the `ALTER` waits — and
every *subsequent* `SELECT`, which would have run happily alongside the first
one, now queues behind the `ALTER`. Traffic stops within seconds. For a live
latency problem, `pg_stat_activity` filtered on `wait_event_type = 'Lock'` is the
first place to look.

**You'll know it landed when:** you can explain why the fix is `lock_timeout`
rather than "run migrations at night," and you order `SELECT ... FOR UPDATE` rows
deterministically without being asked to.

## The concept

**Row locks** come from `UPDATE`, `DELETE`, `SELECT ... FOR UPDATE` and
`FOR SHARE`. **Table locks** come mostly from DDL, and the four modes worth
knowing by name are:

| Mode | Taken by | Conflicts with |
|---|---|---|
| `ACCESS SHARE` | plain `SELECT` | only `ACCESS EXCLUSIVE` |
| `ROW EXCLUSIVE` | `INSERT`/`UPDATE`/`DELETE` | share-mode locks and above |
| `SHARE UPDATE EXCLUSIVE` | `VACUUM`, `ANALYZE`, `CREATE INDEX CONCURRENTLY` | itself, and stronger modes |
| `ACCESS EXCLUSIVE` | most `ALTER TABLE`, `VACUUM FULL`, `REINDEX` | **everything**, `SELECT` included |

The queue is the part people miss. Postgres grants locks roughly in request
order, so a pending strong request parks in front of later weak ones. That is why
the compatibility matrix alone does not predict the outage: `SELECT` and `SELECT`
are compatible, but not when an `ALTER` is waiting between them.

**Deadlock** is a cycle in the waits-for graph. Postgres detects it after
`deadlock_timeout` (default **1 second**) and aborts one transaction with
SQLSTATE `40P01`. Read that default correctly: it is a *detection delay*, not a
limit — the victim waits the full second before anything happens, so a deadlock
costs a second of latency even when your retry works perfectly. The fix is almost
never "retry harder"; it is **lock ordering** — every transaction acquires rows in
the same deterministic order, which makes a cycle impossible rather than rare. In
SQL that is `SELECT ... WHERE id = ANY($1) ORDER BY id FOR UPDATE`, and the
`ORDER BY` is the entire fix.

`SELECT ... FOR UPDATE SKIP LOCKED` turns a table into a work queue: each worker
claims rows nobody else has locked, without blocking. It is how you build a job
queue without a broker — with the honest caveat that heavy queue churn produces
index bloat ([Topic 2](../02-mvcc-and-vacuum/README.md) again, from a different
direction). Postgres' bottom-up index deletion improved this substantially but
did not eliminate it, so watch index size on queue tables.

**The guardrails that actually work are per-session settings, not process
discipline:**

- **`lock_timeout`** — fail fast instead of queueing. `SET LOCAL lock_timeout =
  '3s'` **before every DDL statement**, then retry the migration. This converts
  an outage into a failed migration, which is the whole trade.
- **`statement_timeout`** per role — 30s for the web role, longer for analytics.
- **`idle_in_transaction_session_timeout`** — also your Topic 2 fix, and the same
  reason: a transaction nobody is driving is holding something somebody wants.
- **`log_lock_waits = on`** so waits appear in the log with the blocking PID,
  after `deadlock_timeout` has elapsed.

## How each language actually gets there

**Python only.** Lock modes, the queue, and deadlock detection are all server
behaviour; a Go or Node client would issue the same `SELECT ... FOR UPDATE` and
see the same `40P01`. What differs per language is only which library helps or
hinders, and none of it needs a program.

**Python** — SQLAlchemy's `.order_by(Account.id).with_for_update()` is the fix,
and its trap is that across a `JOIN` it locks rows in *both* tables unless you
pass `of=`. Django's `select_for_update()` raises outside a transaction (a good
default), and `select_for_update(skip_locked=True)` gives you the queue. Put
`SET LOCAL lock_timeout` **inside** the migration transaction — `LOCAL` scopes it
to that transaction, which is exactly what you want;
`django-pg-zero-downtime-migrations` does this for you and is worth reading even
if you do not adopt it.

Worth knowing: **Go** and **Node** need the same `40P01` retry shape, and Node has
one detail this layer cares about — a one-second deadlock stall does not block
other requests, because they are on other pool connections, but it *does* hold a
pool slot for that second. That is the mechanism by which a locking problem
becomes a pool problem, which is [Topic 7](../07-connection-pools/README.md).

## The experiment

**1. Deadlock under load.** `transfer(from_id, to_id, amount)` in two versions:
**A** locks the two `accounts` rows in argument order, **B** locks them
`ORDER BY id`. Fire transfers between random pairs from the seeded 10,000
accounts, with enough concurrency that pairs collide. Count `40P01` from the
client *and* from the Postgres log for each version. Then read the server's
deadlock report closely — it names both PIDs and both statements, and it is one
of the most useful messages Postgres emits.

**2. The migration incident, end to end.** With a steady read workload running:
open a session that holds a long lock on `orders`, then run
`ALTER TABLE orders ADD COLUMN notes text;` from a third session. Watch
throughput go to zero. Inspect the queue live — this query is the one to keep:

```sql
SELECT a.pid, a.state, a.wait_event_type, a.wait_event, l.mode, l.granted,
       left(a.query, 60)
FROM pg_locks l JOIN pg_stat_activity a USING (pid)
WHERE l.relation = 'orders'::regclass
ORDER BY a.query_start;
```

The `granted` column is the whole story: one ungranted `ACCESS EXCLUSIVE` row,
and a growing pile of ungranted `ACCESS SHARE` rows behind it. Then repeat with
`SET lock_timeout = '3s';` before the `ALTER`, and show the migration failing
harmlessly instead of taking the site down.

**3. Queue semantics.** Three workers against `jobs` using
`FOR UPDATE SKIP LOCKED`; verify no job is processed twice. Remove `SKIP LOCKED`
and show the three workers serialise into one. Run it hard for ten minutes and
record index size growth on the `jobs` table.

## How to run

Assumes [`lab/README.md`](../lab/README.md). From the `03-data` directory:

```
python3 05-locking-and-deadlocks/python/deadlock_ordering.py
python3 05-locking-and-deadlocks/python/migration_lock_queue.py
python3 05-locking-and-deadlocks/python/skip_locked_queue.py
```

`sql/lock_queue.sql` carries the inspection query as a standalone script, plus
the two that matter beside it — everything waiting on a lock server-wide, and
`pg_blocking_pids()` to name who is blocking whom. Run it in a second terminal
while `migration_lock_queue.py` is mid-flight, with `\watch 1`, rather than
reading the program's own output: watching the queue build is the part that
makes the mechanism stick.

Each program runs in ten to twenty seconds at its defaults and cleans up after
itself. Knobs: `ACCOUNTS` / `TRANSFERS` / `WORKERS` for experiment 1 (shrink
`ACCOUNTS` to raise the collision rate); `READERS` / `HOLD_S` / `LOCK_TIMEOUT`
for experiment 2 (`HOLD_S` is how long the blocking transaction holds its lock,
and it is also how long your outage lasts); `JOBS` / `WORKERS` / `BATCH` /
`WORK_MS` / `CHURN_S` for experiment 3. `WORK_MS` is the important one there —
it is how long a worker holds the row lock while doing the job, and setting it
to zero makes the whole effect disappear.

## Predict, then record

Before running: how many deadlocks per minute in version A? In version B —
precisely zero, or merely fewer, and why? How many seconds after the `ALTER`
starts does throughput hit zero? Does `lock_timeout` protect you fully, or is
there still a window where requests queue?

| Scenario | 40P01/min | throughput | p99 | time to stall |
|---|---|---|---|---|
| transfer, argument order |  |  |  | — |
| transfer, ORDER BY id |  |  |  | — |
| ALTER, no lock_timeout |  |  |  |  |
| ALTER, lock_timeout = 3s |  |  |  |  |

| Queue variant | jobs processed | duplicates | index size start → end |
|---|---|---|---|
| FOR UPDATE SKIP LOCKED |  |  |  |
| FOR UPDATE (no SKIP LOCKED) |  |  |  |

**Broken experiment, not wrong prediction, if:**

- **Zero deadlocks in version A.** The transfers are not overlapping. Shrink the
  account pool, or check that your pool is large enough to have two transactions
  in flight at once — a pool of one cannot deadlock with itself.
- **Ordering by id still deadlocks.** There is a second lock source you did not
  choose: a foreign-key check, a trigger, or a unique-index insertion, all taking
  locks in an order you did not write. The deadlock report names it — this is a
  finding, not a failure.
- **The `ALTER` completes immediately.** Your blocking session does not actually
  hold a lock on `orders`. `SELECT pg_sleep(30)` alone touches no table; it has
  to have read from `orders` inside an open transaction.
- **Throughput does not stall.** Confirm the reads actually hit `orders` and not
  a table nobody is altering.
- **`SKIP LOCKED` shows duplicate processing.** That is not a race in Postgres —
  it is your worker committing the claim in a separate transaction from the work,
  which is question 4 below arriving early.

## Answer before moving on

1. Why does an `ALTER TABLE` that is merely *waiting* block new `SELECT`s, when
   the lock it wants has not been granted?
2. `lock_timeout` vs `statement_timeout` vs `idle_in_transaction_session_timeout`
   — one sentence each on what it saves you from, and why you need all three
   rather than the strictest one.
3. Deterministic ordering prevents deadlocks between two transactions doing the
   *same* operation. Name a realistic pair of *different* operations where it
   fails anyway.
4. `SKIP LOCKED` gives you at-least-once processing. What exactly makes it not
   exactly-once, and what would you add to make the work idempotent?

## Further reading

- [PG18 docs §13.3, Explicit Locking](https://www.postgresql.org/docs/18/explicit-locking.html) — the full conflict matrix and the deadlock section
- [PG18 docs §14.7, Locking and Indexes](https://www.postgresql.org/docs/18/locking-indexes.html)
- The `ALTER TABLE` reference's lock notes — which forms take `ACCESS EXCLUSIVE` and which have been weakened in recent majors, which is the difference between a safe migration and an outage

## Next up

[Topic 6 — Finding N+1 systematically](../06-finding-n-plus-1/README.md). Locks
are about statements fighting each other; the next topic is about the far more
common case of one request issuing ten thousand statements that fight nobody and
are still your p99.
