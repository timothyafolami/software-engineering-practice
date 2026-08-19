# Layer 4 · Topic 5 — Consensus, and Raft specifically

### The takeaway (read this first)

**The one idea:** consensus is how a group of machines agrees on a single
ordered log of decisions despite crashes and partitions, and the whole of Raft
reduces to three rules — at most one leader per term, elected by a majority; a
candidate wins only if its log is at least as up to date as the voter's; an
entry is committed only once a majority holds it *and* the current leader has
committed something from its own term.

**Why it matters in practice:** every piece of infrastructure you rely on for
correctness under failure — etcd, Consul, Kafka's KRaft controller, your cloud
provider's control plane — is a consensus system, and their failure modes only
become legible once you have built one. It is also, per the roadmap, the single
most respected thing you can put on a CV at this level, and the layer's whole
"you own this when" test.

**You'll know it landed when:** your implementation survives
`go test -race -count 20`, and you can explain from memory why a leader must not
commit an entry from a previous term by counting replicas.

## The concept

Derive it rather than memorise it.

You want agreement despite `f` failures. Take any two majorities of a cluster of
`2f+1` nodes: they must share at least one member, because two sets of more than
half cannot be disjoint. That shared member is what makes one majority decision
impossible to contradict by another. That is the entire reason quorums are
majorities — and it is also why a 4-node cluster tolerates the same single
failure as a 3-node one (a majority of 4 is 3, so you can lose one either way)
while costing more and being slower. Even-sized clusters are strictly wasteful,
and you should be able to derive that in ten seconds.

**Terms** are Topic 3 applied: a logical clock, monotonic, incremented on every
election attempt. They exist so that a stale leader is recognisable *without
consulting a wall clock* — a node seeing a higher term than its own knows it is
behind, and no amount of NTP skew changes that.

The four parts, in the order the labs build them:

- **Leader election (3A).** Randomised election timeouts break symmetry so that
  split votes are rare and self-correcting; heartbeats from a live leader
  suppress elections; one vote per node per term plus a majority requirement
  gives *at most one leader per term* — the safety property everything else
  rests on.
- **Log replication (3B).** `AppendEntries` carries `prevLogIndex` and
  `prevLogTerm`; a follower whose log does not match at that point rejects,
  forcing the leader to walk backwards until the logs agree, then overwrite the
  divergent tail. The **election restriction** (§5.4.1) — a candidate whose log
  is behind cannot win — is the subtle part, and where implementations are
  silently wrong while passing simple tests.
- **Persistence (3C).** `currentTerm`, `votedFor` and the log must reach stable
  storage **before** you reply to an RPC that depends on them. Get the ordering
  backwards and everything passes, then one run in two hundred fails: a vote was
  granted, the process died before `votedFor` was durable, the node restarted
  and voted again in the same term. Two leaders in one term, and every safety
  argument in the paper collapses at once.
- **Snapshots (3D).** Logs are unbounded and memory is not; snapshots make the
  log finite, and `InstallSnapshot` handles a follower that has fallen further
  behind than the leader's retained log.

**The one to sit with, and the reason to read the extended paper:** a leader may
**not** commit an entry from a previous term merely because a majority of nodes
store it (Figure 8). It must first commit an entry from its *own* term, which
implicitly commits everything before it. Construct the losing scenario by hand,
on paper, before writing any code — a sequence where an entry is on a majority,
is declared committed by replica count, and is then overwritten by a later
leader that never had it. If you cannot build that scenario yourself, you will
write this bug, and no simple test will catch it.

## How each language actually gets there

**Go only, and the reason is not arbitrary.** MIT 6.5840's labs, its `labrpc`
RPC shim, its partition simulator and the race detector the handout expects you
to develop under are **all Go**. The natural course for this material and one of
the languages already in this lab coincide, which is exactly why the roadmap
picks 6.5840. Reimplementing that harness in Python to "use my main language"
would cost the month and teach nothing about Raft — the harness is not the
lesson, it is the scaffolding that makes the lesson checkable.

Go also happens to fit the algorithm: `sync.Mutex` around a single state struct,
goroutines per peer for outbound RPCs, and `select` on a timer channel for the
election timeout are all a near-transcription of Figure 2. And the race detector
matters here more than anywhere else in the lab — a data race in Raft state does
not show up as a crash, it shows up as a lost log entry.

Current Go is **1.26** (1.25 and 1.26 both supported as of Aug 2026), though the
toolchain on this machine is **go1.24.5** — see [`../lab/`](../lab/README.md).
That matters for one thing only: `testing/synctest` graduated in 1.25 (the old
experimental API was removed in 1.26) and virtualizes time inside a test bubble,
making timeout-driven tests deterministic and instant. It is unavailable until
you upgrade, and it is **not** worth bolting onto the MIT harness anyway — do
not fight the harness. Save it for the lease timers and retry logic in Topic 7.

The two concurrency rules used throughout the implementation here, both of which
produce deadlocks that only appear under load: **no RPC is ever issued while
holding the lock**, and **no channel send happens while holding the lock**.

## The experiment

**Part A — consensus you did not write (about an hour, and do it first).** A
3-node etcd cluster in compose; `etcdctl endpoint status` shows you the leader.
Then, with Pumba or Toxiproxy:

1. Partition one follower. Observe that nothing user-visible happens, and be
   able to say why in terms of majorities.
2. Partition two nodes away from the third, then from the **minority** side
   issue a write and a linearizable read. Record the exact error and how long it
   took to arrive — that latency is your Topic 1 CP choice, in milliseconds.
3. Partition the leader alone, and time the interval from isolation to a new
   leader accepting writes. Note the new term.
4. Heal the partition, watch the old leader step down, and check what it did
   with any writes it accepted while isolated — including whether it accepted
   any at all.

**Part B — the actual work.** MIT 6.5840 Lab 3, parts 3A → 3D. Handout
constraints that shape the design, so read them before coding rather than after:
heartbeats no more than 10 per second; a new leader elected within 5 seconds of
a failure (which is why election timeouts here exceed the paper's 150–300ms
suggestion); a 120-second cap per test and under 600 seconds for all of Lab 3;
grading runs *without* `-race` but you develop *with* it; membership changes
(§6) are not required.

**What is in this folder** is a working Raft covering the ground of 3A, 3B and
3C — `raft.go` (Figure 2, with a comment at every point it departs from a naive
reading), `network.go` (a stand-in for `labrpc` that deep-copies arguments and
checks reachability twice, so "the request arrived but the reply was lost" is
expressible), `persister.go` (a byte slice on purpose, so the save points are
visible), and `config_test.go` (a harness that retries `checkOneLeader` while a
cluster settles but fails immediately and loudly on two leaders in one term).
**3D is deliberately absent.** Snapshots are where this folder stops and the
real labs start; get them from MIT rather than from here.

Keep a `RAFT-LOG.md` beside the code: one line per test failure — which test,
which invariant broke, what you changed. That file is the real deliverable of
this topic and the thing you will still be able to talk about in two years.

## How to run

The Go implementation needs nothing running — no Docker, no network:

```
cd golang/raft && go test -race ./...
cd golang/raft && go test -race -count 20 ./...
```

The MIT labs, which are the actual assignment:

```
git clone git://g.csail.mit.edu/6.5840-golabs-2026 6.5840
cd 6.5840/src && make RUN="-run 3A" raft1
go test -run 3A -race -count 20 ./raft1/...
```

Part A's etcd cluster (blocked while the Docker daemon is down —
`python3 ../lab/local/check_env.py`):

```
docker compose up -d etcd1 etcd2 etcd3
docker compose exec etcd1 etcdctl \
  --endpoints=etcd1:2379,etcd2:2379,etcd3:2379 endpoint status -w table
pumba netem --duration 60s --tc-image gaiadocker/iproute2 loss --percent 100 etcd1
```

Pumba's `netem` needs `NET_ADMIN` and a `tc`-capable image, which is why
`--tc-image` is not optional here; see [`../lab/`](../lab/README.md). For a
plain partition, Toxiproxy is less trouble.

## Predict, then record

**Predict first, in writing:** how long from leader kill to a new leader
accepting writes? What error does a minority-side client get, and after how long
— does it fail fast, or hang until its own timeout? Does a *read* succeed on the
minority side, and does the answer depend on a flag?

| Scenario | Writes succeed? | Client error + latency | Time to new leader | Term after |
|---|---|---|---|---|
| 1 follower partitioned | | | | |
| 2 of 3 partitioned (minority view) | | | | |
| 2 of 3 partitioned (majority view) | | | | |
| Leader partitioned alone | | | | |
| Partition healed | | | | |

| Lab part | First green | Failures found by `-count 20` after "passing" | Hours | Hardest bug |
|---|---|---|---|---|
| 3A | | | | |
| 3B | | | | |
| 3C | | | | |
| 3D | | | | |

**What would mean the experiment is broken, not the prediction wrong:**

- **Minority-side reads succeed.** etcd reads are linearizable by default and
  need a quorum, but `--consistency=s` opts into serializable (stale) local
  reads. Check you did not pass it. If you did, that is not a bug — that is
  Topic 4's ladder showing up in a real tool, and worth recording as such.
- **Elections take longer than 5 seconds.** Before blaming your code: check that
  `--heartbeat-interval` is not near or above the election timeout, and check
  the container is not CPU-starved. A throttled container makes every consensus
  timing test lie — and that is the same CFS-throttling mechanism Topic 7 turns
  into a split brain, so reproduce it deliberately with `cpus: '0.1'` once you
  have the honest number.
- **3A passes on the first run and you move on.** "Passed once" is not passed.
  Raft bugs are overwhelmingly timing-dependent, and a run count of 1 is a coin
  flip. If `-count 20` finds nothing, run `-count 100` overnight before
  believing it.
- **Tests pass without `-race` and you never ran them with it.** Develop with
  `-race` always.
- **Two leaders in one term and the harness retried past it.** That is a harness
  bug, not a flake. `checkOneLeader` may retry a cluster that is *settling*; it
  must never retry away two leaders in the same term.

## Answer before moving on

1. Construct Figure 8 concretely: a sequence where a leader commits a
   previous-term entry by replica count and a later leader overwrites it. Then
   explain why committing a current-term entry first fixes it.
2. A 4-node cluster: state its fault tolerance, and explain why it is strictly
   worse than 5 and no better than 3.
3. Your implementation passes 3A fifty times and fails once in two hundred. What
   class of bug is that almost always, and what do you instrument first?
4. Why must persistence happen *before* the reply rather than after? Give the
   two-node sequence that breaks if you get it backwards.
5. Name one problem in your own production stack where you would genuinely want
   a replicated log, and one where reaching for it would be a mistake — and say
   what you would use instead in the second case.

## Next up

[Topic 6 — The outbox, sagas, and why 2PC is avoided](../06-outbox-sagas-and-why-2pc-is-avoided/README.md):
you have a replicated log and a Postgres. Now the thing that goes wrong between
your database and everything downstream of it.
