# Layer 3 · Topic 4 — Reading a query plan fluently

### The takeaway (read this first)

**The one idea:** the most diagnostic number in a plan is not the time — it is
the ratio of **estimated to actual rows**, because every choice made downstream
of that node was made on the strength of that estimate.

**Why it matters in practice:** "this query got slow" is almost never "the query
changed." The *plan* changed, because the data crossed a threshold nobody was
watching. Fluency is how you get from "it's slow" to "the estimate on this join
is off by 400x, which is why it picked a nested loop" in ninety seconds instead
of an afternoon.

**You'll know it landed when:** you can name the fix before running anything —
the second half of the roadmap's ownership test for this layer.

## The concept

Read a plan **inside out** — the most indented node runs first — and then read
four things, in this order.

1. **`rows=` estimated vs `actual rows=`.** A large mismatch is the root cause
   more often than everything else combined. Nested loops get chosen when the
   outer side is *estimated* tiny; if it is actually large, you get an inner
   index scan executed a million times.
2. **`loops=`.** Reported node times are **per loop**.
   `actual time=0.01..0.02 rows=1 loops=2000000` cost forty seconds, not 0.02ms
   (2,000,000 × 0.02ms = 40s — do that multiplication every time). This is the
   single most common plan-reading mistake there is.
3. **Buffers.** In PG18, `EXPLAIN (ANALYZE)` includes `BUFFERS` **by default**,
   so you no longer have to remember to ask. `shared hit` is cache, `shared read`
   is disk. Two identical-looking plans with wildly different times usually
   differ here, and now the output says so.
4. **`Rows Removed by Filter`.** Work done and then thrown away — the signature
   of a predicate the index could not serve, which is
   [Topic 3](../03-indexes/README.md) seen from the other side.

**Scan types, and what each one means about your data.** A **Seq Scan** wins when
you need a large fraction of the table, because sequential I/O beats random —
which is why "the planner ignored my index" is usually the planner being right.
An **Index Scan** wins on high selectivity and gives you ordered output for free.
A **Bitmap Heap Scan** is the misread middle: build a bitmap of matching heap
pages from the index, sort it, then read the heap in **physical order**, turning
random I/O into sequential. It means "too many rows for an index scan, too few
for a seq scan," and it is frequently the correct choice. `Recheck Cond` with
`lossy=N` means `work_mem` was too small, so the bitmap degraded to page
granularity and every row on those pages must be rechecked.

**Join types.** **Nested Loop** — correct only when the outer side is genuinely
small; the `loops=` trap lives here. **Hash Join** — watch `Batches: > 1`, which
means it spilled to disk because `work_mem` was too small. **Merge Join** — both
sides sorted; cheap exactly when the sorts come free from indexes, expensive when
they do not. PG18 also reports per-node CPU and WAL statistics and the
`Index Searches` count from Topic 3.

## How each language actually gets there

**Four languages, not six** — and unusually for this layer, the extra three are
not decoration. The plan is chosen by the server, but **which plan the server
chooses depends on how your driver sent the statement**, and the four drivers
here make four different choices by default. Rust and Java are omitted only
because their drivers land on mechanisms already demonstrated by these four
(tokio-postgres prepares like pgx; PgJDBC's `prepareThreshold` is the same
five-execution switch, just on the client side).

**Python** — the anchor, and two habits rather than one mechanism.
`auto_explain` (`log_min_duration = '200ms'`, `log_nested_statements = on`) gets
you plans **in production**, where you cannot attach a debugger and cannot
reproduce the data. **SQLCommenter** appends structured comments
(`/*controller='orders',route='...'*/`) so a slow query found in
`pg_stat_statements` or in the log carries its own callsite — built into Django
as `SQLCOMMENT`, available for SQLAlchemy through a `before_cursor_execute` hook.
One caveat that bites: `pg_stat_statements` normalises parameters but **not**
comments, so putting dynamic values in the comment explodes your statement cache.
Separately, psycopg3 prepares a statement server-side automatically once it has
seen it `prepare_threshold` times (5 by default), which quietly puts Python in
the same category as Go below — most people using it do not know this.

**Go** — the generic-plan trap, and the reason it is a *Go* section. `pgx` uses
the extended protocol with named prepared statements by default. Postgres runs a
**custom plan** for a prepared statement's first executions, then compares the
cost of a generic plan against the average of those custom plans and may switch
to the generic one permanently ([PG18 `PREPARE`
docs](https://www.postgresql.org/docs/18/sql-prepare.html)). A generic plan is
built without knowing your parameter values, so on skewed data — `status =
'complete'` at ~92% vs `status = 'failed'` at a fraction of a percent — it can be
correct for one value and catastrophic for another. The plan you see in `psql`
(custom, real values) then differs from the one production runs.
`SET plan_cache_mode = force_generic_plan` reproduces it on demand, which turns a
nasty class of "it's fast when I test it" into a two-line check.

**Node** — the mirror image, and the reason it matters in
[Topic 7](../07-connection-pools/README.md). `pg` sends unnamed statements, so
the server plans each execution with the real parameter values and you mostly get
custom plans: no generic-plan surprise, and no server-side prepared statement to
be orphaned by a transaction-mode pooler — which is why `pg` works behind
PgBouncer with zero configuration while drivers that prepare need care. The cost
is that you pay planning time on every execution, which the same experiment
measures.

**C++** — the layer with nothing in between. libpq is what all of the above are
built on, and it is the only client here where you choose the protocol message
yourself: `PQexec` (simple query protocol — one round trip, no parameter
binding, no plan reuse), `PQexecParams` (extended protocol, unnamed statement —
what `pg` effectively does), and `PQprepare` + `PQexecPrepared` (a **named**
server-side object with a lifetime tied to the session — what pgx does). Writing
the three by hand is what makes "prepared statement" stop being a library concept
and become a thing the server holds on your behalf, in a session, until that
session ends. It also makes the pooler problem obvious before you meet it: a
named statement belongs to a *connection*, and transaction-mode pooling hands you
a different one.

## The experiment

**1. The plan-reading drill.** A `sql/plans/` directory of ten queries, run as
predict-then-check: write the predicted plan **on paper** first — scan type per
table, join type, rough row counts — then run `EXPLAIN (ANALYZE)` and score
yourself. At minimum, include:

- a primary-key point lookup;
- a `created_at` range returning ~0.1% of `orders`;
- the same range returning ~40% — this should flip to a seq scan, and **the
  bisecting script that finds the exact flip percentage produces the single most
  instructive number in this topic**;
- `orders ⋈ line_items` for one customer (expect a nested loop) and for a whole
  month (expect a hash join);
- `GROUP BY status`;
- a deliberately wrecked estimate from correlated predicates
  (`country = 'NG' AND status = 'failed'`), then repaired with
  `CREATE STATISTICS` on the pair;
- `WHERE lower(email) = ...` against a plain index on `email` — unusable, fixed
  with an expression index;
- a Python `str` bound to a `bigint` column, producing an implicit-cast index
  miss. Very common in the wild and nearly invisible in application code.

**2. The protocol comparison.** The same parameterised query, executed enough
times to cross the threshold, from each of the four clients — with
`pg_stat_statements` and `auto_explain` watching. Record which client produces a
generic plan, when, and what the plan is for a common parameter value vs a rare
one. Then flip `plan_cache_mode` and re-run.

**3. The production-shaped exercise.** With load running: use
`pg_stat_statements` ordered by `total_exec_time` to find the top three,
`auto_explain` output for their plans, and SQLCommenter tags to get back to the
route. **What is slow, why is it slow, who called it** — that triple is the
entire workflow, and it is the one you will actually use at 3am.

## How to run

Assumes [`lab/README.md`](../lab/README.md). From the `03-data` directory:

```
python3 04-reading-a-query-plan/python/plan_drill.py
python3 04-reading-a-query-plan/python/flip_threshold.py
python3 04-reading-a-query-plan/python/production_triage.py
(cd 04-reading-a-query-plan/golang/plan_cache && go run .)
npm install --prefix 04-reading-a-query-plan/nodejs   # once
node 04-reading-a-query-plan/nodejs/protocol_plans.js
g++ -O2 -std=c++17 -I"$(pg_config --includedir)" -L"$(pg_config --libdir)" \
    -Wl,-rpath,"$(pg_config --libdir)" \
    -o /tmp/plan_protocol 04-reading-a-query-plan/cpp/plan_protocol.cpp -lpq && /tmp/plan_protocol
```

`plan_drill.py` runs experiment 1 — the ten queries live in
[`sql/plans/`](sql/plans), one file each, and each file also runs unchanged in
psql (`psql -d sep_lab_03_data -f 04-reading-a-query-plan/sql/plans/03_range_wide.sql`).
**Read the file headers and write your predictions down before running the
program**; the headers tell you what to predict and deliberately not what the
answer is. `flip_threshold.py` bisects the exact selectivity where the plan
changes, for two columns of the same table that differ only in physical
correlation. The Go, Node and C++ programs are experiment 2 — three drivers,
three protocol defaults, one server. `production_triage.py` is experiment 3.

The C++ program needs libpq's headers, which `pg_config` locates for you on any
machine that has a Postgres install (including Homebrew's on macOS). The
`-Wl,-rpath` is not optional on macOS: without it the binary links and then
fails at startup with `Library not loaded: @rpath/libpq.5.dylib`, because
Homebrew's Postgres is keg-only. If `pg_config` is not on your `PATH`, the
Postgres client package is not installed — `python3 lab/local/check_env.py` says
so. The Node program needs `pg`; `package.json` beside it declares it and the
program prints the install command rather than a stack trace if it is missing.

`pg_stat_statements` must be in `shared_preload_libraries`, which requires a
server restart; `production_triage.py` prints the exact `ALTER SYSTEM` command
and falls back to a clearly-labelled client-side ranking without it.
**`auto_explain` does not need a restart** — `LOAD 'auto_explain'` works in any
session, which is the most useful thing to know about it, and that program
demonstrates it live.

## Predict, then record

| Query | Predicted plan | Actual plan | est vs actual rows | Buffers hit/read | Time | Right? |
|---|---|---|---|---|---|---|
| 1 point lookup |  |  |  |  |  |  |
| 2 range 0.1% |  |  |  |  |  |  |
| 3 range 40% |  |  |  |  |  |  |
| 4 join, one customer |  |  |  |  |  |  |
| 5 join, one month |  |  |  |  |  |  |
| 6 correlated predicates |  |  |  |  |  |  |
| 7 correlated + CREATE STATISTICS |  |  |  |  |  |  |
| 8 lower(email) |  |  |  |  |  |  |
| 9 str bound to bigint |  |  |  |  |  |  |
| 10 GROUP BY status |  |  |  |  |  |  |

Record separately the exact selectivity at which the range query flips
index scan → bitmap heap scan → seq scan:

| Transition | selectivity % | rows |
|---|---|---|
| index scan → bitmap heap scan |  |  |
| bitmap heap scan → seq scan |  |  |

| Client | prepares by default? | generic plan after N executions | plan for common value | plan for rare value |
|---|---|---|---|---|
| psycopg3 |  |  |  |  |
| pgx |  |  |  |  |
| node-postgres |  |  |  |  |
| libpq, PQexecParams |  |  |  |  |
| libpq, PQprepare |  |  |  |  |

**Broken experiment, not wrong prediction, if:**

- **The flip threshold is above ~50% or below ~0.1%.** Check
  `random_page_cost` — the 4.0 default badly distorts this on an SSD — and
  `effective_cache_size`, in the session that ran the query.
- **Times are dominated by each query's first run.** You are measuring cold cache
  rather than plan choice. Run each three times and use the warm one.
- **`EXPLAIN ANALYZE` total is far below what the application sees.** Planning
  time, round trips and result serialisation are not in that number. Compare
  against `pg_stat_statements.mean_exec_time` before concluding the query is
  innocent.
- **No `BUFFERS` lines in the output.** You are not on PG18. Add `BUFFERS`
  explicitly and note it, rather than concluding buffers do not matter.
- **Every client shows the same plan in experiment 2.** Either you did not
  execute enough times to cross the threshold, or a pooler in the path is
  resetting the session between executions — which is itself
  [Topic 7](../07-connection-pools/README.md)'s finding arriving early.

## Answer before moving on

1. A nested loop with `loops=1200000` on the inner side. Name three distinct root
   causes, and how you would tell them apart from the plan alone.
2. Why is a `Bitmap Heap Scan` often better than an `Index Scan` despite doing
   strictly more work?
3. The planner estimates 12 rows and gets 480,000. Give four causes and the fix
   for each.
4. Fast in `psql`, slow from the application, same parameters. Give three
   mechanisms that produce exactly that, and how you would confirm each.

## Further reading

- [PG18 docs §14.1, Using EXPLAIN](https://www.postgresql.org/docs/18/using-explain.html)
- [depesz: BUFFERS on by default in PG18](https://www.depesz.com/2025/01/15/waiting-for-postgresql-18-enable-buffers-with-explain-analyze-by-default/)
- [PG18 docs, `PREPARE`](https://www.postgresql.org/docs/18/sql-prepare.html) — the custom-vs-generic plan rule, from the source

## Next up

[Topic 5 — Locking, deadlocks, and the migration that took the site down](../05-locking-and-deadlocks/README.md).
Plans decide how much work a statement does; locks decide whether it gets to
start at all.
