# Layer 4 · Topic 2 — Idempotency keys, atomically

### The takeaway (read this first)

**The one idea:** exactly-once *delivery* is impossible, so you build
at-least-once delivery plus an idempotent consumer, which gives effectively-once
*processing* — and the idempotency must be enforced by a unique constraint in
the same transaction as the effect, never by a check-then-act.

**Why it matters in practice:** this is the direct fix for Topic 1. Once the
handler is idempotent, a client retry on an ambiguous timeout is *safe*, and
latency stops being a correctness problem and goes back to being a performance
problem. It is also the roadmap's flagship project ("a payments API done
properly") and the thing interviews at this level actually probe.

**You'll know it landed when:** you can explain why `SELECT ... FOR UPDATE` is
useless for a row that does not exist yet, why
`INSERT ... ON CONFLICT DO NOTHING RETURNING id` returns zero rows on exactly
the path you care about, and what the second concurrent request with the same
key experiences while the first transaction is still open.

## The concept

Start from the failure. Two requests carrying the same idempotency key arrive at
the same millisecond on two workers. The intuitive handler is:

```
row = SELECT * FROM idempotency_keys WHERE key = $1;
if row is None:
    INSERT INTO idempotency_keys ...;
    charge_the_card();
```

Both `SELECT`s return nothing, because under `READ COMMITTED` neither sees the
other's uncommitted insert. Both proceed. Two charges. The window is
microseconds wide, which is why this passes every test you write by hand and
fails in production the first time a caller retries fast.

The fix is to make "did I win?" a fact the database decides, in the same
statement that records the claim:

```sql
INSERT INTO idempotency_keys (tenant_id, key, fingerprint, state)
VALUES ($1, $2, $3, 'in_flight')
ON CONFLICT (tenant_id, key) DO NOTHING;
```

**The key record has three states, not two:** `in_flight`, `succeeded`,
`failed_permanently`. Two states is the classic bug — with only "exists" and
"does not exist" you cannot distinguish "someone is working on this right now"
from "nobody has started." It carries a **request fingerprint** (a hash of
method + path + normalised body), so the same key with a different body is a
`422`, not a replay of the wrong thing. It stores the **response** for
byte-identical replay. It has a **TTL** (~24h) and is **scoped per tenant**, so
one customer cannot collide with another's key space.

Two details trip up nearly everyone:

1. **`ON CONFLICT DO NOTHING RETURNING id` returns zero rows on conflict.** You
   do not learn the existing row from this statement. "Did I win?" is
   `rowcount == 1`; the loser must `SELECT` separately, as a second statement.
2. **Under `READ COMMITTED` the loser's `INSERT` blocks** on the unique index
   until the winner commits or rolls back. It does not fail fast — it *waits*.
   So a duplicate's latency is bounded below by the winner's **entire**
   transaction. This design converts duplicates into latency spikes, which given
   a live latency problem you should measure rather than assume. The
   alternatives are all explicit choices: keep the transaction short, set a
   `statement_timeout` on that path, or return `409 — in flight, retry` the
   instant you detect a conflict.

The structural rule, and the one worth memorising: **the key row and the effect
commit in the same transaction.** Insert the key, commit, then charge in a
second transaction, and a crash between them poisons the key forever — every
retry from then on sees `succeeded` for work that never happened, and the
customer never gets charged and never gets told.

On Postgres 18, make the key table's surrogate primary key `uuidv7()` rather
than `gen_random_uuid()`. Random v4 keys scatter B-tree inserts across the whole
index; time-ordered v7 keys append. This table takes an insert on every single
request, so the difference is not academic. Note that `uuidv7()` is an 18
function and the Postgres this machine has locally is 17.5, so the fallback run
below uses `gen_random_uuid()` and its insert-throughput numbers are not
comparable with a container run — see [`../lab/`](../lab/README.md).

## How each language actually gets there

**Three languages, not six, and the reason is the rule from the repo README:**
the mechanism here lives *outside* the language — it is a unique index and a
transaction boundary in Postgres. What differs per runtime is only how the
driver spells SQLSTATE `23505` and which framework-shaped trap sits next to it.
Rust, C++ and Java would each add one more spelling of the same error code
(`SqlState::UNIQUE_VIOLATION`, `PQresultErrorField(res, PG_DIAG_SQLSTATE)`,
`SQLException.getSQLState()`) and nothing else; that is not worth three more
implementations.

**Python — the driver is easy and the framework is the trap.** psycopg3 exposes
`cur.rowcount` directly; SQLAlchemy 2.x uses
`pg_insert(...).on_conflict_do_nothing(index_elements=[...])` and
`result.rowcount`. The FastAPI-specific hazard is worth the whole topic: an
`IntegrityError` escaping inside an implicitly-begun session leaves that session
**aborted**, so every later statement in the same request fails with
`InFailedSqlTransaction` and surfaces as a confusing 500 a long way from the
cause. Wrap conflict-capable statements in `session.begin_nested()` — a
SAVEPOINT — so a caught unique violation rolls back to the savepoint instead of
poisoning the transaction.

**Go — explicit enough that you cannot swallow it by accident.** With `pgx`:
`errors.As(err, &pgErr)` then `pgErr.Code == "23505"`. There is no exception
hierarchy to over-catch, so the unique-violation branch has to be written on
purpose. That explicitness is a genuine advantage here and it is the same
property that made Go the readable one in Topic 1.

**Node.js — the same code, and the place the wrong instinct reads best.**
`node-postgres` surfaces `err.code === '23505'` identically. Node earns its
place by hosting implementation **A**: "I will just check first" reads most
natural in JavaScript, sits in the most codebases, and is most wrong. Writing
the consumer side here also sets up Topic 6, where the same idempotency lives at
the other end of the pipeline.

## The experiment

Three implementations of the same endpoint behind an env flag (`IMPL`, see
[`../lab/`](../lab/README.md)):

- **A — check-then-insert.** `SELECT`, and if absent `INSERT` and charge.
  Included precisely so you can watch it fail, and so the failure has a number.
- **B — atomic insert.** `ON CONFLICT DO NOTHING`, fingerprint check,
  stored-response replay, the three-state machine, effect in the same
  transaction.
- **C — advisory lock.** `pg_advisory_xact_lock(hashtext(key))` then
  check-and-act. Also correct, different cost profile, and a different failure
  mode behind a connection pooler — which Topic 7 returns to.

k6 fires `K` unique keys, each 3–5 times **simultaneously from different VUs**.
That distinction is the experiment: firing duplicates sequentially from one VU
tests nothing, because the first one has already committed. Add a chaos layer
that duplicates and reorders requests, and keep Topic 1's toxics running so that
real retries happen for real reasons rather than because the script decided to.

The assertion is SQL, not a log line:

```sql
SELECT idempotency_key, count(*) FROM charges
GROUP BY 1 HAVING count(*) > 1;   -- must return zero rows
```

Record p50/p99 **for duplicate requests separately** from first attempts. That
second number is the price of the design, and it belongs in the table next to
the correctness result rather than in a footnote.

## How to run

Compose (blocked while the Docker daemon is down —
`python3 ../lab/local/check_env.py`):

```
IMPL=A docker compose up -d --force-recreate payments-api
docker compose run --rm k6 run /scripts/topic2_duplicates.js
psql -d sep_lab_04_dist -f sql/topic2_assert.sql     # repeat for IMPL=B, IMPL=C
```

Local fallback, which reproduces the race without k6 or containers by driving
concurrent connections from the language runtimes directly against whatever
Postgres is listening. Each language runs all three implementations; `--impl` is
the only thing that changes.

```
python3 python/idempotency_race.py --impl A --keys 200 --concurrency 5
python3 python/idempotency_race.py --impl B --keys 200 --concurrency 5
python3 python/idempotency_race.py --impl C --keys 200 --concurrency 5

cd nodejs && npm install && cd ..          # once; node-postgres
node nodejs/idempotency_race.js --impl A --keys 200 --concurrency 5

cd golang && go run idempotency_race.go -impl A -keys 200 -concurrency 5 && cd ..

psql -d sep_lab_04_dist -f sql/topic2_assert.sql
```

Two flags worth knowing, present in all three languages:

- `--hold-ms N` (default 10) delays inside the *winner's* transaction, applied
  identically in A, B and C. It changes none of them; it widens a window that is
  otherwise microseconds wide, which is the only way to watch the race rather
  than infer it. The README's "B's duplicate p99 equals the winner p99" note is
  about exactly this knob.
- `--vary-slot N` makes slot N send a **different body under the same key**. That
  is question 3 of *Answer before moving on*, made runnable: B answers `422`
  because it stored a fingerprint, while A and C cannot tell and answer `200`
  with the wrong charge. Note the count moves between runs — when the odd body
  happens to *win* the race it is the other four requests that get the 422.

```
python3 python/idempotency_race.py --impl B --keys 200 --concurrency 5 --vary-slot 4
python3 python/idempotency_race.py --impl A --keys 200 --concurrency 5 --vary-slot 4
```

Teardown when you are finished with the whole layer:
`python3 ../lab/local/teardown_lab.py`.

The fallback loses the network faults and the load profile. It keeps the only
thing the correctness result depends on: several connections inside the same
key's window at the same time, released together by a barrier.

## Predict, then record

**Predict first, in writing:** how many duplicate charges does A produce at
5-way concurrency over 200 keys? Does B produce any? What does B's p99 look like
for *losing* requests versus winners, and why? Which of B and C has the worse
tail, and by roughly what factor?

| Impl | Duplicate charges | 409s | p99 winner (ms) | p99 duplicate (ms) | Notes |
|---|---|---|---|---|---|
| A check-then-insert | | | | | |
| B on-conflict | | | | | |
| C advisory lock | | | | | |

**What would mean the experiment is broken, not the prediction wrong:**

- **A produces zero duplicates.** Almost certainly one of three things. (i) The
  duplicates are not actually concurrent — fire them from distinct VUs at the
  same instant, e.g. `http.batch`. (ii) Your DB pool is size 1, so the requests
  serialize inside your own application before they ever reach Postgres. (iii)
  There is a `UNIQUE` constraint on `charges.idempotency_key` and **the database
  is saving you** — correct in production, fatal to the demonstration. Drop it
  for the A run, then put it back; in production that constraint is your last
  line of defence and should outlive this experiment.
- **B's duplicate p99 equals the winner p99.** The duplicates probably arrived
  after the winner had already committed. Add a deliberate delay inside the
  winner's transaction to widen the window. That is not cheating — it is how you
  observe a race that is normally microseconds wide.
- **More charges than requests, anywhere.** Your script or a retry wrapper is
  generating extra traffic. Reconcile `http_reqs` against the row count before
  concluding anything at all.
- **C never blocks.** Check you used `pg_advisory_xact_lock` and not
  `pg_try_advisory_lock`, which returns false instead of waiting — a different
  design with a different table row.

## Answer before moving on

1. Why is `SELECT ... FOR UPDATE` on the key row insufficient to serialize two
   concurrent *first* attempts? Say exactly what it would lock.
2. The winner's transaction takes 8 seconds. Describe the loser's experience
   under `READ COMMITTED`, then say what you *want* it to be and precisely what
   you give up to get that.
3. Same key, different body. What status do you return, and why is `200` with
   the stored response actively dangerous rather than merely sloppy?
4. Client- or server-generated keys? Name the single property the key must have,
   and say what breaks if a client derives its keys from a timestamp.

## Next up

[Topic 3 — Clocks lie](../03-clocks-lie/README.md): why you cannot use a
timestamp for that, or for anything else that orders events across machines.
