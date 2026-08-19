# Layer 3 · Topic 1 — Isolation levels, and precisely which anomaly each permits

### The takeaway (read this first)

**The one idea:** an isolation level is a contract about which *interleavings*
the database promises you will never see, and every level below `SERIALIZABLE`
permits at least one interleaving in which two individually-correct transactions
produce a state neither of them would have allowed alone.

**Why it matters in practice:** double refunds, double-booked shifts, negative
balances that "can't happen because we check first." Rare in staging (low
concurrency), constant in production — exactly the profile of a bug that survives
review, because the code that causes it reads correctly line by line.

**You'll know it landed when:** you can draw the write-skew interleaving from
memory and say "`REPEATABLE READ` does not prevent it, `SERIALIZABLE` does, and
the cost is a retry loop plus false aborts under sequential scans" — and mean
every clause, including the last one.

This topic is half of the roadmap's ownership test for the whole layer:
*construct a write skew example on a whiteboard, say which isolation level
prevents it and what that costs.*

## The concept

**Anomalies are defined by what a transaction can observe.**

- *Dirty read* — you see data another transaction has not committed.
- *Non-repeatable read* — you read the same row twice and get two values.
- *Phantom* — you run the same range query twice and get a different row *set*.
- *Lost update* — two read-modify-write cycles interleave and one write vanishes.
- *Write skew* — two transactions read an overlapping set, each decides its own
  write is safe based on what it read, and they write **different rows**. Nothing
  conflicts at row level, and the invariant spanning both rows breaks anyway.

Write skew is the one worth drawing, because it is the one that survives the
defences people reach for. Two doctors are on call for a shift; the rule is at
least one must remain. Each doctor's request runs `SELECT count(*) WHERE shift_id
= $1 AND on_call` and sees **2**, concludes "there is another doctor, I may go
off call," and updates *its own* row. Both statements are correct. Both
transactions read a consistent state. The shift ends with nobody on call.

**Postgres is stricter than the standard in specific places.** This is the table
to memorise, and it is not the textbook one
([PG18 §13.2](https://www.postgresql.org/docs/18/transaction-iso.html)):

| Level | Dirty read | Non-repeatable | Phantom | Serialization anomaly (write skew) |
|---|---|---|---|---|
| `READ UNCOMMITTED` (standard) | allowed | possible | possible | possible |
| `READ UNCOMMITTED` (**Postgres**) | **not possible** (behaves as RC) | possible | possible | possible |
| `READ COMMITTED` (PG default) | not possible | possible | possible | possible |
| `REPEATABLE READ` (standard) | not possible | not possible | **allowed** | possible |
| `REPEATABLE READ` (**Postgres**) | not possible | not possible | **not possible** | possible |
| `SERIALIZABLE` | not possible | not possible | not possible | not possible |

Postgres' `REPEATABLE READ` is snapshot isolation — strictly stronger than the
standard's, it prevents phantoms too. It still permits write skew, and the reason
is worth being able to state cold: **write skew is not a read anomaly.** Every
read in a write-skew execution is perfectly consistent with one snapshot, so
snapshot isolation has nothing to object to.

Two mechanisms let you derive the rest instead of memorising it:

- At `READ COMMITTED`, **each statement takes a fresh snapshot**. If an `UPDATE`
  hits a row a concurrent transaction just committed, Postgres does not abort —
  it waits, **re-evaluates the `WHERE` clause against the new row version**, and
  proceeds if it still matches. That is exactly why
  `UPDATE accounts SET balance_cents = balance_cents - 100 WHERE id = 1` does
  *not* lose updates, while `SELECT balance_cents` → compute in Python →
  `UPDATE ... SET balance_cents = 900` absolutely does. **The anomaly lives in
  your round trip, not in the database.**
- At `REPEATABLE READ`, the same collision instead raises `could not serialize
  access due to concurrent update` (SQLSTATE `40001`). The bug does not
  disappear; it becomes visible. `SERIALIZABLE` adds SSI on top: predicate locks
  (`SIReadLock` in `pg_locks`, which do **not** block and cannot deadlock) track
  read/write dependencies, and a transaction aborts when no serial order could
  have produced the outcome.

**The cost of `SERIALIZABLE`, stated precisely** — not "it's slow":

1. You must write a retry loop. `40001` cannot be engineered away; it is the
   mechanism, not a failure of it.
2. A **sequential scan takes a relation-level predicate lock**. A seq scan on a
   hot table therefore raises the false-abort rate sharply — which is what makes
   Topics 3 and 4 load-bearing for this one. An index that changes no
   correctness property changes your abort rate.
3. Predicate locks escalate tuple → page → relation as
   `max_pred_locks_per_transaction` runs out, silently raising aborts as
   concurrency grows.
4. Read-only transactions must be retried too, and their results are not valid
   until they commit.

## How each language actually gets there

**Two languages, not six.** The anomaly is entirely server-side, so a third and
fourth client would print the same table twice. Go is here for one claim that is
about the *client* rather than the server, and that claim is testable.

**Python** — the anchor, and the production stack. SQLAlchemy sets isolation per
engine or per connection with
`engine.execution_options(isolation_level="SERIALIZABLE")`; psycopg3 exposes it
on the connection. Django exposes it only as connection-level `OPTIONS`, making
per-transaction isolation deliberately awkward. Retry belongs in a decorator, not
in views: catch `OperationalError` and check the SQLSTATE (`e.sqlstate` on
psycopg3, `e.orig.sqlstate` through SQLAlchemy) for `40001`. **The trap Python
makes easy:** retrying only the failed `UPDATE` is the natural shape when the
exception surfaces at the statement, and it reintroduces the anomaly. A retry
must re-run the *whole* transaction, reads included, because the reads are what
the new snapshot invalidates.

**Go** — the ergonomics claim. `sql.TxOptions{Isolation: sql.LevelSerializable}`
sets the level; `pgconn.PgError.Code` carries the SQLSTATE as an ordinary value
you compare, not an exception you may or may not catch at the right layer.
Because errors are values, the natural way to write the retry is a function that
takes the whole transaction as a closure — `inTx(ctx, func(tx) error { ... })` in
`golang/write_skew/main.go` — which makes the *correct* shape the easy one and
the anomaly-reintroducing shape the awkward one. That is the point: a language
ergonomics fact became an architecture fact, and it is why Go services run
`SERIALIZABLE` more often than Python ones do.

Worth knowing but not built here: **Node's** `pg` gives you no isolation API at
all, so you issue `SET TRANSACTION ISOLATION LEVEL` yourself as the first
statement of the transaction; and Prisma's interactive-transaction timeout will
abort long transactions under contention before Postgres does, which looks like a
database error and is a client default.

## The experiment

Four programs and a pair of SQL scripts, in the order to run them.

| File | What it does |
|---|---|
| `python/lockstep_psql.py` | The whiteboard version. Drives two real `psql` sessions one statement at a time through the write-skew interleaving, at all three isolation levels, printing both sessions' output as it happens. **Start here** — this is the drawing the ownership test asks for. |
| `sql/write_skew_session_{a,b}.sql` | The same interleaving to run by hand in two terminals. Numbered steps; alternate between the files. |
| `python/write_skew.py` | Write skew under real concurrency: 100 shifts, both doctors released simultaneously, at read committed / repeatable read / serializable / read committed + `FOR UPDATE`. Reproduces the anomaly and then fixes it two different ways in one run. |
| `python/lost_update.py` | Read-modify-write vs. a single-statement `UPDATE`: 500 concurrent withdrawals from one account, at read committed and repeatable read, plus the `FOR UPDATE` variant. |
| `python/serializable_cost.py` | The cost of `SERIALIZABLE`, measured: the same workload with and without the index the read needs, sampling `pg_locks` for `SIReadLock` granularity while transactions are live. |
| `golang/write_skew/main.go` | The same write-skew workload in Go with pgx and pgxpool, for the retry-ergonomics comparison. |

Reproduce the anomaly **first in two lock-step sessions** — the version you can
draw — then under concurrency — the version proving it happens without you
arranging it.

**What each program is evidence of:**

`write_skew.py` prints one row per variant. `broken` is
`GROUP BY shift_id HAVING sum(on_call::int) = 0` — shifts with nobody on call:

```
variant                         broken   40001   retries/req   p50 ms   p99 ms   req/s
read committed                  <yours>  <yours>  <yours>      <yours>  <yours>  <yours>
repeatable read                 <yours>  ...
serializable                    <yours>  ...
read committed + FOR UPDATE     <yours>  ...
```

The two rows to compare are *repeatable read* and *serializable*. The fourth row
is the manual fix, and it is the one that teaches the mechanism: lock the rows
you **read**, not the row you write. Each variant also prints the
`transaction_isolation` its transactions actually ran at, because "the flag was
never applied" is the most common way this experiment lies to you.

`lost_update.py` is the sharpest sentence in this topic made runnable: *the
anomaly lives in your round trip, not in the database.* Same isolation level,
same concurrency — the read-modify-write variant loses updates and the
single-statement variant cannot. At `REPEATABLE READ` those same losses become
`40001` aborts.

`serializable_cost.py` is the one worth running twice. Identical `SERIALIZABLE`
workload, once with an index on the column the read filters on and once without.
With the index you see `tuple` and `page` SIReadLocks; without it, `relation` —
a sequential scan read the whole table, so its predicate lock covers the whole
table and every concurrent writer now conflicts with it. Watch the abort rate and
p99 move on a schema change that has nothing to do with correctness.

**Retries have no backoff, on purpose.** Under sustained contention every retry
re-collides immediately, and the `gave up` column is where that ends. That is
question 4 below, and it is the same failure the reliability layer calls a retry
storm.

## How to run

Assumes [`lab/README.md`](../lab/README.md). Run from the `03-data` directory:

```
python3 lab/local/check_env.py
python3 01-isolation-levels/python/lockstep_psql.py
python3 01-isolation-levels/python/write_skew.py
python3 01-isolation-levels/python/lost_update.py
python3 01-isolation-levels/python/serializable_cost.py
cd 01-isolation-levels/golang/write_skew && go run .
```

Tuning without editing code, all optional: `SHIFTS`, `PAIRS_IN_FLIGHT`,
`WITHDRAWALS`, `WORKERS`, `TOTAL_SHIFTS`, `HOT_SHIFTS`; `LAB_DSN` for the Python
programs, `LAB_PG_URL` for the Go one.

The two `sql/` scripts need no lab tooling — open two terminals on the lab
database and alternate between the numbered steps.

## Predict, then record

Write these down **before running anything**. For each isolation level: does the
write-skew scenario break the invariant? Does the read-modify-write variant lose
updates at `READ COMMITTED`? Does the atomic variant? At which level does the
atomic variant start *failing* instead of losing updates? And: does dropping the
index change the abort rate by a little or by a lot?

| Scenario | Isolation | Broken / lost | 40001 aborts | Retries/req | p99 | Req/s |
|---|---|---|---|---|---|---|
| write skew | read committed |  |  |  |  |  |
| write skew | repeatable read |  |  |  |  |  |
| write skew | serializable |  |  |  |  |  |
| write skew + FOR UPDATE | read committed |  |  |  |  |  |
| lost update (RMW) | read committed |  |  |  |  |  |
| lost update (RMW) | repeatable read |  |  |  |  |  |
| lost update (atomic) | read committed |  |  |  |  |  |
| write skew, no index | serializable |  |  |  |  |  |

**Broken experiment, not wrong prediction, if:**

- **Zero broken shifts at `READ COMMITTED`.** The pair is not overlapping. Raise
  `PAIRS_IN_FLIGHT`, and confirm nothing upstream is serialising requests —
  check `pg_stat_activity` mid-run for two live transactions on the same shift.
- **Any `40001` at `READ COMMITTED`.** Postgres never raises it there. Your flag
  is not applied; the `transaction_isolation` line each variant prints is how you
  check.
- **The single-statement `UPDATE` losing an update.** Impossible in one
  statement — you have two, or autocommit split them.
- **`SERIALIZABLE` with zero aborts *and* zero breakage *and* unchanged
  throughput.** There was no contention to serialise; confirm every request
  targets the same small set of shifts.
- **`SIReadLock` rows never appear in `pg_locks`.** You sampled after the
  transactions committed. Predicate locks are held only while the transactions
  are live and for a window afterwards, so the sample has to run *during* the
  workload.

## Answer before moving on

1. Why can't row-level locking on the rows you *write* prevent write skew, and
   what would you have to lock instead to prevent it manually?
2. `SELECT ... FOR UPDATE` on the rows you *read* does prevent this specific
   case. Name a write-skew case where it cannot, and say why. (Hint: what do you
   lock when the invariant is over rows that do not exist yet?)
3. Postgres' `REPEATABLE READ` prevents phantoms; the standard's does not. Given
   that, is there any anomaly for which upgrading RR → `SERIALIZABLE` in Postgres
   buys you nothing? Justify.
4. Your retry loop retries on `40001`. Under sustained contention every retry
   immediately re-collides. What does the fix share with
   retry-with-jitter-and-budget from the reliability layer, and what does it
   share with lock ordering from Topic 5?

## Further reading

- [PG18 docs §13.2, Transaction Isolation](https://www.postgresql.org/docs/18/transaction-iso.html) — the source of the anomaly table above, and short enough to read in one sitting
- [PG18 docs §13.3, Explicit Locking](https://www.postgresql.org/docs/18/explicit-locking.html) — `FOR UPDATE` semantics, for the manual fix
- Kleppmann, *Designing Data-Intensive Applications* — transactions are Chapter 7 in the 1st edition and **Chapter 8** in the 2nd; read it alongside this topic, then the Postgres docs above for what DDIA deliberately keeps vendor-neutral

## Next up

[Topic 2 — MVCC, and what vacuum falling behind does to latency](../02-mvcc-and-vacuum/README.md).
The snapshots that made these isolation levels possible are versions of rows that
somebody has to clean up, and the cleanup is the next incident.
