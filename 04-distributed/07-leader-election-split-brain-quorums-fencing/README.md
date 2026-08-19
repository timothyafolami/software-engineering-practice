# Layer 4 · Topic 7 — Leader election, split brain, quorums, and fencing tokens

### The takeaway (read this first)

**The one idea:** a lock with a timeout does not prevent two workers acting at
once — it only makes it rarer — so safety has to be enforced at the *resource*,
by rejecting any write that carries a stale fencing token.

**Why it matters in practice:** the pause that causes this is not exotic. It is
a GC pause, a **CFS throttle on a CPU-limited container**, a VM live migration,
a host that started swapping, or an event loop blocked by one synchronous call.
Your production containers have CPU limits. A throttled process that resumes
after its lease expired and carries on working is the most plausible split brain
you will ever meet, and it is a direct consequence of the same throttling that
may be behind your latency problem.

**You'll know it landed when:** you can explain why "check the lock is still
mine, then write" can never be made safe, and you reach for a monotonically
increasing token checked *by the storage system* rather than by your
application.

## The concept

A **lease** is a lock with an expiry. You need the expiry, because a holder can
die without releasing — a lock with no expiry plus one crashed holder is a
permanently wedged system. But the expiry creates the hazard: the holder can be
**paused** past its own expiry, wake up believing it still holds the lock, and
act on that belief.

Checking validity before acting does not help. The check and the act are not
atomic, and an arbitrarily long pause can land between them:

```
if lease.still_valid():     # true
    # <-- 15 second pause here. Lease expires. Someone else takes it.
    write_the_payout()      # two writers, both "correct"
```

You cannot shrink that window to zero, because you do not control how long your
process is descheduled. Any argument that ends with "but the window is very
small" is an argument about probability, not safety, and at production request
rates a small window is a daily event.

The fix is a **fencing token**: a monotonically increasing number issued with
each acquisition, carried on every write, and **validated by the resource**:

```sql
UPDATE payouts SET status = 'sent', fence = $token
WHERE id = $1 AND fence < $token;   -- zero rows updated == you are stale
```

The validation must be *in the write*. If it is in an `if` in your application,
you have moved the race, not removed it — the same pause can land between your
check and your `UPDATE`. Zero rows updated is the signal, and the stale holder
must log loudly and exit rather than retry.

**Kleppmann's "How to do distributed locking" (2016) is still canonical and
still correct.** Redlock issues no fencing token, and its safety argument rests
on bounded network delay, bounded process pauses and bounded clock error — none
of which you can enforce, and the second of which this whole topic is about. Too
heavyweight for efficiency locks, not safe for correctness locks. Nothing in a
decade has changed that.

**Quorums.** `2f+1`, because any two majorities intersect (Topic 5 derives it),
which is why even-sized clusters are strictly wasteful. And a distinction that
is commonly and expensively conflated: "quorum" in **leaderless replication**
(`R + W > N`) is a *different, weaker* thing than a **consensus** quorum. The
first guarantees that a read set overlaps a write set, so you see the value. The
second guarantees agreement on an *order*. Overlap is not agreement, and a
system that gives you the first will not give you the second no matter how you
tune R and W.

**For a Postgres-shaped stack.** `pg_advisory_lock` is genuinely useful and
genuinely dangerous behind pgbouncer in transaction pooling mode: a
*session-level* advisory lock is held on a server connection you stop owning the
moment your transaction ends, so you can hold a lock you cannot release and
release one you do not hold. Use `pg_advisory_xact_lock` (transaction-scoped),
or keep that path off the pooler entirely. And the cheapest correct fencing
token is one you already have: a `leases` table with an `epoch` integer,
incremented on acquisition, guarded on every write.

## How each language actually gets there

**All six.** The question this topic asks — *what makes your process stop
running for longer than its lease* — has a different answer in every runtime,
and the punchline is that fencing is the only fix common to all six. That spread
is the reason to write it six times rather than once.

**Python — the keepalive is a coroutine, and one blocking call starves it.**
A `Leader` async context manager over `pg_advisory_xact_lock`, or an etcd lease
with a keepalive task. The hazard connects straight back to Layer 1 Topic 3:
your lease renewal is a coroutine on the same event loop as everything else, so
a single synchronous call anywhere — a blocking DB driver, a big `json.loads`,
a `requests` call somebody left in — stops the renewal while the process is
perfectly healthy and busy. Your lease expires because you were *working*. This
is far more common in Python services than GC pauses.

**Node.js — the same hazard, with no way out.** Same event loop, same
starvation, but stricter: there is no threading module to escape to, only
`worker_threads`, which are separate isolates. A synchronous `bcrypt.hashSync`
or a large `JSON.parse` on the request path is enough to lose a 10-second lease.

**Java — the canonical version, and the one Kleppmann drew.** A stop-the-world
GC pause on a large heap can exceed a lease TTL outright, and unlike the event
loop cases there is nothing in your code to point at — the pause belongs to the
collector. Worth knowing alongside it: a thread can only be paused at a
**safepoint**, so a long counted loop can *delay* the pause the JVM is trying to
take, which makes the actual stop-the-world duration longer than the collector's
own accounting suggests.

**Go — the runtime that mostly does not have the problem, which is the point.**
The netpoller and asynchronous preemption mean a lease-renewal goroutine keeps
getting scheduled even when other goroutines are busy or blocked. So in Go the
pause must come from *outside* the runtime: a CFS throttle, a SIGSTOP, a
migrating VM. Go is also where the reference implementation lives —
etcd's `clientv3/concurrency` (`NewSession`, `NewElection`, `Campaign`,
`Resign`) is worth reading in full. The detail most code ignores: the session's
`Done()` channel is your "you have lost the lock" signal, and a leader that does
not `select` on it keeps working after being deposed. etcd also hands you a
fencing token for free — the key's `CreateRevision`.

**Rust — no GC, and the hazard survives anyway.** No collector means no
collector pause, which is exactly why Rust is worth including: a tokio
keepalive task on the `current_thread` flavour is starved by a blocking call in
another task just as thoroughly as Python's is, and `spawn_blocking` is the fix.
The lesson is that the hazard is about *scheduling*, not about garbage, and a
language marketed on not having a GC does not get an exemption.

**C++ — nothing between you and the kernel, so the pause is the kernel's.** No
runtime, no collector, no event loop: the only thing that can stop your renewal
thread is the OS. A CFS throttle when the cgroup's quota is exhausted, a
page-fault storm when the host starts swapping, a `SIGSTOP`. This is the version
that makes the general shape obvious — every other runtime in this list adds its
*own* reasons to pause on top of the ones C++ already has, and none of them
removes these.

## The experiment

Take Topic 6's relay and make it a **singleton**, elected between two
containers, then break it on purpose.

1. **Baseline.** Both `relay-a` and `relay-b` running, one elected. Confirm that
   only one publishes.
2. **The pause.** `docker kill -s SIGSTOP relay-a` for longer than the lease
   TTL. `relay-b` takes over. `SIGCONT relay-a` and watch it publish the batch
   it believed it owned. Count rows published twice — and, the number that
   actually matters, rows in a `payouts` table executed twice.
3. **Fencing.** The elected leader receives an `epoch` (etcd's `CreateRevision`,
   or an incrementing counter from a `leases` table). Every write carries it and
   is guarded by `AND fence < $epoch`. Re-run the SIGSTOP: the stale writer must
   get zero rows updated, log loudly, and exit — not silently continue.
4. **The realistic version.** Instead of `SIGSTOP`, set `cpus: '0.1'` on
   `relay-a` and load it, so the pause comes from **CFS throttling**. This is
   how it actually happens in production, and the point is seeing that the
   mechanism and the fix are identical to the SIGSTOP case.
5. **The six-language pause audit.** Each language runs a lease holder whose
   renewal loop targets a fixed interval and records the *actual* gap between
   consecutive renewals, while that runtime's characteristic hazard is applied:
   a blocking call on the event loop (Python, Node, Rust), an allocation storm
   on a large heap (Java), a CPU-bound goroutine flood (Go), a tight loop with
   the cgroup quota exhausted (C++). Record the longest renewal gap each
   produces. The interesting result is which runtimes exceed a 10-second TTL
   from their *own* behaviour, with no external fault at all.

## How to run

**Part 5, the six-language pause audit, runs locally with nothing installed.**
Each program holds a lease with a 10-second TTL, renews on a 1-second timer,
measures the *actual* gap between renewals with that runtime's **monotonic**
clock (Topic 3 — a wall clock here would let an NTP step masquerade as a pause,
and the question is a correctness one), and applies that runtime's
characteristic hazard.

```
python3 python/pause_audit.py
node nodejs/pause_audit.js
cd golang && go run pause_audit.go && cd ..
cd rust/pause_audit && cargo run --release && cd ../..
g++ -O2 -std=c++17 -pthread -Wall -Wextra -o /tmp/l4t7_cpp cpp/pause_audit.cpp && /tmp/l4t7_cpp
javac java/PauseAudit.java -d /tmp/javabuild && java -Xmx2g -cp /tmp/javabuild PauseAudit
```

Three of them are worth a second look:

- **Go** runs three configurations and expects to survive all of them — that is
  the finding, not a disappointment. If one blows the TTL, check the machine was
  not already loaded before recording it.
- **Rust** runs five, and rows 3 and 4 are the point: one blocking task on the
  `multi_thread` flavour usually survives, so "just use `multi_thread`" looks
  like the fix. The pool is finite. Row 4 is what you get under load.
- **C++** reproduces the real hazard rather than approximating it: it forks a
  child to send `SIGCONT`, then `SIGSTOP`s itself. That is exactly what
  `docker kill -s SIGSTOP` does in parts 2 and 3. The **cgroup CFS-throttling
  variant does not exist on Darwin**, and the program says so and prints the
  container command instead of running something else and calling it the same.
- **Java** *measures* rather than asserts. Whether G1 on a 2g heap can blow a
  10-second TTL is a question about your JVM, and the program prints the flags to
  re-run under `SerialGC`, `ParallelGC` and `ZGC` with `-Xlog:gc`. The comparison
  is the exercise; a run that holds the lease is a real answer about a
  *configuration*, not about Java.

**Parts 2 and 3 — the paused leader, with and without fencing — also run
locally**, against whatever Postgres is listening. Two workers contend for a
lease row; the elected one is paused for longer than the TTL, the other takes
over, and the paused one wakes still believing it leads.

```
python3 python/fencing_demo.py --fencing 0
python3 python/fencing_demo.py --fencing 1
psql -d sep_lab_04_dist -f sql/topic7_duplicate_payouts.sql
```

The pause is a thread that stops renewing and stops working, which is what a
`SIGSTOP`, a CFS throttle, a stop-the-world collection and a blocked event loop
all look like **from the database's side** — and the database's side is where
the safety argument has to hold. What it does not reproduce is a pause landing
inside an in-flight statement; that needs the container version below.

Read the run's own output for the `stale-epoch attempts` line before anything
else. A run where the stale writer never attempted a write tested nothing, and
the program says so rather than printing a clean table.

Teardown for the whole layer: `python3 ../lab/local/teardown_lab.py`.

Parts 1–4 under compose (blocked while the Docker daemon is down —
`python3 ../lab/local/check_env.py`):

```
docker compose up -d postgres etcd1 relay-a relay-b
docker kill -s SIGSTOP $(docker compose ps -q relay-a)
sleep 15   # longer than the lease TTL
docker kill -s SIGCONT $(docker compose ps -q relay-a)
psql -d sep_lab_04_dist -f sql/topic7_duplicate_payouts.sql
FENCING=1 docker compose up -d --force-recreate relay-a relay-b   # then repeat
```

## Predict, then record

**Predict first, in writing:** with a 10-second lease TTL and a 15-second
SIGSTOP, how many duplicate payouts? How long does failover take — closer to the
TTL, or the TTL plus a full election? With fencing on, how many writes does the
stale leader attempt, and how many of them succeed? And for part 5: which
runtimes can blow a 10-second TTL with no external fault?

| Scenario | Duplicate payouts | Stale-writer attempts | Stale-writer rejections | Failover time |
|---|---|---|---|---|
| No fencing, SIGSTOP 15s | | | | |
| Fencing, SIGSTOP 15s | | | | |
| No fencing, CFS throttle | | | | |
| Fencing, CFS throttle | | | | |

| Runtime | hazard applied | longest renewal gap | exceeded 10s TTL? |
|---|---|---|---|
| Python | blocking call on the loop | | |
| Node.js | blocking call on the loop | | |
| Go | CPU-bound goroutine flood | | |
| Rust | blocking call in a task | | |
| C++ | cgroup quota exhausted | | |
| Java | allocation storm, 2g heap | | |

**What would mean the experiment is broken, not the prediction wrong:**

- **The resumed leader never writes anything.** Either the SIGSTOP window was
  shorter than the lease TTL, or your client re-checked on resume and
  self-terminated — etcd's session `Done()` does exactly this if it is wired
  correctly. That is *good code*, but it means you tested the library rather
  than the hazard. Disable that check deliberately for one run, observe the
  hazard, then re-enable it and keep it.
- **Fencing "works" but you never see a rejected write.** Verify the guard is in
  the `UPDATE`'s `WHERE` clause and not an application-level `if`, then verify
  the stale leader attempted a write at all. Log every attempt with its epoch,
  not just the successes.
- **Zero duplicate payouts without fencing.** Either the relay had no in-flight
  batch when it was stopped (stop it mid-batch, under load), or a unique
  constraint is silently protecting you — again correct in production, fatal to
  the demonstration. Find out which, and say which in the table.
- **Failover time equals the TTL exactly, every run.** Suspiciously clean. Check
  you are measuring from the stop rather than from the next renewal attempt, and
  that your measurement clock is monotonic (Topic 3).
- **Go's pause audit shows a long gap.** That is a finding, but check first that
  you did not create the gap yourself with a `runtime.LockOSThread`, a cgo call,
  or `GOMAXPROCS=1` plus a loop the scheduler cannot preempt. Go's whole claim
  here is that it takes an external fault to starve a goroutine.
- **The C++ cgroup variant "passes" on macOS.** It cannot. There are no cgroup
  files on Darwin; if the program reported a result it read something else or
  silently skipped the hazard. Run it inside a container.

## Answer before moving on

1. Why can "check the lease is still valid, then write" never be made safe
   without a token, no matter how tight the check?
2. etcd gives you `CreateRevision` as a fencing token. What property does it
   have that a timestamp does not, and why does it still work if every clock in
   the cluster jumps an hour?
3. Postgres advisory locks behind pgbouncer in transaction pooling: describe the
   failure step by step, in terms of server connections.
4. Your fencing token is validated by a downstream service you do not own. Is
   that safe? State the precise condition under which it is, and what you would
   do if that condition does not hold.
5. Of the six runtimes above, which would you least like to hold a 10-second
   lease in, and what would you change about the lease design rather than about
   the runtime?

## Next up

**Layer 5 — Designing for failure.** Timeout budgets across a call chain,
retries with jitter *and a budget*, circuit breakers, load shedding, and
metastable failure — the state where a system stays down after the trigger is
gone because the retry load has become the problem. Topics 1 and 2 here are its
prerequisites, and Layer 5 is where a production latency problem finally gets a
name.
