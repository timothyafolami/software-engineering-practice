# Layer 3 · Topic 6 — Finding N+1 systematically, not by noticing

### The takeaway (read this first)

**The one idea:** N+1 is not detectable by reading code — the loop and the query
live in different files, often in different layers. It is detectable by
**counting queries per request**, which is mechanical, automatable and
enforceable in CI.

**Why it matters in practice:** the query count is `N + 1`, so it scales with
the result set and nothing else. Ten rows in development is eleven queries and
looks instant; ten thousand rows in production is ten thousand and one queries,
and even at a few milliseconds each that is tens of seconds. It is the single
most likely cause of a p99 that is fifty times your p50. If your latency
problem is worse on some endpoints and worse for your biggest customers, look
here first — which is why `SEQUENCE.md` puts this topic first in the running
order despite it being sixth in the numbering.

**You'll know it landed when:** you have a CI test that fails when a request
exceeds a declared query budget, and you own "queries per request" as a number
the way you own latency.

## The concept

**Lazy loading is the mechanism.** Touching `order.customer.name` inside a loop
issues one query per iteration. The count scales with the result set, so it never
trips a threshold in development, and it never appears in the diff — the loop is
in a serializer, the query is in an ORM relationship declared in a model file
somebody wrote two years ago.

**Four detection methods, in increasing order of production usefulness:**

1. **Per-request query counter.** Middleware counts statements and asserts a
   budget. Cheapest, catches most, works in CI. Django ships
   `assertNumQueries(n)`; SQLAlchemy needs about ten lines on
   `before_cursor_execute`. **Make it a test gate, not a dashboard** — a
   regression should fail the build, because a dashboard nobody is paid to watch
   is a dashboard nobody watches.
2. **`pg_stat_statements` sorted by `calls`, not by time.** This is the
   production-side trick most people miss. An N+1 is a query with an enormous
   `calls` count and a *tiny* `mean_exec_time`, so sorting by `total_exec_time`
   finds it only when the total happens to be large enough to reach the top —
   sorting by `calls` always does.
3. **OpenTelemetry span counts per parent span.** One parent span with hundreds
   of child DB spans sharing a single query template is unmistakable, and this is
   the only method that also catches **cross-service N+1**, where an HTTP fan-out
   has the identical shape and no ORM tool can see it.
4. **SQLCommenter** to get from the slow query back to the route that emitted it
   — the same habit as [Topic 4](../04-reading-a-query-plan/README.md), used for
   attribution rather than for plans.

**The fixes, with the trap stated.** In SQLAlchemy 2.0, `selectinload()` issues
one extra query with `WHERE id IN (...)`, while `joinedload()` issues one `JOIN`.
For a **one-to-many** relationship `joinedload` multiplies rows — 100 orders × 20
line items is 2,000 rows to de-duplicate in Python — and is frequently *slower*
than the N+1 it replaced. Rule of thumb: **`joinedload` for many-to-one,
`selectinload` for one-to-many.** Django has the same distinction under different
names (`select_related` vs `prefetch_related`).

The deeper point: the fix is not "always eager load." That produces enormous
queries that are their own problem, and it moves the cost from the database to
your serialiser. The fix is **knowing the number and choosing.**

## How each language actually gets there

**Two languages.** The database cannot tell an N+1 from a fast endpoint — it sees
10,001 cheap correct queries and answers all of them — so this topic is genuinely
client-shaped, but only in two distinct ways. Go, Rust, C++ and Java would each
demonstrate "we have no ORM doing this to us," which is a sentence, not a
program.

**Python** — the ORM version, and the anchor. SQLAlchemy's lazy loading is a
*default*: accessing an unloaded relationship attribute on a persistent object
emits SQL, transparently, from whatever code happens to touch it. That
transparency is the feature and the bug. It also has an identity map, which means
repeated access to the *same* related row inside one session is free — so a naive
demonstration can accidentally show no problem at all, which the broken-experiment
list below covers.

**Node** — the batching version, and the reason it earns a folder. `pg` has no
ORM lazy loading to blame, and N+1 shows up anyway because **GraphQL resolvers
make it the default**: a resolver runs per field per object, so `orders { customer
{ name } }` over 100 orders calls the customer resolver 100 times by design.
DataLoader is the answer, and its mechanism is worth understanding precisely —
it collects every key requested **within one tick of the event loop**, then issues
a single batched query with `WHERE id IN (...)`. That is the same `IN (...)` idea
as `selectinload`, moved from the ORM to the application, and it works because
Node's event loop gives a natural, well-defined batching window. This is
[Layer 1's](../../01-machine/03-concurrency-models/README.md) single-threaded
event loop showing up as a *data-access* strategy, which is the kind of
connection this lab exists to make.

## The experiment

1. **Build the naive endpoint.** `GET /customers/{id}/orders?limit=N`,
   lazy-loading `order.customer` and `order.line_items`, instrumented with a
   per-request query counter that logs `queries=`.
2. **Scale it.** Run at `limit` = 10, 100, 1000 and plot queries/request and p99
   against `limit`. **Latency linear in result size is the fingerprint** — that
   shape, not the absolute number, is what you learn to recognise.
3. **Detect it three ways without reading the code.** The counter;
   `SELECT calls, mean_exec_time, query FROM pg_stat_statements ORDER BY calls
   DESC LIMIT 5`; and an OpenTelemetry trace counting child spans per request
   span. Confirm all three point at the same query.
4. **Fix it, then fix it wrong.** `selectinload`, re-measure. Then `joinedload`
   on the one-to-many — you should be able to make it *slower* than the N+1 it
   replaced, and explain why in terms of row multiplication plus Python-side
   de-duplication.
5. **Ship the gate.** A pytest asserting
   `queries_for(GET /customers/1/orders?limit=100) <= 3`, failing the build
   otherwise. This is the deliverable; everything above is how you learned to
   trust it.
6. **The Node half.** The same endpoint shape with a GraphQL-style resolver, then
   DataLoader, measuring queries per request for both. Then the interesting case:
   make the batching window wrong — issue the requests across ticks rather than
   within one — and show DataLoader batching nothing while looking correctly
   configured.

## How to run

Assumes [`lab/README.md`](../lab/README.md). From the `03-data` directory:

```
python3 06-finding-n-plus-1/python/query_counter.py
python3 06-finding-n-plus-1/python/lazy_vs_eager.py
npm install --prefix 06-finding-n-plus-1/nodejs    # once
node 06-finding-n-plus-1/nodejs/dataloader_batching.js
```

`python/orm_lab.py` holds the SQLAlchemy 2.0 models and the ten-line query
counter both Python programs use; it is imported, not run. The models map onto
the lab's existing tables and create nothing.

`query_counter.py` is experiments 1, 2, 3 and 5 — the count against `limit`, the
identity-map trap that makes a careless reproduction show nothing,
`pg_stat_statements` sorted by `calls`, and the CI gate that is the actual
deliverable. `lazy_vs_eager.py` is experiment 4: all four variants (lazy,
selectinload, joinedload, and a single hand-written join) at each `limit`, plus a
second table one level deeper (`customers → orders → line_items`, ~60x row
multiplication) where the joinedload-vs-selectinload trade actually bites. Knobs:
`LIMITS`, `NESTED_LIMITS`, `REPEATS`, `BUDGET`.

The Node program is experiment 6 and needs `pg` (`package.json` beside it
declares it). Its loader is hand-written rather than `npm install dataloader` —
thirty lines, so the batching window is visible rather than a library detail,
which is what makes the third row of its table make sense.

Experiment 3's `pg_stat_statements` path needs the extension loaded — see
`python3 lab/local/check_env.py`. The OpenTelemetry path is the one experiment in
this layer that is worth deferring to
[Layer 6](../../06-observability/README.md) if you have not built a collector
yet; the counter and `pg_stat_statements` are sufficient to complete the topic.

## Predict, then record

Before running: queries per request at each `limit`. p99 at `limit = 1000` before
and after the fix. Whether `joinedload` on `line_items` beats or loses to
`selectinload`, and by how much. And: how many rows cross the wire in each case —
work that out on paper first, it is the number that explains the result.

| Variant | limit | queries/req | rows over wire | p50 | p99 |
|---|---|---|---|---|---|
| lazy | 10 |  |  |  |  |
| lazy | 100 |  |  |  |  |
| lazy | 1000 |  |  |  |  |
| selectinload | 1000 |  |  |  |  |
| joinedload | 1000 |  |  |  |  |
| single join | 1000 |  |  |  |  |

| Node variant | queries/req | p99 |
|---|---|---|
| resolver per field |  |  |
| DataLoader, same tick |  |  |
| DataLoader, across ticks |  |  |

**Broken experiment, not wrong prediction, if:**

- **queries/req is flat as `limit` grows.** The relationship is already
  `lazy="selectin"`, or the session's identity map is serving repeats within the
  request. Real behaviour, wrong experiment — you need *distinct* related rows to
  see the effect.
- **`pg_stat_statements` shows one entry per iteration instead of one aggregated
  entry.** Parameters are being interpolated into the SQL string rather than
  bound. That is a detection problem and, far more urgently, a SQL injection
  risk — stop and fix it.
- **p99 does not improve after the fix.** The bottleneck moved to serialisation.
  Ten thousand rows through Pydantic is not free, and that is a real finding, not
  a failed one.
- **DataLoader shows no improvement.** Check that the keys are requested within
  one tick. An `await` between them ends the batching window, and the
  configuration will look perfect.

## Answer before moving on

1. Why does an N+1 pass code review and pass staging essentially every time?
   Name the specific property of the bug that defeats each.
2. Construct the case where sorting `pg_stat_statements` by `calls` finds an N+1
   that sorting by `total_exec_time` misses.
3. When is `joinedload` / `select_related` the wrong fix, and what row-count
   arithmetic tells you so *before* you measure?
4. Your service calls another service once per item in a list. Which of the four
   detection methods still works? What does that tell you about where to invest?

## Further reading

- [PG18 docs, `pg_stat_statements`](https://www.postgresql.org/docs/18/pgstatstatements.html) — the `calls` column is the one this topic is about
- SQLAlchemy 2.0's relationship-loading documentation — read the whole loader-strategy page once; the defaults are the bug
- [OpenTelemetry semantic conventions for database spans](https://opentelemetry.io/docs/specs/semconv/database/) — what a child DB span must carry for method 3 to work

## Next up

[Topic 7 — Connection pools, worker counts, and the container CPU limit](../07-connection-pools/README.md).
You have just made every request cheap; the next topic is what happens when many
cheap requests contend for a resource you sized by intuition.
