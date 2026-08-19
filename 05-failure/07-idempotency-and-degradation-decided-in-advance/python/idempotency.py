"""
Layer 5 - Topic 7: idempotency, and degradation decided in advance (Python).

Runs the whole of the topic's experiment against a REAL local Postgres, with no
containers involved: the naive double-charge, the atomic insert-on-unique-
constraint that makes a retry safe, the ambiguous result where the client never
learns it succeeded, a crash between the claim and the work, the fingerprint
check, and a degradation matrix that is a table you wrote in advance rather than
an argument you have at 3am.

The mechanism is in Postgres -- a unique index and `ON CONFLICT` under READ
COMMITTED -- not in the runtime. What is Python-specific is the driver-level
trap, and it gets its own section rather than a sentence: an `IntegrityError`
leaves a SQLAlchemy `Session` in a failed state, so you MUST roll back before
you can read the row that beat you. Code that forgets this fails only under
concurrency, which is to say only in production, and the traceback it produces
(`PendingRollbackError`) names a different problem than the one you have.
Section 3 below reproduces that traceback deliberately.

WHAT THIS DEMONSTRATES, IN ORDER

  1. Setup      Two key tables: one with a UNIQUE index on `key`, one without.
                Same SQL shape, one constraint apart.
  2. The race   Eight scenarios, each 50 concurrent requests released together
                by a barrier, sharing ONE idempotency key:
                  naive, sequential            - looks correct, proves nothing
                  naive, 50 concurrent         - the double charge
                  naive, 50 concurrent, pool=1 - the same bug, hidden by a
                                                 SMALLER resource limit
                  correct, claim + execute     - unique index arbitrates
                  correct, single transaction  - the same correctness with a
                                                 different failure surface
                  correct + lost responses     - the realistic ambiguous case
                  correct + crash mid-request  - orphaned claims, and the TTL
                  fingerprint mismatch         - 422, not a silent wrong replay
  3. The Python trap, reproduced and then fixed.
  4. Degradation decided in advance: the matrix, then a kill switch actually
     being flipped mid-run, then the row that is not a kill switch at all.

WHAT TO LOOK FOR IN THE OUTPUT

  * `charge_rows` in the naive concurrent row. Anything above 1 is money.
  * `charge_rows` in the `pool=1` row against the row above it. A pool of one
    serialises everything and hides the race perfectly -- a smaller limit
    concealing a bug, which is the most dangerous kind of green test.
  * `409s` in `correct/claim+execute` against `correct/single-txn`. Both are
    correct. One makes the loser retry, the other makes the loser wait, and
    `loser_p99` is what waiting costs.
  * `orphaned` in the crash rows, before and after the TTL expires. That number
    is the answer to "was the TTL thought through or just typed".
  * The degradation section's goodput before and after the flip -- and then the
    last line, which is a row that cannot be flipped without a deploy and is
    therefore not a kill switch no matter what the matrix calls it.

REQUIREMENTS
    A local Postgres accepting connections (`pg_isready`). This program creates
    the `failure_lab` database if it is missing and owns the tables it makes
    inside it; `dropdb failure_lab` when you are done with the layer.
        pip install -r requirements.txt

RUN
    python3 idempotency.py

Takes about ten seconds. Takes no arguments.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, PendingRollbackError
from sqlalchemy.orm import Session

# ------------------------------------------------------------------ config

DB_NAME = os.environ.get("FAILURE_LAB_DB", "failure_lab")
ADMIN_URL = "postgresql+psycopg:///postgres"
DB_URL = f"postgresql+psycopg:///{DB_NAME}"

CONCURRENCY = 50          # the README's number: 50 requests sharing one key
HOLD_MS = 25              # widens the window; it does not create the race
TTL_S = 60.0              # normal claim TTL
CRASH_TTL_S = 2.0         # short TTL for the crash scenario, so it is watchable
AMBIGUOUS_KEYS = 20       # clients in the lost-response scenario
AMBIGUOUS_LOSS_P = 0.5
MAX_CLIENT_ATTEMPTS = 5

MS = 1000.0


def now_ms() -> float:
    return time.perf_counter() * MS


def pct(vals: list[float], q: float) -> float:
    """Nearest-rank percentile."""
    if not vals:
        return float("nan")
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(-(-q * len(s) // 1)) - 1))
    return s[idx]


def fingerprint(body: dict) -> str:
    """The request body, canonicalised and hashed.

    Same key with a different body is a client bug and must be a 422, not a
    silent replay of the wrong answer. Sorting the keys is what makes the hash
    a statement about the request rather than about JSON serialisation order.
    """
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# ------------------------------------------------------------------ schema

META = sa.MetaData()

charges = sa.Table(
    "charges", META,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("idem_key", sa.Text, nullable=False),
    sa.Column("amount_cents", sa.Integer, nullable=False),
    sa.Column("currency", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

# The correct table. The PRIMARY KEY on `key` is the unique index, and the
# unique index is the entire mechanism -- not the SELECT, not the application
# logic, not the ORM. Everything else in this file is plumbing around it.
keys = sa.Table(
    "idempotency_keys", META,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("fingerprint", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("response", sa.JSON),
    sa.Column("claimed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
)

# The naive table. Byte-for-byte the same columns; `key` is NOT unique and is
# NOT the primary key. That single difference is what the first three scenarios
# below are measuring.
keys_naive = sa.Table(
    "idempotency_keys_naive", META,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("key", sa.Text, nullable=False),
    sa.Column("fingerprint", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("response", sa.JSON),
    sa.Column("claimed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
)


def ensure_database() -> None:
    admin = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": DB_NAME}
            ).first()
            if not exists:
                conn.execute(sa.text(f'CREATE DATABASE "{DB_NAME}"'))
                print(f"  created database {DB_NAME}")
    finally:
        admin.dispose()


def reset_schema(engine: sa.Engine) -> None:
    """Drop and recreate, so every count printed below is this run's own."""
    with engine.begin() as conn:
        META.drop_all(conn)
        META.create_all(conn)


# ------------------------------------------------------------------ server

@dataclass
class Response:
    status: int
    body: dict | None = None
    replayed: bool = False


class Server:
    """The /charge endpoint, in the four implementations the topic compares."""

    def __init__(self, engine: sa.Engine, hold_ms: float = HOLD_MS,
                 ttl_s: float = TTL_S) -> None:
        self.engine = engine
        self.hold_ms = hold_ms
        self.ttl_s = ttl_s
        self.crash_after_claim = threading.Event()

    # -- the work itself, identical in every implementation -----------------

    def _do_work(self, conn: sa.Connection, key: str, body: dict) -> dict:
        """The side effect. In real life a card is charged here."""
        time.sleep(self.hold_ms / MS)
        charge_id = conn.execute(
            sa.insert(charges)
            .values(idem_key=key, amount_cents=body["amount_cents"],
                    currency=body["currency"])
            .returning(charges.c.id)
        ).scalar_one()
        return {"charge_id": charge_id, "amount_cents": body["amount_cents"],
                "currency": body["currency"], "status": "succeeded"}

    # -- 1. naive: SELECT, then INSERT, no unique index ---------------------

    def charge_naive(self, key: str, body: dict) -> Response:
        """Wrong at READ COMMITTED, and wrong in a way that reads as careful.

        Two concurrent transactions both SELECT, both see no row, and both
        proceed. Nothing in the isolation level serialises them: READ COMMITTED
        gives each statement a fresh snapshot of *committed* data, and neither
        transaction has committed anything the other can see. The unique index
        is the only thing that would have, and this table does not have one.
        """
        fp = fingerprint(body)
        with self.engine.begin() as conn:
            row = conn.execute(
                sa.select(keys_naive).where(keys_naive.c.key == key)
            ).mappings().first()
            if row is not None and row["state"] == "completed":
                return Response(200, row["response"], replayed=True)
            if row is None:
                conn.execute(sa.insert(keys_naive).values(
                    key=key, fingerprint=fp, state="in_progress",
                    expires_at=sa.func.now() + sa.text(
                        f"interval '{self.ttl_s} seconds'"),
                ))
            resp = self._do_work(conn, key, body)
            conn.execute(
                sa.update(keys_naive)
                .where(keys_naive.c.key == key)
                .values(state="completed", response=resp)
            )
            return Response(200, resp)

    # -- 2. correct: claim in its own transaction, then execute -------------

    def charge_correct(self, key: str, body: dict) -> Response:
        """Claim, then execute. Two transactions, on purpose.

        The claim commits before the work starts, which is what lets a
        concurrent request find `in_progress` and answer 409 immediately
        instead of holding a connection open waiting for someone else's card
        charge. The price is the crash window: if the executor dies between the
        two transactions, the claim outlives it and blocks every retry until
        the TTL expires. Scenario 7 kills a request in exactly that window.
        """
        fp = fingerprint(body)
        ttl = sa.func.now() + sa.text(f"interval '{self.ttl_s} seconds'")

        # The whole mechanism, in one statement. The DO UPDATE arm is not a
        # convenience: it is how a claim whose holder died gets taken over, and
        # it fires only for a stale in_progress row with a matching body.
        stmt = pg_insert(keys).values(
            key=key, fingerprint=fp, state="in_progress", expires_at=ttl,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[keys.c.key],
            set_={"state": "in_progress", "claimed_at": sa.func.now(),
                  "expires_at": ttl},
            where=sa.and_(
                keys.c.state == "in_progress",
                keys.c.expires_at < sa.func.now(),
                keys.c.fingerprint == stmt.excluded.fingerprint,
            ),
        ).returning(keys.c.key)

        with self.engine.begin() as conn:
            won = conn.execute(stmt).first() is not None

        if not won:
            return self._replay_or_conflict(key, fp)

        if self.crash_after_claim.is_set():
            # The claim is committed and this process is about to stop existing.
            # Nothing rolls back, because there is no open transaction to roll
            # back: that is precisely why the row is now an orphan.
            raise RuntimeError("simulated crash after claim, before work")

        # The side effect and the stored response, in ONE transaction. If these
        # were two, a crash between them leaves a charge nobody can replay --
        # which is worse than the orphan above, because the money moved.
        with self.engine.begin() as conn:
            resp = self._do_work(conn, key, body)
            conn.execute(sa.update(keys).where(keys.c.key == key)
                         .values(state="completed", response=resp))
        return Response(200, resp)

    def _replay_or_conflict(self, key: str, fp: str) -> Response:
        with self.engine.connect() as conn:
            row = conn.execute(
                sa.select(keys).where(keys.c.key == key)
            ).mappings().first()
        if row is None:
            return Response(409)
        if row["fingerprint"] != fp:
            return Response(422)
        if row["state"] == "completed":
            return Response(200, row["response"], replayed=True)
        return Response(409)

    # -- 3. correct, everything in one transaction --------------------------

    def charge_correct_single_txn(self, key: str, body: dict) -> Response:
        """Equally correct, and it never produces an orphan -- because there is
        no window between the claim and the work for a crash to land in.

        What it produces instead is waiting. A loser's `INSERT ... ON CONFLICT`
        blocks on the winner's uncommitted tuple until the winner commits, so
        every concurrent duplicate holds a connection for the full duration of
        the work. Read `loser_p99` in the table: that is the cost, and at a
        large enough duplicate rate it is a pool exhaustion (topic 5) wearing
        an idempotency costume.
        """
        fp = fingerprint(body)
        ttl = sa.func.now() + sa.text(f"interval '{self.ttl_s} seconds'")
        with self.engine.begin() as conn:
            won = conn.execute(
                pg_insert(keys)
                .values(key=key, fingerprint=fp, state="in_progress", expires_at=ttl)
                .on_conflict_do_nothing(index_elements=[keys.c.key])
                .returning(keys.c.key)
            ).first() is not None
            if won:
                resp = self._do_work(conn, key, body)
                conn.execute(sa.update(keys).where(keys.c.key == key)
                             .values(state="completed", response=resp))
                return Response(200, resp)
            # The INSERT above already waited for the winner to commit, so the
            # row is visible to this statement: READ COMMITTED takes a fresh
            # snapshot per statement, which is the same property that made the
            # naive version wrong and makes this one work.
            row = conn.execute(
                sa.select(keys).where(keys.c.key == key)
            ).mappings().first()
            if row["fingerprint"] != fp:
                return Response(422)
            if row["state"] == "completed":
                return Response(200, row["response"], replayed=True)
            return Response(409)


# ------------------------------------------------------------------ client

@dataclass
class Result:
    statuses: list[int] = field(default_factory=list)
    latencies: list[float] = field(default_factory=list)
    bodies: list[str] = field(default_factory=list)
    errors: int = 0
    attempts: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, status: int, latency_ms: float, body: dict | None) -> None:
        with self.lock:
            self.statuses.append(status)
            self.latencies.append(latency_ms)
            self.attempts += 1
            if body is not None:
                self.bodies.append(json.dumps(body, sort_keys=True))


def fire_together(fn, n: int, workers: int) -> Result:
    """Release `n` callers at the same instant.

    The barrier is not decoration. Without it the thread pool ramps up, the
    first request finishes before the last one starts, and the naive version
    passes -- which is the top entry on the README's list of ways this
    experiment breaks rather than the prediction being wrong.
    """
    res = Result()
    barrier = threading.Barrier(n)

    def one(i: int) -> None:
        barrier.wait()
        t0 = now_ms()
        try:
            r = fn(i)
            res.record(r.status, now_ms() - t0, r.body)
        except Exception:
            with res.lock:
                res.errors += 1
                res.attempts += 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, range(n)))
    return res


# ------------------------------------------------------------------ counts

def counts(engine: sa.Engine, key: str | None = None) -> dict:
    with engine.connect() as conn:
        where = (charges.c.idem_key == key) if key else sa.true()
        n_charges = conn.execute(
            sa.select(sa.func.count()).select_from(charges).where(where)).scalar_one()
        orphaned = conn.execute(sa.text(
            "SELECT count(*) FROM idempotency_keys "
            "WHERE state = 'in_progress' AND expires_at > now()")).scalar_one()
        expired = conn.execute(sa.text(
            "SELECT count(*) FROM idempotency_keys "
            "WHERE state = 'in_progress' AND expires_at <= now()")).scalar_one()
    return {"charges": n_charges, "orphaned": orphaned, "expired": expired}


def report(mode: str, res: Result, c: dict, extra: str = "") -> None:
    distinct = len(set(res.bodies))
    n409 = sum(1 for s in res.statuses if s == 409)
    n422 = sum(1 for s in res.statuses if s == 422)
    print(f"  mode={mode:<32} charge_rows={c['charges']:<4} "
          f"distinct_responses={distinct:<4} 409s={n409:<4} 422s={n422:<4} "
          f"orphaned_in_progress={c['orphaned']:<4} attempts={res.attempts:<4}"
          f"{extra}")


def rule(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ------------------------------------------------------- the Python trap

def python_trap(engine: sa.Engine) -> None:
    """`IntegrityError` poisons the Session. Reproduced, then fixed.

    This is not a style point. The wrong version raises about the PREVIOUS
    statement, so the traceback you take to the incident review names a
    different problem than the one you have -- and it only ever appears when
    two requests raced, which is only ever in production.

    Which exception you get depends on the path, and this function prints the
    one this machine actually produced rather than the one the folklore names.
    Through the ORM's autoflush you get SQLAlchemy's own `PendingRollbackError`
    ("Can't reconnect until invalid transaction is rolled back"); through a
    direct `Session.execute` you get Postgres answering
    `InFailedSqlTransaction` -- "current transaction is aborted, commands
    ignored until end of transaction block". Same condition, same fix, and both
    messages point at the failed insert rather than at the fact that somebody
    else owns this key.
    """
    key = f"trap-{uuid.uuid4()}"
    fp = fingerprint({"amount_cents": 100, "currency": "usd"})
    expires = sa.text("now() + interval '60 seconds'")

    with engine.begin() as conn:
        conn.execute(sa.insert(keys).values(
            key=key, fingerprint=fp, state="completed",
            response={"charge_id": -1}, expires_at=expires))

    def bare_insert(session: Session) -> None:
        session.execute(sa.insert(keys).values(
            key=key, fingerprint=fp, state="in_progress", expires_at=expires))
        session.flush()

    print("  (a) catch IntegrityError, then read the winning row WITHOUT rolling back")
    with Session(engine) as s:
        try:
            bare_insert(s)
        except IntegrityError as e:
            print(f"      insert raised {type(e).__name__}: "
                  f"{type(e.orig).__name__} -- correct so far")
            try:
                s.execute(sa.select(keys).where(keys.c.key == key)).first()
                print("      read succeeded -- this SQLAlchemy/driver combination does")
                print("      not poison the session here; check before relying on it")
            except (PendingRollbackError, sa.exc.SQLAlchemyError) as e2:
                first = str(e2).splitlines()[0]
                print(f"      read raised {type(e2).__name__}: {first[:104]}")
                print("      ^ a message about the previous statement, for a problem that")
                print("        is actually 'somebody else owns this key'. This is the bug.")

    print("  (b) the same code with s.rollback() before the read")
    with Session(engine) as s:
        try:
            bare_insert(s)
        except IntegrityError:
            s.rollback()
            row = s.execute(sa.select(keys).where(keys.c.key == key)).mappings().first()
            print(f"      read succeeded: state={row['state']} "
                  f"response={json.dumps(row['response'])}")
            print("      ^ this is the replay path, and one line of rollback is the")
            print("        difference between reaching it and a misleading traceback.")

    print("  (c) ON CONFLICT DO NOTHING never raises at all, which is why the")
    print("      production path in this file uses it -- the trap above is the")
    print("      shape of the code people write BEFORE they learn about it.")
    with engine.begin() as conn:
        won = conn.execute(
            pg_insert(keys).values(key=key, fingerprint=fp, state="in_progress",
                                   expires_at=expires)
            .on_conflict_do_nothing(index_elements=[keys.c.key])
            .returning(keys.c.key)).first()
        print(f"      won the race: {won is not None}   (no exception, no rollback)")

    with engine.begin() as conn:
        conn.execute(sa.delete(keys).where(keys.c.key == key))


# ------------------------------------------------- degradation, in advance

DEGRADATION_MATRIX = [
    # (tier, feature, what "off" looks like to a user, kill switch, who flips, blast radius)
    (0, "authorise + capture", "nothing works; this is the product",
     "none - never shed", "nobody", "total"),
    (1, "3-D Secure step-up", "non-authenticated auth; issuer may decline more",
     "flag: risk.stepup", "on-call", "higher decline rate"),
    (2, "fraud enrichment", "cached features only; wider manual review queue",
     "flag: risk.enrich", "on-call", "review backlog"),
    (3, "receipt email", "queued, sent late; nothing is lost",
     "flag: notify.receipt", "on-call", "support tickets"),
    (3, "analytics fan-out", "dashboards go stale for the duration",
     "flag: analytics.emit", "on-call", "reporting only"),
    (2, "currency rate refresh", "last-known rate, capped staleness",
     "config: fx.freeze", "on-call", "small FX drift"),
]


class Flags:
    """Read at request time, never at import time. That is the whole design."""

    def __init__(self) -> None:
        self._v = {"risk.enrich": True, "notify.receipt": True, "analytics.emit": True}
        self._lock = threading.Lock()

    def get(self, name: str) -> bool:
        with self._lock:
            return self._v.get(name, True)

    def set(self, name: str, value: bool) -> None:
        with self._lock:
            self._v[name] = value


# A flag read once, at import time, into a module constant. It is in the matrix
# and it has an owner and it looks exactly like the others. It is not a kill
# switch: flipping it changes nothing until someone deploys.
BAKED_IN_ENRICH_ENABLED = True


def degradation_demo(flags: Flags) -> None:
    """A dependency is down. The pre-decided matrix says which tiers go first."""
    dep_up = threading.Event()

    def enrich() -> None:
        # The sick dependency: 60ms and then a failure, for as long as it is down.
        time.sleep(0.06)
        if not dep_up.is_set():
            raise RuntimeError("fraud-enrichment dependency is down")

    def handle_charge() -> bool:
        """Returns True if the core operation succeeded."""
        if flags.get("risk.enrich"):
            try:
                enrich()
            except RuntimeError:
                return False        # tier 2 failing takes tier 0 down with it
        if flags.get("notify.receipt"):
            time.sleep(0.002)
        return True

    def measure(n: int = 120) -> tuple[float, float]:
        t0 = now_ms()
        ok = sum(1 for _ in range(n) if handle_charge())
        el = (now_ms() - t0) / MS
        return ok / n * 100.0, n / el

    print("  The matrix, written before the incident:")
    print(f"    {'tier':<5}{'feature':<24}{'off looks like':<50}"
          f"{'kill switch':<22}{'blast radius'}")
    for tier, feature, off, switch, _who, blast in sorted(DEGRADATION_MATRIX):
        print(f"    {tier:<5}{feature:<24}{off:<50}{switch:<22}{blast}")
    print()
    print("  Shed order follows the tier column, which is business importance --")
    print("  not code structure, and not whatever is easiest to switch off.")
    print()

    ok, rps = measure()
    print(f"  dependency down, matrix not applied:  success={ok:5.1f}%  goodput={rps:6.1f}/s")
    flags.set("risk.enrich", False)     # tier 2 first
    flags.set("analytics.emit", False)  # tier 3
    ok, rps = measure()
    print(f"  after flipping risk.enrich + analytics.emit (no deploy, no restart):")
    print(f"                                        success={ok:5.1f}%  goodput={rps:6.1f}/s")
    print()
    print(f"  BAKED_IN_ENRICH_ENABLED is still {BAKED_IN_ENRICH_ENABLED}. It is in the matrix,")
    print("  it has an owner, and it cannot be changed without a deploy -- so it is")
    print("  not a kill switch. Any row like it is a plan, not a control.")
    dep_up.set()


# -------------------------------------------------------------------- main

def main() -> None:
    rule("Layer 5 - Topic 7: idempotency and degradation, decided in advance (Python)")
    ensure_database()
    engine = sa.create_engine(DB_URL, pool_size=CONCURRENCY, max_overflow=0,
                              pool_pre_ping=True)
    reset_schema(engine)
    print(f"  database          {DB_NAME} (local, no containers)")
    print(f"  concurrency       {CONCURRENCY} requests sharing ONE idempotency key")
    print(f"  work window       {HOLD_MS:.0f} ms inside the executing transaction")
    print("  isolation         READ COMMITTED (Postgres' default; not changed anywhere)")
    body = {"amount_cents": 4200, "currency": "usd"}

    # ---------------------------------------------------------- scenarios
    rule("THE RACE: 50 requests, one key, released together")
    srv = Server(engine)

    # 1. naive, sequential.
    key = f"naive-seq-{uuid.uuid4()}"
    res = Result()
    for _ in range(CONCURRENCY):
        t0 = now_ms()
        r = srv.charge_naive(key, body)
        res.record(r.status, now_ms() - t0, r.body)
    report("naive / sequential", res, counts(engine, key),
           "   <- correct, and it proves nothing")

    # 2. naive, concurrent.
    key = f"naive-conc-{uuid.uuid4()}"
    res = fire_together(lambda i: srv.charge_naive(key, body), CONCURRENCY, CONCURRENCY)
    report("naive / 50 concurrent", res, counts(engine, key),
           "   <- every row is a charge to a real card")

    # 3. naive, concurrent, but with a pool of one.
    small = sa.create_engine(DB_URL, pool_size=1, max_overflow=0)
    srv_small = Server(small)
    key = f"naive-pool1-{uuid.uuid4()}"
    res = fire_together(lambda i: srv_small.charge_naive(key, body),
                        CONCURRENCY, CONCURRENCY)
    report("naive / 50 concurrent / pool=1", res, counts(engine, key),
           "   <- same bug, hidden by a SMALLER limit")
    small.dispose()

    # 4. correct: claim then execute.
    key = f"correct-{uuid.uuid4()}"
    res = fire_together(lambda i: srv.charge_correct(key, body),
                        CONCURRENCY, CONCURRENCY)
    lp = pct([l for s, l in zip(res.statuses, res.latencies) if s != 200], 0.99)
    report("correct / claim + execute", res, counts(engine, key),
           f"   loser_p99={lp:6.1f}ms")

    # 5. correct: one transaction for everything.
    key = f"correct1txn-{uuid.uuid4()}"
    res = fire_together(lambda i: srv.charge_correct_single_txn(key, body),
                        CONCURRENCY, CONCURRENCY)
    replay_lat = [l for s, l in zip(res.statuses, res.latencies) if s == 200]
    lp = pct(sorted(replay_lat)[1:], 0.99) if len(replay_lat) > 1 else float("nan")
    report("correct / single transaction", res, counts(engine, key),
           f"   loser_p99={lp:6.1f}ms  <- they waited instead of 409ing")

    # ------------------------------------------------- the ambiguous result
    rule("THE AMBIGUOUS RESULT: the server succeeded, the client never heard")
    print("  Each client retries its own key up to "
          f"{MAX_CLIENT_ATTEMPTS} times; every response has a "
          f"{AMBIGUOUS_LOSS_P:.0%} chance of being lost on the way back.")
    print("  The client cannot tell 'did not happen' from 'happened, answer lost'.")
    print("  That is not a bug to fix; it is the situation. Idempotency is what")
    print("  makes the only available action -- retry -- safe.")
    print()
    rnd = __import__("random").Random(20260819)
    before = counts(engine)["charges"]
    amb = Result()
    amb_keys = [f"amb-{i}-{uuid.uuid4()}" for i in range(AMBIGUOUS_KEYS)]

    def ambiguous_client(i: int) -> Response:
        k = amb_keys[i]
        for _attempt in range(MAX_CLIENT_ATTEMPTS):
            with amb.lock:
                amb.attempts += 1
            r = srv.charge_correct(k, body)
            if r.status == 409:
                time.sleep(0.02)
                continue
            if rnd.random() < AMBIGUOUS_LOSS_P:
                continue        # the response is lost; the charge already happened
            return r
        return Response(504)

    res = fire_together(ambiguous_client, AMBIGUOUS_KEYS, AMBIGUOUS_KEYS)
    res.attempts = amb.attempts
    after = counts(engine)
    delta = after["charges"] - before
    ok = "PASS" if delta == AMBIGUOUS_KEYS else "FAIL"
    report("correct + retries + lost responses", res,
           {"charges": delta, "orphaned": after["orphaned"]},
           f"   distinct keys={AMBIGUOUS_KEYS}  [{ok}]")

    # ------------------------------------------------------- the crash test
    rule("THE CRASH TEST: dying between the claim and the work")
    crash_srv = Server(engine, ttl_s=CRASH_TTL_S)
    key = f"crash-{uuid.uuid4()}"
    crash_srv.crash_after_claim.set()
    try:
        crash_srv.charge_correct(key, body)
    except RuntimeError as e:
        print(f"  executor died: {e}")
    crash_srv.crash_after_claim.clear()

    res = fire_together(lambda i: crash_srv.charge_correct(key, body), 5, 5)
    c = counts(engine, key)
    report("correct + crash, TTL not yet expired", res, c,
           "   <- every retry blocked by a dead holder's claim")
    print(f"  the claim's TTL is {CRASH_TTL_S:.0f}s. Waiting it out...")
    time.sleep(CRASH_TTL_S + 0.3)
    res = fire_together(lambda i: crash_srv.charge_correct(key, body), 5, 5)
    c = counts(engine, key)
    report("correct + crash, after TTL expiry", res, c,
           "   <- reclaimed by ON CONFLICT DO UPDATE, still one charge")
    print()
    print("  The TTL is the only thing that unblocks a claim whose holder is gone, so")
    print("  the client's retry window must be SHORTER than the retention, or the")
    print("  guarantee evaporates at exactly the moment it is needed.")

    # -------------------------------------------------------- fingerprints
    rule("THE FINGERPRINT TEST: same key, different body")
    key = f"fp-{uuid.uuid4()}"
    first = srv.charge_correct(key, body)
    other = {"amount_cents": 999_00, "currency": "usd"}
    second = srv.charge_correct(key, other)
    c = counts(engine, key)
    print(f"  first  request  amount={body['amount_cents']:>7}  -> {first.status}  "
          f"{json.dumps(first.body)}")
    print(f"  second request  amount={other['amount_cents']:>7}  -> {second.status}  "
          f"{'(no body)' if second.body is None else json.dumps(second.body)}")
    print(f"  charge rows for this key: {c['charges']}   "
          f"[{'PASS' if second.status == 422 and c['charges'] == 1 else 'FAIL'}]")
    print("  Without the fingerprint the second request replays the FIRST answer, so")
    print("  a client that reused a key by accident is told its $999 charge succeeded.")

    # ------------------------------------------------------ the Python trap
    rule("THE PYTHON TRAP: IntegrityError poisons the Session")
    python_trap(engine)

    # ------------------------------------------------------- degradation
    rule("DEGRADATION DECIDED IN ADVANCE")
    degradation_demo(Flags())

    engine.dispose()
    print()


if __name__ == "__main__":
    main()
