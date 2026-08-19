# Layer 4 · Topic 6 — The outbox, sagas, and why 2PC is usually avoided

### The takeaway (read this first)

**The one idea:** you cannot atomically write to your database and publish to a
broker, so do not try — write the message into the same database in the same
transaction, and let a separate relay move it, accepting duplicates because
Topic 2 already made the consumer idempotent.

**Why it matters in practice:** the dual write is the most common correctness
bug in service architectures, and it is invisible in testing because it only
manifests when a process dies inside a sub-millisecond window. Under load that
window gets hit constantly, which is another way a latency problem becomes a
correctness problem.

**You'll know it landed when:** you can explain why reordering the two writes
does not help, why a relay tracking a high-water-mark `id` silently skips rows,
and what fills your disk when a logical replication consumer dies.

## The concept

**The dual write.** Two orderings, both broken:

```
INSERT charge; COMMIT; publish(event)   -- crash between: charge exists, no event
publish(event); INSERT charge; COMMIT   -- rollback: event emitted for work that never happened
```

No ordering fixes this, because the two systems have independent commit points
and there is no transaction spanning them. This is Topic 1's ambiguous result,
seen from the inside: your own process is the caller that does not know whether
the second write happened.

**Why not 2PC.** Two-phase commit genuinely solves this — that is worth saying
plainly, because "2PC is bad" is usually recited rather than understood. It is
avoided for three concrete reasons: the coordinator becomes a new single point
of failure that holds locks across *all* participants during the in-doubt
window; a coordinator crash leaves participants blocked, still holding those
locks, until someone intervenes; and most brokers and essentially all
third-party HTTP APIs are not XA participants at all, so it is not on offer for
the calls you most want it for.

The 2026 nuance, because this is being re-litigated: Kafka's **KIP-939** would
make Kafka a proper 2PC participant for exactly the dual-write recipe below. It
has **not shipped** — producer API work was incomplete and the public API
changes were reverted as of the December 2025 digest, and it is absent from
4.2 (Feb 2026) and 4.3 (May 2026). The advice stands, but hold it as an active
argument rather than a settled law.

**The outbox.** One transaction, two inserts:

```sql
BEGIN;
  INSERT INTO charges (...) VALUES (...);
  INSERT INTO outbox (aggregate_id, topic, payload) VALUES (...);
COMMIT;
```

A relay then does
`SELECT ... WHERE published_at IS NULL ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 100`,
publishes each row, and marks it published. `SKIP LOCKED` is the part that lets
you run more than one relay without them fighting over the same rows — each
grabs a disjoint batch instead of blocking on the other's locks.

**Bug 1, the high-water-mark trap, and it is the one to derive.** Sequences
allocate their values **outside** the transaction. So transaction X can take
`id = 99`, transaction Y can take `id = 100`, and Y can *commit first*. A relay
that remembers `last_seen_id` and queries `WHERE id > last_seen` will read
row 100, advance its mark to 100, and then row 99 becomes visible — behind the
mark, forever. It skips it permanently and silently. There is no error and no
gap in the topic; the message simply does not exist. The fix is not to make the
mark cleverer, it is to stop using one: a `published_at` / `status` column with
`SKIP LOCKED` has no ordering assumption to violate.

**Bug 2, unbounded growth.** An unpruned outbox becomes a bloat and autovacuum
problem — Layer 3's material arriving as an incident. Delete published rows on a
schedule, or partition by day and drop partitions.

**Ordering** is per-aggregate only, via a partition key. Global ordering is not
on offer and asking for it is usually a design smell wearing a requirement.

**Log tailing** is the alternative worth knowing. Logical decoding — Debezium,
or a managed CDC service — reads the WAL, so there is no polling load and commit
order is exactly right by construction, which makes Bug 1 impossible rather than
avoided. The cost is a **replication slot**, and the incident to know before you
choose it: if the consumer dies and the slot is not dropped, Postgres retains
WAL indefinitely and **fills the disk**, taking the primary down. No polling
load, one new way to lose the database. That trade is the whole decision.

**Sagas.** A business transaction expressed as a sequence of local transactions,
each with a compensating action. Three properties to internalise:

1. **There is no isolation between steps.** Other actors *will* observe
   intermediate states, and you must decide what that means. A `pending` status
   is usually the honest answer; pretending nobody looks is not.
2. **Compensations must be idempotent and retryable forever**, because
   "compensation failed" has no further fallback. There is no compensation for a
   compensation.
3. **Orchestration versus choreography.** Choreography (each service reacts to
   events) looks decoupled and becomes untraceable event soup; orchestration
   (one component drives the flow) gives you a single place to look when a flow
   hangs at 3am. Prefer orchestration until you have a specific reason not to.

**The 2026 update to weigh honestly:** durable execution engines — Temporal,
Restate, **DBOS** — move saga, retry and idempotency machinery into a runtime.
DBOS fits this stack closest: a library rather than a service, in-process,
Python and TypeScript SDKs, workflow state in the Postgres you already run.
Adopting one is a real reduction in code and bug surface *and* a real addition
to your critical-dependency list. Understand the pattern first; you cannot
operate what you cannot derive.

## How each language actually gets there

**Two languages, and one deliberate removal.** The mechanism here is Postgres
row locking plus a broker, so the language contributes the relay's loop shape
and the consumer's idempotency — nothing more.

**Removed: the second relay in Go.** An earlier draft had a Go relay alongside
the Python one, whose only deliverable was a p99 comparison between the two.
That comparison measures the driver, the poll interval and the container's CPU
share; it teaches nothing whatsoever about the outbox pattern, and a number that
looks like a finding but is not is exactly the defect this layer exists to
avoid. If you want the Go version, write it after the topic is done and treat it
as a Go exercise rather than a distributed-systems one.

**Python — the relay, and the reason it needs two mechanisms.** An asyncio loop
over psycopg3 with `FOR UPDATE SKIP LOCKED`. The refinement worth building is
`LISTEN`/`NOTIFY` to wake the relay instantly on insert, **plus** the polling
loop as a safety net — because `NOTIFY` is not durable and is simply lost if no
listener is connected at that moment. Belt and braces is correct here, and
knowing *why* both are needed is the entire point. A relay that only listens is
correct until its first reconnect; a relay that only polls has your poll
interval as its floor on latency.

**Node.js — the idempotent consumer**, keyed on the outbox row id. Same lesson
as Topic 2 (`err.code === '23505'`, insert-then-act, never check-then-act), at
the other end of the pipeline. It is here rather than in Python because the
consumer is where "I will just check whether I have seen this id" reads most
natural, and Node is where that instinct is strongest.

## The experiment

`POST /payments` must insert a charge and emit `payment.succeeded`. Five
variants behind `MODE` (see [`../lab/`](../lab/README.md)):

- **v0 — dual write.** Publish after commit; kill the broker mid-load
  (`docker compose stop redpanda`, or a Toxiproxy `timeout` on its port); count
  charges with no event. Then flip to publish-before-commit, force commit
  failures, and count events with no charge. Both directions, because the
  intuition that one of them must be safe is what you are dismantling.
- **v1 — outbox with a `SKIP LOCKED` relay** and an idempotent consumer. Repeat
  every fault: broker down, relay `docker kill`ed mid-batch, Postgres restarted.
  Assert that every charge eventually has at least one event, and that the
  consumer's *effect* count equals the charge count exactly.
- **v1-bug — the high-water-mark relay, deliberately.** To reproduce the skip
  you must force out-of-order commits: run two writers where one holds its
  transaction open (`pg_sleep(2)` before commit) so that it takes a *lower* id
  and commits *later*. Count permanently skipped rows.
- **v2 (stretch) — logical decoding relay.** Compare end-to-end p99
  charge→event latency and the load each approach puts on Postgres.
- **v3 (stretch) — the same flow as a DBOS workflow.** Count the lines of your
  own reliability code that disappear, and write down the new failure modes you
  took on in exchange.

## How to run

Compose (blocked while the Docker daemon is down —
`python3 ../lab/local/check_env.py`):

```
MODE=v0 docker compose up -d --force-recreate payments-api
docker compose run --rm k6 run /scripts/topic6_load.js &
sleep 20 && docker compose stop redpanda && sleep 20 && docker compose start redpanda
psql -d sep_lab_04_dist -f sql/topic6_reconcile.sql
```

Three parts run locally without Docker, against whatever Postgres is listening.
Start with **v1-bug**: it is the bug in this topic you are most likely to write
yourself and the cheapest one here to reproduce.

```
python3 python/hwm_skip.py --writers 3 --hold-seconds 2 --duration 60
psql -d sep_lab_04_dist -f sql/topic6_reconcile.sql
```

One program runs **both** relay designs over the **same** outbox rows, so the
comparison is one run rather than two. It refuses to report a result if no
id/commit inversion occurred — that run proved nothing, and the README lists it
as a broken experiment rather than a wrong prediction. `--hold-seconds 0` is the
control: with it, ids and commit order coincide and there is nothing to skip.

Then the relay proper, which needs the writer's own relay switched off
(`--relays 0`) or it will have nothing left to do:

```
python3 python/hwm_skip.py --writers 3 --hold-seconds 1 --duration 30 --relays 0 &
python3 python/outbox_relay.py --seconds 30
python3 python/outbox_relay.py --seconds 30 --no-listen            # the latency floor
python3 python/outbox_relay.py --seconds 30 --drop-notifications   # NOTIFY is not durable
```

Those last two flags are the whole argument for running `LISTEN`/`NOTIFY` *and*
a poll loop. `--no-listen` shows the poll interval as a floor under every event's
latency; `--drop-notifications` throws every wake-up away exactly as a `NOTIFY`
issued while nothing was listening would be, and the poll loop still delivers
everything. A listen-only relay would still be sitting on that backlog,
reporting itself healthy.

And the consumer side, which is Topic 2's lesson at the other end of the pipe:

```
cd nodejs && npm install && cd ..                                  # once
node nodejs/idempotent_consumer.js --mode check-then-act  --consumers 4
node nodejs/idempotent_consumer.js --mode insert-then-act --consumers 4
psql -d sep_lab_04_dist -f sql/topic6_reconcile.sql
```

Both modes see the same duplicate *deliveries* — that is what at-least-once
promised. Only one of them produces duplicate *effects*. Read query 6 of the SQL
for the distinction; it is the only count in this topic that may not duplicate.

Teardown for the whole layer: `python3 ../lab/local/teardown_lab.py`.

What none of this reproduces, and where the topic README says so: a broker that
can be stopped independently of the writer. `t6_delivered` is a table standing in
for one. **v0's dual write, the broker-down runs and the relay-killed run are
blocked** until the Docker daemon is up — they are not silently downgraded into
single-process imitations.

## Predict, then record

**Predict first, in writing:** in v0 with the broker down for 20 seconds at 100
rps, how many charges end with no event? Does v1 lose any — and if not, what
does it cost in charge→event p99? How many rows does the high-water-mark relay
skip in 60 seconds of overlapping transactions, and does that number depend on
load?

| Variant | Charges | Events delivered | Missing events | Duplicate effects | p99 charge→event (ms) |
|---|---|---|---|---|---|
| v0 dual write (broker down 20s) | | | | | |
| v0 publish-before-commit | | | | | |
| v1 outbox (broker down 20s) | | | | | |
| v1 outbox (relay killed) | | | | | |
| v1-bug high-water-mark | | | | | |
| v2 logical decoding | | | | | |

**What would mean the experiment is broken, not the prediction wrong:**

- **v0 loses no events with the broker down.** Almost certainly your broker
  client is buffering and retrying in memory — the *client* is saving you, not
  the design, and it will not when the process dies. Disable client-side
  buffering, or kill the API process instead so the buffer dies with it.
- **The high-water-mark relay never skips a row.** Your transactions are not
  overlapping in the required way: one must *start* first (taking the lower
  sequence value) and *commit* second. Force it with an explicit sleep before
  commit; otherwise ids and commit order coincide and there is nothing to skip.
- **v1 shows duplicate effects.** Duplicates are expected at the *delivery*
  layer and are a bug only at the *effect* layer. Confirm you are counting rows
  in the consumer's result table, not messages consumed. If the effects really
  are duplicated, your consumer is check-then-act — go back to Topic 2.
- **Charge→event p99 under a millisecond for a polling relay.** Your poll
  interval cannot be beaten by physics. You are probably measuring from the
  relay's read rather than from the charge's commit timestamp.
- **v2 shows no Postgres load reduction.** Check that the polling relay is
  actually stopped. Two relays running at once is the most common way this
  comparison comes out flat.

## Answer before moving on

1. Why does publishing *inside* the transaction (publish, then commit) fail to
   solve the dual write? Be specific about the crash window and what is in it.
2. Your relay has been down for six hours. What is the first thing you check
   about Postgres — and what is the different, worse answer if you had used
   logical decoding instead?
3. A saga's third step fails and the compensation for step one *also* fails.
   What is the system supposed to do, and what does that imply about how
   compensations must be written?
4. Name a concrete case where 2PC is genuinely the right answer, and say what
   makes it acceptable there and not in a payments flow.
5. You adopt DBOS and delete 400 lines of retry and idempotency code. Name the
   two new failure modes you just took on, and how you would detect each.

## Next up

[Topic 7 — Leader election, split brain, quorums, and fencing tokens](../07-leader-election-split-brain-quorums-fencing/README.md):
your relay must be a singleton. Making it one is where locks stop working the
way you think they do.
