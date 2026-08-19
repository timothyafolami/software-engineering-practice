# Layer 4 · Distributed systems

The moment you have two processes and a network you are here, whether you meant
to be or not. You already are: FastAPI in one container, Postgres in another, a
payment processor over the wire.

The roadmap's "you own this when" for this layer is unusually concrete, and it
is Topic 5. The other six make Raft *mean* something — and, more usefully, they
turn "the service feels slow" into a named, measurable failure mode. Slow is not
a lesser form of down. In a distributed system slow is worse than down, because
down is unambiguous and slow is not.

| # | Topic | Roadmap bullet | Languages |
|---|---|---|---|
| 1 | [Partial failure and the ambiguous result](01-partial-failure-and-the-ambiguous-result/README.md) | partial failure; CAP correctly | all six |
| 2 | [Idempotency keys, atomically](02-idempotency-keys-atomically/README.md) | idempotency; effectively-once | Python, Go, Node |
| 3 | [Clocks lie](03-clocks-lie/README.md) | clocks; logical & vector clocks | all six |
| 4 | [Consistency models and the replica lag you already have](04-consistency-models-and-replica-lag/README.md) | the consistency spectrum | Python |
| 5 | [Consensus, and Raft specifically](05-consensus-and-raft-specifically/README.md) | consensus — **the "you own this when"** | Go |
| 6 | [The outbox, sagas, and why 2PC is avoided](06-outbox-sagas-and-why-2pc-is-avoided/README.md) | outbox, sagas, 2PC | Python, Node |
| 7 | [Leader election, split brain, quorums, fencing](07-leader-election-split-brain-quorums-fencing/README.md) | leader election; fencing | all six |

## The language set

**Six: Python, Node.js, Go, Rust, C++, Java** — the repo's working set, and it
does not narrow here. An earlier draft of this layer declared a three-language
policy and called six "a mistake." That was wrong and it has been reversed.

Not every topic uses all six, and each topic states its reason in one line.
Where the *runtime* is the subject — how a client reports a failed call
(Topic 1), which clock a duration reads (Topic 3), what makes a process stop
long enough to lose its lease (Topic 7) — all six earn their place, and the
contrast is the lesson. Where the mechanism lives outside the language — a
unique index (Topic 2), streaming replication (Topic 4), MIT 6.5840's Go
harness (Topic 5), row locking plus a broker (Topic 6) — fewer is correct, and
six near-identical Postgres clients would teach nothing.

## The lab

Every compose-driven experiment in this layer shares one stack: service names,
ports, environment variables and file paths are specified once in
[`lab/README.md`](lab/README.md). Topic run blocks use those names, so read it
before changing any of them.

That file also carries the macOS 27 / arm64 constraints — one shared clock
across all containers, Pumba's `netem` requirements, and why every table here
asks for ratios rather than absolute latencies — plus the no-Docker fallback for
the machine this was written on, where the daemon is down and k6 is not
installed.

## How this layer is written

**No number in this layer is a measurement, and there is no "What I saw"
section anywhere in it.** Layer 1 shipped a table of benchmark numbers nobody
reproduced, including a "0 lost updates" race result that was really the
compiler hoisting the loop away. So every topic here ends with *Predict, then
record*: a prediction written before you run anything, a **blank** table, and an
explicit note on **what result would mean the experiment is broken rather than
your prediction wrong**. That last part would have caught all four of Layer 1's
defects.

The same rule applies to prose: every number on a page here is either derived on
that page or carries a source. An uncited statistic is the same defect as a
fabricated table, just harder to spot.

## The "you own this" test (from the roadmap)

> A working Raft implementation that passes the test suite. There is no
> shortcut and no substitute.

That is Topic 5, and it is the spine of the layer. Topics 1–4 are what make it
comprehensible; Topics 6–7 are what you do with it once you have it.

## Resources

- **DDIA 2nd ed.** (Kleppmann & Riccomini, March 2026) — ch. 9 *The Trouble with
  Distributed Systems*, ch. 10 *Consistency and Consensus* (rewritten almost
  entirely for 2e). The roadmap says "part 2"; in 2e that is chapters 9–11.
- **MIT 6.5840, Spring 2026** — `pdos.csail.mit.edu/6.824/`. Lectures, notes,
  papers and labs all public, no login.
- **The Raft paper** — read the *extended* version; the labs follow Figure 2
  exactly, and §5.4.1 and Figure 8 are where precision is mandatory.
- **Jepsen** — start with `jepsen.io/analyses/amazon-rds-for-postgresql-17.4`
  (April 2025); `jepsen.io/consistency` is the best single reference for Topic 4.
- **Kleppmann, "How to do distributed locking" (2016)** — Topic 7's source text.
- **Bronson et al., "Metastable Failures in Distributed Systems"** (HotOS 2021)
  — the bridge from Topic 1's retry loop to Layer 5.
- **Marc Brooker's blog** — the best current writing on the operational reality
  of everything in this layer.
