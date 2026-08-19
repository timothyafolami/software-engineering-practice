# Layer 5 · Topic 7 — Idempotency, and degradation decided in advance

### The takeaway (read this first)

**The one idea:** every technique in this layer — retries, hedging, timeouts,
at-least-once anything — is only *legal* if repeating an operation is safe,
so idempotency is not a payments feature, it is the precondition for the
whole layer.

**Why it matters in practice:** exactly-once delivery is impossible over a
network. What you can build is at-least-once delivery plus an idempotent
consumer, which gives effectively-once **processing** — a sentence that
answers a startling fraction of distributed systems interview questions and a
larger fraction of real bugs.

**You'll know it landed when:** you reach for an idempotency key without
deciding to, and you can explain why `SELECT`-then-`INSERT` is wrong at READ
COMMITTED without looking it up.

## The concept

An **idempotency key** is a client-generated unique value that lets the
server recognise a retry of something it may already have done. The mechanism
that makes it correct is a **unique constraint**, not a check.

The client sends `Idempotency-Key: <uuid>`. The server attempts:

```sql
INSERT INTO idempotency_keys (key, fingerprint, state)
VALUES (...)
ON CONFLICT DO NOTHING
RETURNING id;
```

- If the insert **won**, you are the executor: do the work, and store the
  response **in the same transaction as the side effect**.
- If it **lost**, someone else owns this key. `completed` means replay the
  stored response byte-for-byte; `in_progress` means return 409 and let the
  client retry — without waiting, and without redoing the work.

**Fingerprint the request body**, because the same key with a different body
is a client bug and must be a 422, not a silent replay of the wrong answer.
And **expire keys on a documented TTL**, remembering that the client's retry
window must be shorter than your retention or the guarantee evaporates
exactly when it is needed.

**Why the naive version is wrong**, and this is a Layer 3 callback: at READ
COMMITTED — Postgres' default — two concurrent transactions that both
`SELECT` first both see no row, and both proceed to insert. Nothing in the
isolation level serialises them; the unique index is the only thing that
does. Watching the naive version double-charge under concurrency is worth
more than this paragraph.

**When the side effect is not in your database** — charging a card, sending
mail, publishing to Kafka — you cannot make it atomic with the key insert,
and you need the **transactional outbox**: write the *intent* in the same
transaction as the key, and let a relay deliver it at-least-once to a
consumer that is itself idempotent. That is Layer 4's material, and the
handoff point is exactly here.

**On the wire,** `Idempotency-Key` is an IETF Internet-Draft
(`draft-ietf-httpapi-idempotency-key-header`, draft-07 as of October 2025,
with Standards Track intent) and **not yet an RFC**. It is a de facto
standard set by Stripe and adopted widely. Use the name; just do not call it
a standard in a design doc without that qualifier.

**Graceful degradation belongs in this topic** because it has the same shape:
a decision made in advance, so that the 3am version is a switch flip rather
than an argument. Write the matrix *now* — feature, tier, what "off" looks
like to a user, kill-switch mechanism, who may flip it, blast radius. Two
rules make it real: the shed order follows **business** importance rather
than code structure, and any row whose kill switch requires a deploy is not a
kill switch.

## How each language actually gets there

**Three languages here, not six: Python, Go and Node.js.** The mechanism
lives in Postgres — a unique index and `ON CONFLICT` under READ COMMITTED —
not in the runtime, so six near-identical database clients would teach
nothing. The three below are the languages actually deployed at work, and
each has a *distinct* driver-level failure mode around the same SQL, which is
the only per-language content this topic genuinely has.

**Python:** `postgresql.insert(...).on_conflict_do_nothing()` in SQLAlchemy,
wrapped in middleware that owns the whole protocol — key extraction,
fingerprinting, execution, response storage and replay — so no endpoint has
to remember any of it. The Python-specific trap is that an `IntegrityError`
leaves the session in a failed state, so you **must** roll back before you can
read the existing row. Code that forgets this fails only under concurrency,
which is to say only in production, and the traceback you get
(`PendingRollbackError`) names a different problem than the one you have.

**Go:** the same SQL, with `sql.ErrNoRows` from the `RETURNING` clause as the
lost-the-race signal. `pgx` surfaces the constraint name in
`*pgconn.PgError.ConstraintName`, so you can tell *which* unique constraint
fired — which matters the moment you have two (one on the key, one on a
natural business key), because the correct handling differs: one is a retry,
the other is a genuine conflict. Go's other advantage here is that
`context` cancellation on the losing path actually returns the connection.

**Node.js:** the same SQL again, but with a driver-level hazard worth knowing:
some clients and poolers will transparently re-execute a statement after a
connection-level error, so a single logical `INSERT` can reach Postgres twice
from one call in your code. That is precisely why the **constraint**, and not
application logic, must be the arbiter — the arbiter has to live somewhere
that a transparent replay cannot bypass. Node's async ergonomics also make
the "forgot to await the rollback" bug easy, with the same shape as Python's
poisoned session.

## The experiment

A `/charge` endpoint on FastAPI + Postgres, an `idempotency_keys` table, a
`charges` table, and a deliberately hostile k6 test.

1. **Naive** (`SELECT` then `INSERT`, no unique index): 50 concurrent
   requests sharing one key, issued from *different* VUs. Count rows in
   `charges`.
2. **Correct** (unique index + `ON CONFLICT`): identical test. Assert exactly
   one charge row and 50 byte-identical responses.
3. **With topic 3's retry layer active** and toxiproxy failing the *response*
   path, so the client never learns it succeeded: assert still exactly one
   charge. This is the realistic case — the ambiguous result, where the
   client cannot distinguish "did not happen" from "happened, answer lost".
4. **Crash test:** `docker compose kill app` mid-request, restart, let the
   client retry. Assert no orphaned `in_progress` keys blocking forever and
   no double charge. This is where you find out whether your TTL was actually
   thought through or just typed.
5. **Fingerprint test:** same key, different body → 422, original charge
   untouched.

Output shape:

```
mode=<name>  charge_rows=<n>  distinct_responses=<n>  409s=<n>  orphaned_in_progress=<n>
```

## How to run

**The harness is built and was executed here.** `lab/docker-compose.yml`,
`lab/app/`, `lab/scripts/*.js` and
`lab/tools/*.py` exist (specified in
[`../lab/README.md`](../lab/README.md)) and the commands below were run
against them. You do **not** need to install `k6`: it runs from the
`grafana/k6` image, which is what `docker compose run --rm k6` starts. What
you do need is Docker running (`docker info`) and host ports 8000-8003 free —
if something else on your machine holds 8000, `up` fails with `port is
already allocated`. From `05-failure/lab/`:

```
cd ../lab
docker compose --profile payments up -d --build
docker compose run --rm k6 run /scripts/07_idempotency.js -e MODE=naive
docker compose exec postgres psql -U app -d failure_lab \
  -c "SELECT count(*) FROM charges;"
docker compose run --rm k6 run /scripts/07_idempotency.js -e MODE=correct
docker compose run --rm k6 run /scripts/07_idempotency.js -e MODE=chaos
```

Each run truncates both tables in `setup()`, so the row count after it is
that run's own. Step 4, the crash test, is the one you drive by hand — and it
is the step that finds out whether the TTL was thought through:

```
docker compose run --rm k6 run /scripts/07_idempotency.js -e MODE=correct &
sleep 2 && docker compose kill app && docker compose up -d app
curl -s localhost:8000/admin/report | python3 -m json.tool
```

`orphaned_in_progress` in that report is the number that matters: a claim
whose holder died and whose TTL has not expired blocks every retry of that
key until it does.

**All three standalone versions are written and were run here.** Each runs the
same race against a local Postgres with no containers involved, takes no
arguments, and finishes in about ten seconds:

```
python3 -m pip install -r python/requirements.txt   # SQLAlchemy + psycopg
python3 python/idempotency.py

cd golang && go run idempotency.go && cd ..         # pgx v5, go.mod beside it

cd nodejs && npm install && node idempotency.js     # node-postgres
```

They need a local Postgres accepting connections (`pg_isready`) and create
their tables in the `failure_lab` database; drop it with `dropdb failure_lab`
when you are done. Each program drops and recreates its own tables on startup,
so every count it prints is that run's own.

All three run the same eight rows — naive sequential, naive concurrent, naive
concurrent with `pool=1`, the correct claim-then-execute version, a correct
single-transaction variant, the ambiguous result, the crash test either side of
the TTL, and the fingerprint check — plus the degradation matrix and a kill
switch being flipped mid-run. Two rows are worth reading against each other
before anything else: `naive / 50 concurrent` versus the `pool=1` row directly
beneath it, where a *smaller* connection limit hides the bug completely; and
`correct / claim + execute` versus `correct / single transaction`, which are
both correct and differ only in whether a duplicate gets a 409 or waits — the
`loser_p99` column prices the waiting.

Each language then adds its own driver-level section: Python reproduces the
poisoned `Session` (and prints which exception this SQLAlchemy and driver
actually raise, which is not the one the folklore names), Go fires two
different unique constraints and tells them apart by
`*pgconn.PgError.ConstraintName`, and Node builds a `withRetry` wrapper that
transparently re-executes a committed statement and shows the unique index
being the only thing that saves it.

## Predict, then record

Before running: how many charge rows will the naive version create from 50
concurrent identical requests? Does that answer change if `pool_size=1`, and
why? After the crash test, what state are the key rows in, and what unblocks
them?

| Mode | charge rows (expect 1) | distinct responses (expect 1) | 409s | orphaned keys |
|---|---|---|---|---|
| naive, sequential | | | | |
| naive, 50 concurrent | | | | |
| correct, 50 concurrent | | | | |
| correct + retries + injected faults | | | | |
| correct + mid-request crash | | | | |

**What would mean the experiment is broken, not the prediction wrong:**

- **The naive version produces exactly 1 charge.** Your requests are not
  actually concurrent. Check three things: the key is shared across VUs
  rather than generated per-VU; k6 is using an arrival-rate executor; and
  `pool_size > 1`, because a pool of one serialises everything and hides the
  race perfectly — a genuinely beautiful case of a *smaller* resource limit
  concealing a bug.
- **The correct version produces more than 1.** Either the unique index is
  not on the column you think, or the key insert and the side effect are in
  different transactions. Check with `\d idempotency_keys`.
- **Every request returns 409.** `in_progress` never advances to
  `completed` — most likely an exception path that never commits.
- **The crash test passes trivially.** Confirm that the kill actually landed
  mid-transaction. Add a deliberate delay between the key insert and the
  charge insert to widen the window, then kill inside it.

## Answer before moving on

1. Why must the key row and the side effect be in the *same* transaction?
   Construct the specific failure that occurs when they are not.
2. A client retries with the same key after your TTL has expired. What
   happens, and what should the relationship be between TTL and the client's
   retry window?
3. "Exactly-once delivery is impossible; at-least-once plus an idempotent
   consumer gives effectively-once processing." Explain the first clause
   precisely — *why* is exactly-once delivery impossible?
4. Which endpoints in the service you actually own are non-idempotent today?
   For each one: cost to fix, and blast radius if a retry fires before you do.

## Sources

- `draft-ietf-httpapi-idempotency-key-header` (IETF Internet-Draft, draft-07
  as of October 2025) — the header's name and semantics; not an RFC
- Stripe's idempotency documentation — the de facto behaviour everyone copies
- Postgres documentation on transaction isolation, for why READ COMMITTED
  permits the `SELECT`-then-`INSERT` race

## Next up

That is the layer. Back to the [layer index](../README.md) for the
"you own this when" test, and then **Layer 6 — Observability and operating**,
which is the natural sequel: every experiment here depended on measuring
something default dashboards do not show.
