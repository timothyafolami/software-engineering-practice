# Layer 3 · Data and databases

The roadmap calls this the highest-return layer on the page, and it is right:
**most application bugs that survive code review are database semantics bugs.**
They survive review because the code looks correct. `SELECT`, check a condition,
`UPDATE` — reads fine in a diff, and is wrong the moment two of them run at once.
Nothing in the diff tells you that.

Eight topics. All the teaching content lives in the topic READMEs below; this
page is the index.

| # | Topic | Folder | Languages |
|---|---|---|---|
| 1 | Isolation levels, and precisely which anomaly each permits | [`01-isolation-levels/`](01-isolation-levels/README.md) | Python, Go |
| 2 | MVCC, and what vacuum falling behind does to latency | [`02-mvcc-and-vacuum/`](02-mvcc-and-vacuum/README.md) | Python |
| 3 | Indexes: B-tree internals, column order, and the cost of each one | [`03-indexes/`](03-indexes/README.md) | Python |
| 4 | Reading a query plan fluently | [`04-reading-a-query-plan/`](04-reading-a-query-plan/README.md) | Python, Go, Node, C++ |
| 5 | Locking, deadlocks, and the migration that took the site down | [`05-locking-and-deadlocks/`](05-locking-and-deadlocks/README.md) | Python |
| 6 | Finding N+1 systematically, not by noticing | [`06-finding-n-plus-1/`](06-finding-n-plus-1/README.md) | Python, Node |
| 7 | Connection pools, worker counts, and the container CPU limit | [`07-connection-pools/`](07-connection-pools/README.md) | Python, Go, Node |
| 8 | Replication lag, read-your-own-writes, and the one-way doors | [`08-replication-lag/`](08-replication-lag/README.md) | Python |

**Work them in order of suspicion, not in numbered order.** If you have a
production latency problem right now, five of the most common causes of "it got
slow and nothing changed" live here, and `SEQUENCE.md` orders them by how likely
each is to be your bug and how mechanically detectable it is:
**6 → 4 → 3 → 7 → 2 → 1 → 5 → 8**.

## The shared lab

One database, one schema, one seed, serving every topic — plus a Docker stack for
the two topics that need more than one Postgres. Build it once:
**[`lab/README.md`](lab/README.md)**. Topic READMEs assume it and do not restate
it.

## Why so few languages here

The repo's rule is *pick the languages that make the mechanism visible, and state
a one-line reason whenever you use fewer than six.* This is the layer where fewer
is correct, and the reason is the same one every time: **the mechanism lives in
Postgres, not in the client.** Write skew, vacuum starvation, plan flips, lock
queues and replication lag behave identically whichever language issued the
statement, so a second client would print the same table twice.

Python anchors every topic — it is the production stack, and its programs read
like application code. A second or third language appears only where **the
client's own behaviour is the finding**: retry ergonomics (Topic 1), prepared
statements and plan caching across driver protocols (Topic 4), application-layer
batching (Topic 6), and pool semantics and their defaults (Topic 7). Each of
those topics states its reason in its own first paragraph.

## The roadmap's ownership test

> Construct a write skew example on a whiteboard, say which isolation level
> prevents it and what that costs, and read a slow query plan and name the fix
> before running anything.

Three separable skills: **construct** (Topic 1), **name the cost** (Topic 1 — the
retry loop you have to write, and the false aborts a sequential scan causes),
**predict a plan** (Topics 3-4). The other five topics are the operational half
that makes those three survive contact with production.

## Resources

- [PostgreSQL 18 docs §13.2, Transaction Isolation](https://www.postgresql.org/docs/18/transaction-iso.html) — the anomaly table in Topic 1 comes from here; short, and better than most books on this specific point
- [PostgreSQL 18 release notes](https://www.postgresql.org/docs/18/release-18.html) — the version-specific behaviour several topics depend on
- **Kleppmann, *Designing Data-Intensive Applications*.** The roadmap says "chapter 7 above all" — that is the 1st edition's transactions chapter. In the 2nd edition (Kleppmann & Riccomini, 2026) transactions are **Chapter 8**. Read it alongside Topic 1.
- **Petrov, *Database Internals*.** The roadmap's other named resource for this layer, and the one that answers the "but *why* is it built that way" questions the Postgres docs treat as settled: Part I is B-tree mechanics for Topic 3, Part II is replication and consensus for Topic 8.
- [Jepsen: Amazon RDS for PostgreSQL 17.4](https://jepsen.io/analyses/amazon-rds-for-postgresql-17.4) — read the whole thing; the best writing anywhere on guarantee vs. claim, and the source of Topic 8's central warning

Each topic carries its own reading list for the specific mechanism it teaches,
including which widely-repeated advice Postgres 17 and 18 have made stale.

## Next up

Build the lab, then Topic 1 end to end — two lock-step `psql` sessions first, then
under real concurrency. Start there: write skew is the roadmap's named test for
this whole layer, and it is one evening.
