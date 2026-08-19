# Layer 4 · Topic 4 — Consistency models, and the replica lag you already have

### The takeaway (read this first)

**The one idea:** a consistency model is a promise about what a *reader* is
allowed to observe, not a statement about how data is stored; the strong ones
cost a round trip on the read path, and almost every team silently downgrades to
"eventual" the day they add a read replica to fix a latency problem.

**Why it matters in practice:** "I saved it and it is gone" is the most common
user-visible distributed systems bug there is, and it comes from read-your-writes
being broken by a replica somebody added for speed. If a read replica is on your
list of fixes for a current slowness problem, read this before you ship it — the
fix has a correctness price and this topic puts a number on it.

**You'll know it landed when:** you can place linearizable, sequential, causal
and eventual on a ladder and say what each costs *on the read path*, and when
somebody asks for "strong consistency" you ask three questions before agreeing.

## The concept

Strongest first.

**Linearizable.** The system behaves as if there is exactly one copy of the
data. Every operation appears to take effect at a single instant somewhere
between its invocation and its response, and real-time order is respected: if
write W completes before read R begins, R sees W. This is the model people mean
when they say "it just works." It costs a quorum round trip *even for reads*,
because the node answering has to establish that it has not been deposed since
it last heard from anyone — which is Topic 5's material arriving early.

**Sequential.** Everyone agrees on one order of operations, but that order need
not match real time. A read can return a stale value as long as everybody is
stale in the same way.

**Causal.** Operations that causally precede one another are observed in that
order; concurrent operations may be observed in different orders by different
readers. This is the interesting one, because it is the **strongest model that
remains available during a partition** — the ceiling that CAP puts on you, and
therefore where the serious design work happens.

**Eventual.** A liveness promise and nothing more: *if writes stop, replicas
converge.* It says nothing whatsoever about what you can see right now, which is
why "eventually consistent" is a description of a failure mode rather than a
guarantee.

Inside causal sit the four **session guarantees**, and these are the ones that
generate bug reports:

- **read-your-writes** — you see your own writes. Break this and users retype
  things.
- **monotonic reads** — you never see time go backwards. Break this and a value
  appears, disappears, reappears as requests land on different replicas.
- **monotonic writes** — your own writes apply in the order you issued them.
- **writes-follow-reads** — a write that responds to something you read is
  ordered after it. Break this and a reply appears before the comment it answers.

**The 2026 fact that makes this yours rather than theory.** Jepsen's April 2025
analysis of Amazon RDS for PostgreSQL 17.4 reports that multi-AZ clusters
**violate Snapshot Isolation**, exhibiting **Long Fork** and other
G-nonadjacent cycles across the primary and reader endpoints, in every version
they tested from 13.15 through 17.4 — because lock order and WAL order can
differ, so primary and secondaries can disagree about apparent transaction
order. Set `REPEATABLE READ` and read from a reader endpoint and what you
actually have is closer to Parallel Snapshot Isolation. Long Fork means two
readers can observe two concurrent writes in *opposite* orders, which SI
forbids. Source: `jepsen.io/analyses/amazon-rds-for-postgresql-17.4`.

**Read-your-writes on a replica, mechanically.** Postgres has a real primitive
coming for this and it is not here yet. `pg_wal_replay_wait()` was proposed for
17, then added and reverted — the procedure holds a snapshot, which blocks the
very replay it is waiting on. The clean version is `WAIT FOR LSN`, arriving in
PostgreSQL 19 (Beta 3 as of 2026-08-13, GA targeted for Sept/Oct 2026). On the
18 line you do it by hand:

1. capture `pg_current_wal_insert_lsn()` on the primary at commit;
2. return it to the client as an opaque token;
3. on the read path, poll `pg_last_wal_replay_lsn()` on the replica until it is
   `>=` the token, with a deadline after which you fall back to the primary.

That deadline is the whole design. Without it, a lagging replica turns a read
into an unbounded wait, and you have traded a stale read for a hung request.

## How each language actually gets there

**Python only, and the reason is the rule from the repo README:** the mechanism
lives entirely outside the language. This is streaming replication, WAL LSNs and
a routing decision — six near-identical two-pool clients would teach nothing
that one does not. Go (`pgxpool` ×2) and Node (`pg.Pool` ×2) are mechanically
identical and are worth writing once each only if you want to see for yourself
how little of this is language-specific.

**Python.** Two SQLAlchemy 2.x engines with an *explicit per-request routing
decision*, not a global `Session` bound to one of them. The LSN token rides in a
signed cookie or a response header. Two traps, one loud and one quiet:

- **Loud:** a module-level `SessionLocal` bound to the reader makes every write
  fail with `cannot execute INSERT in a read-only transaction`. You find this in
  the first minute.
- **Quiet, and the one that reaches production:** a health check that only ever
  hits the primary, so nothing tells you the replica is 40 seconds behind until
  users do.

**The deployment detail that invalidates half the fixes:** pgbouncer in
**transaction pooling** mode. Any strategy relying on session state — session
pinning, `SET`, session-level advisory locks — is invalid there, because your
next statement may land on a different server connection entirely. Find out your
pooling mode before you design anything around sessions. Topic 7 returns to this
with a sharper example.

## The experiment

Compose gives you `postgres:18` as `pg-primary` plus a streaming standby
`pg-standby` with `recovery_min_apply_delay` set from the `APPLY_DELAY`
environment variable — the honest way to get *deterministic* lag without having
to generate enough load to induce it accidentally. FastAPI (`api`) writes to the
primary and reads from the standby. k6 performs write → immediate read of the
same entity at a configurable gap.

Measure the **stale read rate** as a function of (apply delay × read-after-write
gap). Then implement and measure two fixes:

- **Fix A — sticky primary reads** for N seconds after a session's last write.
  Measure the *cost*, which is the increase in primary QPS. That number is why
  people do not do this, and it belongs in the table.
- **Fix B — LSN token.** Measure the added read p99 and the fallback-to-primary
  rate under lag. This turns "consistency versus latency" from an adjective into
  a millisecond count.

Log `pg_is_in_recovery()` from inside the read path on **every** request. It
costs nothing and it is the only way to know your routing is actually happening
rather than both DSNs resolving to the same host.

## How to run

Blocked while the Docker daemon is down — this topic needs a real streaming
standby and the local fallback cannot fake one. Check with
`python3 ../lab/local/check_env.py`.

```
APPLY_DELAY=500ms docker compose up -d pg-primary pg-standby api
docker compose run --rm k6 run /scripts/topic4_rw.js
FIX=sticky docker compose up -d --force-recreate api && docker compose run --rm k6 run /scripts/topic4_rw.js
FIX=lsn    docker compose up -d --force-recreate api && docker compose run --rm k6 run /scripts/topic4_rw.js
psql -d sep_lab_04_dist -f sql/topic4_stale_reads.sql
```

The one part that runs without Docker is the ladder itself:

```
python3 python/session_guarantees.py
```

It walks two simulated replicas through each of the four session guarantees plus
a Long Fork, and prints which guarantee each observation violates. **It proves
nothing about Postgres and measures nothing** — it is a vocabulary check, it is
labelled as one in its own output, and every violation it reports is found by a
checker rather than announced by the narrator, so a broken check shows up as a
clean run rather than as a confident wrong answer.

`sql/topic4_stale_reads.sql` runs against the empty schema it creates, which is
all it can do here — it has never returned a row on this machine and its header
says so. Read query 0 first when you do have data: if `pct_on_standby` is 0 on a
`fix=none` run you were reading the primary, and a stale-read rate of 0% means
nothing at all.

```
psql -d sep_lab_04_dist -f sql/topic4_stale_reads.sql
```

## Predict, then record

**Predict first, in writing:** at 500ms apply delay with the read fired 50ms
after the write returns, what stale-read percentage do you expect? What do
sticky reads do to primary QPS — 2x? 10x? What does the LSN token add to read
p99, and is that paid by *every* read or only by reads that follow a write?

| Config | Apply delay | Read-after-write gap | Stale reads % | Primary QPS | Read p99 (ms) |
|---|---|---|---|---|---|
| No fix | 500ms | 50ms | | | |
| No fix | 500ms | 2s | | | |
| Fix A sticky | 500ms | 50ms | | | |
| Fix B LSN token | 500ms | 50ms | | | |
| Fix B LSN token | 5s | 50ms | | | |

| Fix B detail | Apply delay | Fallback-to-primary rate | Poll iterations p50 | Poll iterations p99 |
|---|---|---|---|---|
| LSN token | 500ms | | | |
| LSN token | 5s | | | |

**What would mean the experiment is broken, not the prediction wrong:**

- **0% stale reads with no fix at 500ms apply delay.** You are reading from the
  primary. Check the `pg_is_in_recovery()` log line, and check that your pooler
  or DNS is not collapsing both DSNs onto one host. This is the single most
  likely way to "prove" that a bug which exists does not.
- **100% stale reads.** Your write may not have committed before the read fired.
  Confirm the row is visible on the primary immediately after the write returns,
  then look at the read path.
- **Fix B adds no p99.** The replica was already caught up and your poll
  returned on its first iteration. Raise the apply delay above your poll
  interval so the wait is real, and record the poll-iteration count so you can
  see that it happened.
- **Fix A shows no primary QPS increase.** Stickiness is not taking effect —
  most likely there is no session identity at all, or k6 is not preserving the
  cookie between the write and the read.
- **Stale read rate does not change between a 50ms and a 2s gap.** Something
  other than lag is deciding your result. Check that the gap is applied
  client-side and that k6 is not batching the two requests.

## Answer before moving on

1. Which of the four session guarantees does "route reads to the primary for 5
   seconds after a write" actually provide, and which does it not? Name a
   concrete user-visible bug that survives it.
2. Sketch a Long Fork: two writers, two readers, and the exact observation
   pattern Snapshot Isolation forbids. Then explain how a primary plus an async
   standby produces it.
3. Why does linearizability cost a round trip even for a *read* in a Raft
   system? Name the standard optimization and the assumption it quietly makes.
   (This is the bridge between Topics 3 and 5, and the assumption is a clock.)
4. A PM asks for "strong consistency." What three questions do you ask first,
   and what does each possible answer change about the design?

## Next up

[Topic 5 — Consensus, and Raft specifically](../05-consensus-and-raft-specifically/README.md):
the algorithm that turns "which of us is in charge" from an unknown into a fact.
The one you cannot shortcut.
