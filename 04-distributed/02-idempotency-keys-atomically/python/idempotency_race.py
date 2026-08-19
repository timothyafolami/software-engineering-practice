"""
Layer 4 Topic 2 -- three idempotency handlers under a real concurrent duplicate.

WHAT THIS DEMONSTRATES: the same endpoint written three ways, each hit by C
genuinely simultaneous requests carrying the SAME idempotency key, on C separate
Postgres connections released together by a barrier.

  IMPL A  check-then-insert   SELECT, and if absent INSERT and charge.
  IMPL B  on-conflict         INSERT ... ON CONFLICT DO NOTHING, three states,
                              fingerprint check, stored-response replay, and the
                              effect in the SAME transaction as the key row.
  IMPL C  advisory lock       pg_advisory_xact_lock(hashtext(key)), then check
                              and act inside the lock.

WHAT TO LOOK FOR IN THE OUTPUT: the DUPLICATE CHARGES line, and then the latency
block underneath it. Correctness is one number; the price of the design is the
*second* latency column -- what a losing (duplicate) request pays while the
winner's transaction is still open. B converts duplicates into latency, and that
belongs next to the correctness result rather than in a footnote.

DELIBERATE: charges has NO unique index on idempotency_key. In production that
index is your last line of defence and should outlive this experiment; here it
would make implementation A look correct, which is exactly the "broken
experiment, not wrong prediction" trap the README warns about. The program
prints this fact every run so it can never be a silent assumption.

  python3 python/idempotency_race.py --impl A --keys 200 --concurrency 5
  python3 python/idempotency_race.py --impl B --keys 200 --concurrency 5
  python3 python/idempotency_race.py --impl C --keys 200 --concurrency 5
  psql -d sep_lab_04_dist -f sql/topic2_assert.sql
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import uuid

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"),
)
import lab_db  # noqa: E402  - after the sys.path fix, on purpose

import psycopg  # noqa: E402
from psycopg import errors as pg_errors  # noqa: E402

TENANT = "acme"

# uuidv7() is a Postgres 18 function and this fallback runs against whatever is
# listening locally, which on this machine is 17.5. See lab/README.md: v4 keys
# scatter B-tree inserts, v7 keys append, and this table takes an insert on every
# request -- so do not read anything into insert throughput from a fallback run.
DDL = """
CREATE TABLE IF NOT EXISTS idempotency_keys (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     text        NOT NULL,
    key           text        NOT NULL,
    fingerprint   text        NOT NULL,
    state         text        NOT NULL
                  CHECK (state IN ('in_flight', 'succeeded', 'failed_permanently')),
    response      jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL DEFAULT now() + interval '24 hours',
    UNIQUE (tenant_id, key)
);

CREATE TABLE IF NOT EXISTS charges (
    id              bigserial PRIMARY KEY,
    run_id          text        NOT NULL,
    impl            text        NOT NULL,
    tenant_id       text        NOT NULL,
    idempotency_key text        NOT NULL,
    amount_cents    integer     NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
    -- NO UNIQUE (tenant_id, idempotency_key). On purpose. See the header.
);
CREATE INDEX IF NOT EXISTS charges_run_idx ON charges (run_id);
"""


def fingerprint(body: dict) -> str:
    """Hash of method + path + normalised body. Same key, different body -> 422."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"POST /payments {canonical}".encode()).hexdigest()


# ------------------------------------------------------------------ the handlers
# Each returns (status, note). status is what the HTTP layer would send.
# `hold_ms` is a deliberate delay inside the winner's transaction, applied
# identically in all three implementations. It does not change any of them; it
# widens a window that is otherwise microseconds wide so the race is observable.
# The README calls this out explicitly -- it is not cheating, it is how you watch
# a race you would otherwise only meet in production.


def handle_a(conn: psycopg.Connection, run_id: str, key: str, body: dict,
             hold_ms: int) -> tuple[int, str]:
    """A -- check-then-insert. The intuitive handler, and the wrong one.

    Note the transaction structure, because it is the whole bug and it is easy to
    miss. The three statements are in three separate transactions, and the effect
    happens BEFORE the key row is recorded. That is not a strawman -- it is what
    check-then-act looks like in the wild, because `charge_the_card()` is an HTTP
    call to a payment processor that has no transaction to join and cannot be
    rolled back. Here the charges INSERT stands in for that call.

    Put the effect and the key row in one transaction and A stops failing -- but
    that *is* implementation B's structural rule, so writing it here would be
    writing B and calling it A.
    """
    row = conn.execute(
        "SELECT state FROM idempotency_keys WHERE tenant_id = %s AND key = %s",
        (TENANT, key),
    ).fetchone()
    # Under READ COMMITTED neither concurrent request sees the other's
    # uncommitted INSERT, so both of them find nothing and both proceed.
    if row is not None:
        return 200, "replay"

    if hold_ms:
        time.sleep(hold_ms / 1000.0)   # the processor call takes a moment
    conn.execute(
        "INSERT INTO charges (run_id, impl, tenant_id, idempotency_key, amount_cents)"
        " VALUES (%s, 'A', %s, %s, %s)",
        (run_id, TENANT, key, body["amount_cents"]),
    )
    # The money has moved. Only now do we try to record that it did -- and the
    # unique index tells us, far too late, that somebody else already had.
    try:
        conn.execute(
            "INSERT INTO idempotency_keys (tenant_id, key, fingerprint, state) "
            "VALUES (%s, %s, %s, 'succeeded')",
            (TENANT, key, fingerprint(body)),
        )
    except pg_errors.UniqueViolation:
        return 500, "23505 AFTER charging -- the card was already charged"
    return 201, "charged"


def handle_b(conn: psycopg.Connection, run_id: str, key: str, body: dict,
             hold_ms: int) -> tuple[int, str]:
    """B -- atomic insert. Key row and effect commit in the same transaction."""
    fp = fingerprint(body)
    with conn.transaction():
        cur = conn.execute(
            "INSERT INTO idempotency_keys (tenant_id, key, fingerprint, state) "
            "VALUES (%s, %s, %s, 'in_flight') "
            "ON CONFLICT (tenant_id, key) DO NOTHING",
            (TENANT, key, fp),
        )
        # ON CONFLICT DO NOTHING RETURNING id returns ZERO rows on conflict, so
        # RETURNING never tells you about the row that already exists. "Did I
        # win?" is rowcount == 1, and the loser must SELECT separately.
        #
        # And note what already happened above if we lost: under READ COMMITTED
        # this INSERT *blocked* on the unique index until the winner committed or
        # rolled back. It did not fail fast. A duplicate's latency is bounded
        # below by the winner's entire transaction -- that is the design's price,
        # and the p99-duplicate column below is where you read it.
        won = cur.rowcount == 1

        if won:
            if hold_ms:
                time.sleep(hold_ms / 1000.0)
            charge_id, = conn.execute(
                "INSERT INTO charges (run_id, impl, tenant_id, idempotency_key,"
                " amount_cents) VALUES (%s, 'B', %s, %s, %s) RETURNING id",
                (run_id, TENANT, key, body["amount_cents"]),
            ).fetchone()
            conn.execute(
                "UPDATE idempotency_keys SET state = 'succeeded', response = %s "
                "WHERE tenant_id = %s AND key = %s",
                (json.dumps({"charge_id": charge_id, "status": "succeeded"}),
                 TENANT, key),
            )
            return 201, "charged"

        state, stored_fp, response = conn.execute(
            "SELECT state, fingerprint, response FROM idempotency_keys "
            "WHERE tenant_id = %s AND key = %s",
            (TENANT, key),
        ).fetchone()
        if stored_fp != fp:
            # Same key, different body. Replaying the stored response here would
            # tell the caller their *new* request succeeded. It did not exist.
            return 422, "fingerprint mismatch"
        if state == "succeeded":
            return 200, f"replay {response}"
        if state == "failed_permanently":
            return 409, "previous attempt failed permanently"
        # in_flight and we got here anyway: the winner rolled back rather than
        # committed, so nobody owns this key. Retryable, and it must say so.
        return 409, "in flight, retry"


def handle_c(conn: psycopg.Connection, run_id: str, key: str, body: dict,
             hold_ms: int) -> tuple[int, str]:
    """C -- advisory lock. Also correct; a different cost profile."""
    with conn.transaction():
        # xact, not session. A session-level advisory lock behind pgbouncer in
        # transaction pooling mode is held on a server connection you stop owning
        # the moment the transaction ends -- Topic 7 returns to this.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{TENANT}:{key}",))
        row = conn.execute(
            "SELECT state FROM idempotency_keys WHERE tenant_id = %s AND key = %s",
            (TENANT, key),
        ).fetchone()
        if row is not None:
            return 200, "replay"
        conn.execute(
            "INSERT INTO idempotency_keys (tenant_id, key, fingerprint, state) "
            "VALUES (%s, %s, %s, 'succeeded')",
            (TENANT, key, fingerprint(body)),
        )
        if hold_ms:
            time.sleep(hold_ms / 1000.0)
        conn.execute(
            "INSERT INTO charges (run_id, impl, tenant_id, idempotency_key, amount_cents)"
            " VALUES (%s, 'C', %s, %s, %s)",
            (run_id, TENANT, key, body["amount_cents"]),
        )
    return 201, "charged"


HANDLERS = {"A": handle_a, "B": handle_b, "C": handle_c}


# ----------------------------------------------------------------- the harness

class Result:
    __slots__ = ("status", "note", "ms")

    def __init__(self, status: int, note: str, ms: float) -> None:
        self.status, self.note, self.ms = status, note, ms


def worker(slot: int, impl: str, run_id: str, keys: list[str], barrier: threading.Barrier,
           hold_ms: int, vary_slot: int, out: list[list[Result]], errors: list[str]) -> None:
    """One connection, one virtual client. Fires request `slot` for every key."""
    handler = HANDLERS[impl]
    with lab_db.connect(autocommit=True) as conn:
        for i, key in enumerate(keys):
            body = {"amount_cents": 4200 + i, "currency": "GBP"}
            if slot == vary_slot:
                # Same key, DIFFERENT body -- a client that reused an idempotency
                # key for a new request. Only implementation B can tell; A and C
                # store no fingerprint, so they replay the wrong thing at 200.
                body = {"amount_cents": 999999, "currency": "GBP"}
            try:
                barrier.wait(timeout=30)      # every slot leaves together
            except threading.BrokenBarrierError:
                return
            t0 = time.perf_counter()
            try:
                status, note = handler(conn, run_id, key, body, hold_ms)
            except psycopg.Error as exc:
                status, note = 500, f"{type(exc).__name__}"
                errors.append(f"slot {slot} key {key}: {exc}")
            ms = (time.perf_counter() - t0) * 1000.0
            out[i].append(Result(status, note, ms))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--impl", choices=sorted(HANDLERS), required=True)
    ap.add_argument("--keys", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=5,
                    help="simultaneous requests per key, on separate connections")
    ap.add_argument("--hold-ms", type=int, default=10,
                    help="deliberate delay inside the winner's transaction, "
                         "identical in all three impls, to widen the race window")
    ap.add_argument("--vary-slot", type=int, default=-1,
                    help="make this slot send a DIFFERENT body under the same key "
                         "(-1 = off). Exercises the fingerprint check: B answers "
                         "422, A and C cannot tell and answer 200.")
    ap.add_argument("--reset", action="store_true",
                    help="truncate both tables first (default: keep history, "
                         "run ids keep runs apart)")
    args = ap.parse_args(argv)

    run_id = f"{args.impl}-{uuid.uuid4().hex[:8]}"
    keys = [f"{run_id}-key-{i:05d}" for i in range(args.keys)]

    conn = lab_db.open_lab(ddl=DDL)
    if args.reset:
        conn.execute("TRUNCATE charges, idempotency_keys")
        print("[setup] truncated charges and idempotency_keys")

    lab_db.banner(f"Topic 2 -- IMPL {args.impl}   {args.keys} keys x "
                  f"{args.concurrency} simultaneous requests")
    print(f"  server        : {lab_db.describe_server(conn)}")
    print(f"  run id        : {run_id}")
    print(f"  hold in txn   : {args.hold_ms} ms (identical across A, B and C)")
    if args.vary_slot >= 0:
        print(f"  vary slot     : {args.vary_slot} sends a different body under the same key")
    print("  charges index : NO unique constraint on idempotency_key -- deliberate,")
    print("                  otherwise the database would make A look correct")

    barrier = threading.Barrier(args.concurrency)
    per_key: list[list[Result]] = [[] for _ in keys]
    errors: list[str] = []
    threads = [
        threading.Thread(target=worker, daemon=True,
                         args=(s, args.impl, run_id, keys, barrier,
                               args.hold_ms, args.vary_slot, per_key, errors))
        for s in range(args.concurrency)
    ]
    wall0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - wall0

    # ------------------------------------------------------------- the numbers
    # Winners are decided by the database, not by the harness: the request whose
    # charge row exists is the winner. Everything else is a duplicate attempt.
    charged = [r for rs in per_key for r in rs if r.status == 201]
    conflicts = [r for rs in per_key for r in rs if r.status == 409]
    replays = [r for rs in per_key for r in rs if r.status == 200]
    unprocessable = [r for rs in per_key for r in rs if r.status == 422]
    failed = [r for rs in per_key for r in rs if r.status == 500]
    losers = conflicts + replays + unprocessable + failed

    dup_rows = conn.execute(
        "SELECT count(*) FROM (SELECT idempotency_key FROM charges "
        " WHERE run_id = %s GROUP BY 1 HAVING count(*) > 1) d", (run_id,)
    ).fetchone()[0]
    extra_charges = conn.execute(
        "SELECT coalesce(sum(c - 1), 0) FROM (SELECT count(*) c FROM charges "
        " WHERE run_id = %s GROUP BY idempotency_key) d", (run_id,)
    ).fetchone()[0]
    total_charges = conn.execute(
        "SELECT count(*) FROM charges WHERE run_id = %s", (run_id,)
    ).fetchone()[0]

    lab_db.section("correctness")
    print(f"  requests issued          {args.keys * args.concurrency}")
    print(f"  charge rows written      {total_charges}   (must equal {args.keys})")
    print(f"  KEYS CHARGED MORE THAN ONCE   {dup_rows}")
    print(f"  DUPLICATE CHARGES (extra rows) {extra_charges}")
    if extra_charges:
        print("  ^ every one of these is a customer charged twice for one request.")

    lab_db.section("what each request saw")
    for label, group in (("201 charged", charged), ("200 replayed", replays),
                         ("409 conflict", conflicts), ("422 fingerprint", unprocessable),
                         ("500 error", failed)):
        print(f"  {label:<18}{len(group)}")
    if failed:
        seen: dict[str, int] = {}
        for r in failed:
            seen[r.note] = seen.get(r.note, 0) + 1
        for note, n in sorted(seen.items(), key=lambda kv: -kv[1])[:5]:
            print(f"      {n:>5}x  {note}")

    lab_db.section("latency -- winners vs duplicates (the price of the design)")
    def stat(rs: list[Result]) -> str:
        if not rs:
            return "        -             -             -"
        ms = [r.ms for r in rs]
        return (f"{lab_db.percentile(ms, 0.50):9.1f} ms {lab_db.percentile(ms, 0.99):10.1f} ms"
                f" {max(ms):10.1f} ms")
    print(f"  {'':<14}{'p50':>12}{'p99':>15}{'max':>15}")
    print(f"  {'winner':<14}{stat(charged)}")
    print(f"  {'duplicate':<14}{stat(losers)}")
    print()
    print("  read this against --hold-ms: the winner's own latency already contains")
    print("  the hold, so equal p50s mean the losers waited out the winner's ENTIRE")
    print("  transaction, not that they were cheap. Raise --hold-ms and watch both")
    print("  rows move together -- that coupling is what the design costs you.")
    print(f"\n  wall clock {wall:.2f}s for {args.keys * args.concurrency} requests")

    if errors:
        lab_db.section(f"driver errors ({len(errors)}) -- first 3")
        for e in errors[:3]:
            print("  " + " ".join(e.split())[:110])

    print()
    print(f"  full breakdown:  psql -d {lab_db.DB_NAME} -f sql/topic2_assert.sql")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
