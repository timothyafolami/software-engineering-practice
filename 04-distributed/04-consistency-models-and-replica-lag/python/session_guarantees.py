"""
Layer 4 Topic 4 -- the consistency ladder, as a vocabulary check you can run.

WHAT THIS IS: a simulation. Two in-memory replicas behind a primary, with a
deterministic apply delay. It proves NOTHING about Postgres, and it is not the
experiment -- the experiment needs a real streaming standby and is blocked while
the Docker daemon is down (`python3 ../lab/local/check_env.py`).

WHAT IT IS FOR: naming things. Given an observation, which guarantee does it
violate? That question is the difference between "the app is flaky" and "reads
after writes are landing on a lagging replica and read-your-writes is broken",
and the second one has a fix.

WHAT THIS DEMONSTRATES, in order:
  1. the four session guarantees, one scenario each, each one broken and then
     repaired by a named routing rule. Every violation is DETECTED by a checker
     rather than announced by the narrator -- if the checker were wrong, the
     output would say the run was clean.
  2. a Long Fork: two writers, two readers, and the exact observation pattern
     Snapshot Isolation forbids. This is the shape Jepsen reports for multi-AZ
     Amazon RDS for PostgreSQL (jepsen.io/analyses/amazon-rds-for-postgresql-17.4,
     April 2025), and it is the answer to question 2 of "Answer before moving on".
  3. the two fixes from the experiment -- sticky primary reads and an LSN token --
     applied to the same trace, with the cost of each stated in the units the
     record table asks for: extra primary reads, and extra polls.

WHAT TO LOOK FOR IN THE OUTPUT: the VIOLATION lines, and then the same scenario
under a fix with no violations and a cost. There is no scenario here where a
guarantee is free.

  python3 python/session_guarantees.py
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field


# --------------------------------------------------------------- the cluster

@dataclass
class Replica:
    """A replica that applies the primary's log after `lag` operations.

    Lag counted in operations rather than milliseconds on purpose: this is a
    simulation, and a simulated millisecond is a number I would be inventing.
    An operation is a thing that actually happens in the trace.
    """
    name: str
    lag: int
    applied: int = 0
    store: dict[str, str] = field(default_factory=dict)
    lsn: int = 0

    def catch_up(self, log: list[tuple[int, str, str]]) -> None:
        target = max(0, len(log) - self.lag)
        while self.applied < target:
            lsn, key, value = log[self.applied]
            self.store[key] = value
            self.lsn = lsn
            self.applied += 1

    def read(self, key: str) -> str | None:
        return self.store.get(key)


@dataclass
class Cluster:
    replicas: list[Replica]
    log: list[tuple[int, str, str]] = field(default_factory=list)
    primary: dict[str, str] = field(default_factory=dict)
    _lsn: itertools.count = field(default_factory=lambda: itertools.count(1))
    primary_reads: int = 0
    replica_reads: int = 0
    poll_iterations: int = 0
    fallbacks: int = 0

    def write(self, key: str, value: str) -> int:
        lsn = next(self._lsn)
        self.primary[key] = value
        self.log.append((lsn, key, value))
        for r in self.replicas:
            r.catch_up(self.log)
        return lsn

    def read_primary(self, key: str) -> str | None:
        self.primary_reads += 1
        return self.primary.get(key)

    def read_replica(self, replica: int, key: str) -> str | None:
        self.replica_reads += 1
        return self.replicas[replica].read(key)

    def read_lsn_token(self, replica: int, key: str, token: int,
                       max_polls: int = 3) -> tuple[str | None, str]:
        """Fix B. Poll the replica until it has replayed past the token.

        The deadline is the whole design. Without it a lagging replica turns a
        read into an unbounded wait, and you have traded a stale read for a hung
        request -- which is worse, because a hung request holds a connection.
        """
        r = self.replicas[replica]
        for _ in range(max_polls):
            self.poll_iterations += 1
            if r.lsn >= token:
                self.replica_reads += 1
                return r.read(key), f"replica {r.name}"
            # Replay is not paused while you poll -- the standby is applying WAL
            # the whole time. Each poll therefore closes the gap by one
            # operation. A model where the replica never advances would make
            # every LSN read fall back, which would be a bug in the model rather
            # than a finding about the design.
            r.lag = max(0, r.lag - 1)
            r.catch_up(self.log)
        self.fallbacks += 1
        return self.read_primary(key), "primary (fallback: deadline hit)"


# ---------------------------------------------------------------- the checker

class Checker:
    """Detects violations from the observed trace. Nothing is asserted by hand.

    Two histories, kept apart on purpose:

      my_writes       what THIS session wrote. Only these can break
                      read-your-writes -- somebody else's write being invisible
                      is not a violation of anything.
      version_order   every write anyone made, in order. This is what monotonic
                      reads is checked against: a session that reads version 3
                      and then version 1 has watched time run backwards,
                      whoever wrote them.

    If a scenario claims a violation and the checker disagrees, the output says
    so. That is the point of having a checker rather than a narrator.
    """

    def __init__(self) -> None:
        self.violations: list[str] = []
        self.my_writes: dict[str, str] = {}
        self.version_order: list[tuple[str, str | None]] = []
        self.last_seen_index: dict[str, int] = {}

    def observe_write(self, key: str, value: str, mine: bool = True) -> None:
        if mine:
            self.my_writes[key] = value
        self.version_order.append((key, value))

    def _version_of(self, key: str, value: str | None) -> int:
        """Position of this value in the global write order. -1 means 'before
        any write to this key', which is what a not-yet-replayed read returns."""
        hits = [i for i, (k, v) in enumerate(self.version_order)
                if k == key and v == value]
        return hits[-1] if hits else -1

    def observe_read(self, key: str, value: str | None, where: str) -> None:
        if key in self.my_writes and value != self.my_writes[key]:
            self.violations.append(
                f"read-your-writes: wrote {key}={self.my_writes[key]!r}, "
                f"read {key}={value!r} from {where}")
        idx = self._version_of(key, value)
        if key in self.last_seen_index and idx < self.last_seen_index[key]:
            self.violations.append(
                f"monotonic reads: had seen {key} at version "
                f"{self.last_seen_index[key]}, now reading version {idx} "
                f"({value!r}) from {where}")
        self.last_seen_index[key] = max(idx, self.last_seen_index.get(key, -1))

    def report(self, label: str) -> bool:
        if self.violations:
            print(f"  {label}: {len(self.violations)} VIOLATION(S)")
            for v in self.violations:
                print(f"      {v}")
            return False
        print(f"  {label}: no violations detected")
        return True


def banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def section(title: str) -> None:
    print()
    print("-" * 78)
    print(title)
    print("-" * 78)


# ------------------------------------------------------------- the scenarios

def scenario_read_your_writes() -> None:
    section("1. read-your-writes -- 'I saved it and it is gone'")
    print("  Break it: user writes to the primary, then their next read is routed")
    print("  to a replica that has not replayed the write yet.")
    c = Cluster([Replica("standby", lag=2)])
    ck = Checker()
    c.write("profile:1", "name=old")
    token = c.write("profile:1", "name=NEW")
    ck.observe_write("profile:1", "name=NEW")
    value = c.read_replica(0, "profile:1")
    print(f"      write profile:1 = name=NEW   (lsn {token})")
    print(f"      read  profile:1 -> {value!r} from replica")
    ck.observe_read("profile:1", value, "replica")
    ck.report("  broken")
    print("  The user retypes it. That is the entire bug report you will receive.")

    print()
    print("  Fix A -- sticky primary reads for N seconds after a session's write:")
    c2 = Cluster([Replica("standby", lag=2)])
    ck2 = Checker()
    c2.write("profile:1", "name=old")
    c2.write("profile:1", "name=NEW")
    ck2.observe_write("profile:1", "name=NEW")
    v2 = c2.read_primary("profile:1")
    ck2.observe_read("profile:1", v2, "primary")
    ck2.report("  sticky")
    print(f"      cost: primary reads {c2.primary_reads}, replica reads "
          f"{c2.replica_reads}  <- the replica bought you nothing on this path")


def scenario_monotonic_reads() -> None:
    section("2. monotonic reads -- the value that appears, vanishes, reappears")
    print("  Break it: two replicas at different lag, and consecutive reads from")
    print("  one session land on different ones. Nothing is wrong with either")
    print("  replica; the session is just being bounced by a load balancer.")
    c = Cluster([Replica("fast", lag=0), Replica("slow", lag=3)])
    ck = Checker()
    for v in ("v1", "v2", "v3"):
        c.write("post:9", v)
        # mine=False: a DIFFERENT session wrote these. This reader has written
        # nothing, so read-your-writes cannot be what breaks below.
        ck.observe_write("post:9", v, mine=False)
    for replica, label in ((0, "fast"), (1, "slow"), (0, "fast")):
        v = c.read_replica(replica, "post:9")
        print(f"      read post:9 -> {v!r} from {label}")
        ck.observe_read("post:9", v, label)
    ck.report("  broken")
    print("  Fix: pin the session to one replica, or route by a consistency token.")
    print("  Note what the fix costs -- you have given up free load balancing, and")
    print("  a pinned replica going away is now a user-visible event.")


def scenario_monotonic_writes() -> None:
    section("3. monotonic writes -- your own writes applied out of order")
    print("  Two writes from one session, issued in order, applied in the other")
    print("  order because they took different paths. The final state is the FIRST")
    print("  write's value, and no error was raised anywhere.")
    c = Cluster([Replica("standby", lag=0)])
    print("      session issues:  set cfg=A   then   set cfg=B")
    # Applied in the reverse order. Modelled directly, because the mechanism
    # (two connections, two paths, no ordering between them) is the point and
    # simulating the paths would add nothing.
    c.write("cfg", "B")
    c.write("cfg", "A")
    final = c.read_replica(0, "cfg")
    print(f"      final stored value -> {final!r}")
    if final != "B":
        print("    VIOLATION: monotonic writes -- the later write did not win")
    print("  Fix: a per-session sequence number the storage layer enforces, which")
    print("  is Topic 7's fencing token with a different job description.")


def scenario_writes_follow_reads() -> None:
    section("4. writes-follow-reads -- the reply that appears before the comment")
    print("  A reads a comment, then replies to it. B reads the two keys and gets")
    print("  them from different places -- the reply from a caught-up replica, the")
    print("  comment from a lagging one. B sees an answer to a question that, as")
    print("  far as B can tell, nobody asked.")
    print()
    print("  Note the mechanism, because a single streaming standby cannot do")
    print("  this: WAL is ordered, so one standby replays the comment before the")
    print("  reply or neither. It takes TWO read paths -- two replicas, two")
    print("  shards, a cache beside a database -- and almost every real system")
    print("  has two read paths.")
    c = Cluster([Replica("replica-1", lag=2), Replica("replica-2", lag=0)])
    c.write("comment:1", "is this on?")
    c.write("reply:1", "yes -> comment:1")
    seen_comment = c.read_replica(0, "comment:1")   # lagging replica
    seen_reply = c.read_replica(1, "reply:1")       # caught-up replica
    print()
    print(f"      reader B reads comment:1 from replica-1 -> {seen_comment!r}")
    print(f"      reader B reads reply:1   from replica-2 -> {seen_reply!r}")
    if seen_reply is not None and seen_comment is None:
        print("    VIOLATION: writes-follow-reads -- an effect is visible before")
        print("               the cause it responded to")
    else:
        print("    no violation this run: both keys had been replayed")
    print("  This is the guarantee people forget exists until a forum renders")
    print("  upside down. Causal consistency is the model that provides it, and")
    print("  it is the STRONGEST model that survives a partition.")


def scenario_long_fork() -> None:
    section("5. Long Fork -- the pattern Snapshot Isolation forbids")
    print("  Two writers, two readers, two independent keys. Reader 1 sees x but")
    print("  not y; reader 2 sees y but not x. Neither reader can be explained by")
    print("  ANY single serial order of the two writes, which is what SI promises.")
    print()
    print("  Mechanism, for a primary plus an async standby: lock order and WAL")
    print("  order can differ, so the primary and a reader endpoint can disagree")
    print("  about apparent transaction order. Jepsen reports exactly this for")
    print("  multi-AZ Amazon RDS for PostgreSQL in every version from 13.15 to")
    print("  17.4 -- jepsen.io/analyses/amazon-rds-for-postgresql-17.4, April 2025.")
    print()
    c = Cluster([Replica("endpoint-1", lag=0), Replica("endpoint-2", lag=0)])
    c.write("x", "1")
    c.write("y", "1")
    # Two independent writers commit x=1 and y=1. The two reader endpoints then
    # disagree about which one happened first. Set the replica state directly:
    # "which endpoint has applied which write" IS the observation, and deriving
    # it from a lag counter would only hide it behind arithmetic.
    c.replicas[0].store = {"y": "1"}   # endpoint-1: y only
    c.replicas[1].store = {"x": "1"}   # endpoint-2: x only
    r1 = (c.replicas[0].read("x"), c.replicas[0].read("y"))
    r2 = (c.replicas[1].read("x"), c.replicas[1].read("y"))
    print(f"      reader 1 (endpoint-1) sees x={r1[0]!r} y={r1[1]!r}")
    print(f"      reader 2 (endpoint-2) sees x={r2[0]!r} y={r2[1]!r}")
    serial_orders = {
        "x then y": [("x", "1"), ("y", "1")],
        "y then x": [("y", "1"), ("x", "1")],
    }
    explained = []
    for name, order in serial_orders.items():
        # A reader observing a prefix of a serial order is fine. A reader that
        # skips an earlier write and sees a later one is not.
        def prefix_ok(obs: tuple[str | None, str | None]) -> bool:
            seen = {"x": obs[0] is not None, "y": obs[1] is not None}
            gap = False
            for key, _ in order:
                if not seen[key]:
                    gap = True
                elif gap:
                    return False
            return True
        if prefix_ok(r1) and prefix_ok(r2):
            explained.append(name)
    if not explained:
        print("    VIOLATION: no single serial order of the two writes explains")
        print("               both readers. That is Long Fork, and SI forbids it.")
    else:
        print(f"    explained by: {explained} -- not a fork this run")
    print()
    print("  Consequence worth carrying: set REPEATABLE READ and read from a")
    print("  reader endpoint, and what you actually have is closer to Parallel")
    print("  Snapshot Isolation than to the SI the isolation level names.")


def scenario_lsn_token() -> None:
    section("6. Fix B -- the LSN token, and what it costs")
    print("  Capture pg_current_wal_insert_lsn() at commit, hand it to the client")
    print("  as an opaque token, and on the read path poll")
    print("  pg_last_wal_replay_lsn() until the replica is past it -- with a")
    print("  DEADLINE, after which you fall back to the primary.")
    print()
    print("  PostgreSQL 19 replaces the hand-rolled loop with WAIT FOR LSN")
    print("  (Beta 3 as of 2026-08-13). pg_wal_replay_wait() was proposed for 17,")
    print("  added, then reverted: the procedure holds a snapshot, which blocks")
    print("  the very replay it is waiting on. On the 18 line you do it by hand.")

    for lag, label in ((1, "replica nearly caught up"), (6, "replica badly behind")):
        c = Cluster([Replica("standby", lag=lag)])
        ck = Checker()
        for i in range(8):
            c.write("order:7", f"state-{i}")
        token = c.write("order:7", "state-FINAL")
        ck.observe_write("order:7", "state-FINAL")
        value, where = c.read_lsn_token(0, "order:7", token)
        ck.observe_read("order:7", value, where)
        print()
        print(f"  lag {lag} ops ({label}):")
        print(f"      token {token}, replica replayed to lsn {c.replicas[0].lsn}")
        print(f"      read  -> {value!r} from {where}")
        print(f"      polls {c.poll_iterations}, fallbacks to primary {c.fallbacks}")
        ck.report("      correctness")
    print()
    print("  Both configurations are CORRECT. They differ in cost, and the cost is")
    print("  the number the record table asks for: poll iterations on the read")
    print("  path, and the fraction of reads that end up on the primary anyway.")
    print("  Under enough lag the LSN token degrades into Fix A with extra steps --")
    print("  which is a fine outcome as long as you knew it would.")


def main() -> int:
    banner("Layer 4 Topic 4 -- session guarantees, as a vocabulary check")
    print("  THIS IS A SIMULATION. Two in-memory replicas, lag counted in")
    print("  operations. It proves nothing about Postgres and measures nothing.")
    print("  The measurement needs a real streaming standby and is blocked while")
    print("  the Docker daemon is down -- python3 ../lab/local/check_env.py.")
    print()
    print("  What it is for: given an observation, naming the guarantee it broke.")

    scenario_read_your_writes()
    scenario_monotonic_reads()
    scenario_monotonic_writes()
    scenario_writes_follow_reads()
    scenario_long_fork()
    scenario_lsn_token()

    section("the ladder, and what each rung costs on the READ path")
    rows = [
        ("linearizable", "one copy; real-time order respected",
         "a quorum round trip, even for reads"),
        ("sequential", "one agreed order, not real-time order",
         "none; everyone stale together"),
        ("causal", "cause before effect; concurrent may differ",
         "metadata per op; STRONGEST under partition"),
        ("eventual", "if writes stop, replicas converge",
         "none, and it promises nothing about now"),
    ]
    print(f"  {'model':<15}{'what a reader may observe':<45}{'read-path cost'}")
    for name, promise, cost in rows:
        print(f"  {name:<15}{promise:<45}{cost}")
    print()
    print("  'Eventually consistent' is a liveness promise and nothing more. It is")
    print("  a description of a failure mode, not a guarantee -- which is why the")
    print("  four session guarantees above, all of which sit inside causal, are")
    print("  what actually generates and closes bug reports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
