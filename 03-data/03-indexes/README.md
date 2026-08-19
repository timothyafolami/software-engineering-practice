# Layer 3 · Topic 3 — Indexes: B-tree internals, column order, and the cost of each one

### The takeaway (read this first)

**The one idea:** a B-tree is a *sorted* structure, so it can only help
predicates that are prefixes of that sort order — and every index is a tax on
every write to that table, forever.

**Why it matters in practice:** the two most common index mistakes are opposite
and equally expensive. The index that does nothing because the column has four
distinct values, and the six indexes on a hot write table that quietly halved
ingest throughput. Both look entirely reasonable in a pull request, and neither
shows up in code review.

**You'll know it landed when:** given DDL and a query, you can say which index
serves it, **how far into a composite index the scan gets before it must start
filtering**, and roughly what one more index costs on writes.

## The concept

For a composite index on `(a, b, c)` the sort is lexicographic: by `a`, ties
broken by `b`, then by `c`. Every rule people memorise about composite indexes
falls out of that one fact, so derive them rather than remembering them.

- `WHERE a = 1 AND b = 2` — a contiguous range of the index. Both are
  `Index Cond`. Great.
- `WHERE a = 1 AND c = 3` — finds the `a = 1` range, then has to look at every
  entry in it, because within `a = 1` the entries are sorted by `b` and `c` is
  scattered. The index "is used" and still reads far too much. **Do not expect
  this to show up as a `Filter`.** A B-tree evaluates a qual on any indexed
  column *inside the index*, so `EXPLAIN` prints it in `Index Cond` and
  `Rows Removed by Filter` stays 0 — on PG18 and on PG17 alike. `Filter` appears
  only for quals the index cannot evaluate at all. The cost of the extra reading
  lands in **`Buffers`**, and on PG18 in **`Index Searches`**; those are the
  numbers to compare, not the node labels.
- `WHERE a > 1 AND b = 2` — the range on `a` breaks the sort for `b`. Only `a` is
  a boundary condition. **The rule: equality columns first, then the one range
  column, then anything along for the ride.**
- `WHERE b = 2` alone — the scan has nowhere to start. Historically useless; see
  skip scan below.

Three more properties that decide real designs:

**Covering.** `CREATE INDEX ... ON orders (customer_id, created_at) INCLUDE
(total_cents)` puts the payload in the leaf without adding it to the sort,
enabling an **index-only scan** — provided the visibility map says the pages are
all-visible, which is precisely where [Topic 2](../02-mvcc-and-vacuum/README.md)
stops being true.

**Low cardinality is usually futile.** An index on `status` where ~92% of rows
are `'complete'` cannot help a query for `'complete'`: it would produce nearly
every row in random heap order, strictly worse than reading the table
sequentially. It *can* help for the rare value, which is why a **partial index**
(`WHERE status = 'failed'`) is so often the right answer — tiny, cheap, and
dramatically selective.

**Write cost, stated physically.** Every `INSERT` writes an entry into *every*
index on the table. Every `UPDATE` touching an **indexed** column writes a new
entry in *every* index, because the new row version lives at a new location that
every index must now point to. Updates touching only non-indexed columns can use
**HOT** and skip index maintenance entirely — which is exactly why "just add an
index on `updated_at`" is far more expensive than it looks: it converts your
cheapest updates into your most expensive ones.

**What changed, and it invalidates advice you have internalised.** Postgres 18
added **B-tree skip scan**: the planner can use a multicolumn index even when the
*leading* column has no equality restriction, by probing the index once per
distinct leading value. "The leading column must be in your `WHERE` clause or the
index is useless" is now a soft rule with a sharp condition: skip scan pays only
when the skipped leading column has **few distinct values**. The condition is
about the *skipped* column's cardinality, not about the shape of the predicate on
the columns that remain — a range on the trailing column skips just fine, which
is measurable in one plan and worth doing before you believe the sentence. At
high cardinality the planner declines to skip; it does not necessarily fall back
to a sequential scan, and often keeps using the index in a form that does not
probe. PG18's `EXPLAIN` reports `Index Searches: N` — the probe count — and it is
now one of the first numbers to read: **1 means it did not skip**, whatever the
node above it is called.

## How each language actually gets there

**Python only.** A B-tree is a property of the server; a Go or Node client
issuing the same `CREATE INDEX` and the same `SELECT` would print the same plan,
so a second language here buys a second copy of one table.

The one client-shaped thing in this topic is really a **locking** fact wearing an
index costume, and it belongs to your migration tool rather than your language.
Any migration adding an index to a table over roughly a million rows must use
`CREATE INDEX CONCURRENTLY`: Alembic's
`op.create_index(..., postgresql_concurrently=True)` inside a non-transactional
migration, Django's `AddIndexConcurrently` with `atomic = False`. A plain
`CREATE INDEX` holds a lock that blocks writes for its entire duration; on a
20-million-row `line_items` that is an outage, and it is
[Topic 5](../05-locking-and-deadlocks/README.md)'s problem arriving in Topic 3's
clothes.

## The experiment

`python/column_order.py` is the first and most important of these: two indexes
over the same three columns in different orders, four query shapes, one table of
results.

```
IDX_A = idx_orders_cust_status_created   -- (customer_id, status, created_at)
IDX_B = idx_orders_status_cust_created   -- (status, customer_id, created_at)
```

and the four shapes — `eq + eq`, `eq + range`, `range + eq`, `second column
only`. For each, record the chosen index, whether the second column was an
`Index Cond` or a `Filter`, `Rows Removed by Filter`, `Index Searches`, and
buffers. **"The index was used" is not the question.** The question is how far
into the index the scan got before it started throwing rows away, and
`Rows Removed by Filter` is that number.

Then three more experiments, in order:

2. **Skip scan (PG18).** Build `(status, created_at)` — `status` has 4 distinct
   values — and query on `created_at` alone; expect a skip scan. Then build
   `(customer_id, created_at)` — tens of thousands of distinct customers — and
   run the same query shape; expect the planner to refuse. Same shapes, opposite
   decision, and the only variable is cardinality. This is the crispest single
   demonstration in the layer, and `Index Searches: N` is where you see it.
3. **The index the planner is right to ignore.** Index `orders(status)`; query
   `WHERE status = 'complete'` (~92% of rows) and expect a sequential scan. Then
   the habit worth keeping for life: `SET enable_seqscan = off;` and compare
   *actual* times. **Forcing the index slower ⇒ the planner was right and your
   index is the problem. Forcing it faster ⇒ the planner was wrong and your
   statistics or cost settings are the problem** — not the index. Repeat with
   `status = 'failed'` (rare) and again with a partial index, and watch the
   decision flip.
4. **Write cost.** Insert at a fixed rate with 0, 1, 3 and 6 indexes present.
   Record sustained inserts/sec, WAL bytes from a `pg_stat_wal` delta, and p99
   insert latency. Then repeat with `UPDATE`s touching an indexed vs a
   non-indexed column, and check `n_tup_hot_upd` in `pg_stat_user_tables` to
   *prove* whether HOT applied rather than assuming it.

## How to run

Assumes [`lab/README.md`](../lab/README.md). From the `03-data` directory:

```
python3 03-indexes/python/column_order.py
psql -d sep_lab_03_data -f 03-indexes/sql/01_skip_scan.sql
psql -d sep_lab_03_data -f 03-indexes/sql/02_futile_index.sql
psql -q -d sep_lab_03_data -f 03-indexes/sql/03_write_cost.sql
```

The three `sql/` scripts are experiments 2, 3 and 4 in order. Each creates its
own indexes and drops them again, so the seeded tables come out as they went in;
`03_write_cost.sql` creates and drops its own `idx_write_cost` table rather than
churning one the other topics read. Its workload size is the two constants
`n_batches` and `rows_per_batch` at the top of its `DO` block — psql does not
interpolate variables into a dollar-quoted body, so they are constants on purpose
rather than `\set` values that would silently not apply.

Two settings decide whether any of this is meaningful, and both are session
scope, set by `lab_db.tune_session`: `random_page_cost = 1.1` (the 4.0 default
assumes spinning rust and distorts every decision in this topic) and
`effective_cache_size`. Verify with `SHOW random_page_cost` **in the session that
runs the query** before you trust a single plan here.

Skip scan needs Postgres 18. `python3 lab/local/check_env.py` reports your
server version and says so explicitly; on an older server experiment 2 does not
degrade gracefully, it simply tests something else.

## Predict, then record

Before running: for each query in (1), which index the planner picks and whether
the second column is an `Index Cond` or a `Filter`. For (2), whether a skip scan
appears in each of the two cases. For (3), whether `enable_seqscan = off` is
faster or slower, and by roughly what factor. For (4), the percentage throughput
drop from 0 to 6 indexes.

| Query | Index used | Cond / Filter | Rows Removed | Index Searches | Buffers | Time |
|---|---|---|---|---|---|---|
| eq + eq |  |  |  |  |  |  |
| eq + range |  |  |  |  |  |  |
| range + eq |  |  |  |  |  |  |
| second column only |  |  |  |  |  |  |

| Case | Skip scan? | Index Searches | Time |
|---|---|---|---|
| low-cardinality leading column |  |  |  |
| high-cardinality leading column |  |  |  |

| Indexes | inserts/sec | WAL bytes/insert | p99 insert | n_tup_hot_upd |
|---|---|---|---|---|
| 0 |  |  |  |  |
| 1 |  |  |  |  |
| 3 |  |  |  |  |
| 6 |  |  |  |  |

**Broken experiment, not wrong prediction, if:**

- **Everything sequential-scans.** You skipped `ANALYZE` after seeding, or the
  table fits entirely in `shared_buffers` and reading it is free.
- **Skip scan never appears.** Confirm `SHOW server_version` is 18 or later, and
  that the **skipped leading column really is low-cardinality** — that is the
  condition that decides it. Do not blame the predicate shape: a range on the
  trailing column still skips.
- **Index count makes no measurable difference to inserts.** You are bottlenecked
  on the client or the network, not on Postgres. Confirm the server is CPU- or
  WAL-bound during the run before concluding indexes are cheap.
- **`enable_seqscan = off` still sequential-scans.** It is a cost penalty, not a
  prohibition. It means every alternative was estimated *worse* — which is itself
  an answer, not a failure.
- **`n_tup_hot_upd` climbs during the indexed-column update run.** Your `UPDATE`
  is not touching the column you think it is, or the index you meant to add was
  never created.

## Answer before moving on

1. Why can an index scan on a low-cardinality column be slower than reading the
   whole table when it reads strictly *fewer* rows?
2. `(a, b)` vs `(b, a)`: name a workload that genuinely needs both, and one where
   keeping both is waste. What distinguishes them?
3. Skip scan handles low-cardinality leading columns. Is there any remaining
   reason to prefer `(status, created_at)` over `(created_at, status)` on PG18?
   Argue both sides.
4. You add an index to fix a slow read and write latency rises. Name the physical
   operations each write now performs that it did not before — and say which of
   them a HOT update would have avoided.

## Further reading

- [PG18 docs §11, Indexes](https://www.postgresql.org/docs/18/indexes.html) — multicolumn ordering and index-only scans
- [pgEdge: Postgres 18 skip scan](https://www.pgedge.com/blog/postgres-18-skip-scan-breaking-free-from-the-left-most-index-limitation)
- Petrov, *Database Internals*, Part I — why a B-tree is shaped the way it is, which is the level below what the Postgres docs treat as settled

## Next up

[Topic 4 — Reading a query plan fluently](../04-reading-a-query-plan/README.md).
You now know which index *should* serve a query; next is reading what the planner
actually did, and why it disagreed with you.
