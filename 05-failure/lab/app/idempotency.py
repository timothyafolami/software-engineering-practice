"""
Layer 5 lab - /charge, twice: the way everyone writes it, and the way that works.

WHAT THIS DEMONSTRATES
  Every technique in topics 2-6 makes a request happen more than once.
  Timeouts abandon work that completed; retries send it again; hedging
  sends it again ON PURPOSE. All of that is only legal if the operation
  can be repeated safely, which makes idempotency the precondition for the
  rest of the layer rather than a topic beside it.

  IDEMPOTENCY_MODE=naive     SELECT, then INSERT, no unique index. Reads
                             fine. Fifty concurrent requests with the same
                             key produce fifty charges, because every one
                             of them ran its SELECT before any of them ran
                             its INSERT. The bug is not in the code; it is
                             in believing a read tells you about the future.

  IDEMPOTENCY_MODE=correct   The uniqueness lives in the DATABASE:
                             idempotency_keys.key is a primary key, and
                             the claim is INSERT ... ON CONFLICT DO NOTHING
                             RETURNING. Exactly one caller gets a row back
                             and does the work; everyone else finds the
                             stored response and returns it byte-identical.

WHAT TO LOOK FOR
  charge_rows = 1 and distinct_responses = 1 in the correct mode, at every
  level of concurrency, including with the response path being destroyed by
  toxiproxy so the client never learns it succeeded. That ambiguous case -
  "did not happen" versus "happened, answer lost" - is the realistic one
  and the reason this exists.

  And orphaned_in_progress = 0 after a kill. A claim with no TTL is a
  distributed deadlock you wrote yourself: the process that claimed the key
  died, and every retry now waits for it forever. IDEMPOTENCY_TTL_S is the
  answer, and step 4 of the experiment is how you find out whether you
  thought about it or just typed it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

from sqlalchemy import text

from .config import config
from .db import do_work, engine
from .metrics import counters


def fingerprint(body: dict) -> str:
    """Stable hash of the request body, so the same key with a different body is caught."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


async def _insert_charge(conn, key: str, body: dict) -> int:
    row = await conn.execute(
        text("INSERT INTO charges (idem_key, amount_cents, currency) "
             "VALUES (:k, :a, :c) RETURNING id"),
        {"k": key, "a": int(body.get("amount_cents", 100)),
         "c": str(body.get("currency", "usd"))},
    )
    counters.inc("charges")
    return int(row.scalar_one())


async def charge_naive(key: str, body: dict) -> tuple[int, dict]:
    """SELECT then INSERT. The race is real and the window is the query time."""
    eng = await engine()
    async with eng.begin() as conn:
        existing = await conn.execute(
            text("SELECT id FROM charges WHERE idem_key = :k LIMIT 1"), {"k": key})
        found = existing.first()
        if found is not None:
            return 200, {"charge_id": int(found[0]), "replayed": True, "mode": "naive"}
        # The work that makes the window wide enough to see. In production
        # this is the payment gateway call, and it is much wider than this.
        await conn.execute(text("SELECT pg_sleep(:s)"),
                           {"s": max(0.0, int(config.get("SERVICE_MS")) / 1000.0)})
        charge_id = await _insert_charge(conn, key, body)
        return 201, {"charge_id": charge_id, "replayed": False, "mode": "naive"}


async def charge_correct(key: str, body: dict) -> tuple[int, dict]:
    """Claim the key in the database, then do the work, then publish the answer.

    Three states, and the middle one is where the interesting failures live:

      claimed by me      -> do the work, store the response, return it
      claimed by someone -> wait briefly for them to publish, then replay
                            their answer byte for byte; 409 only if they are
                            still working when the wait runs out; and if
                            their claim is older than IDEMPOTENCY_TTL_S,
                            steal it, because they are not coming back
      claimed with a different body -> 422, and the original charge is not
                            touched. Same key, different money, is a bug in
                            the caller and must not be silently replayed.
    """
    eng = await engine()
    fp = fingerprint(body)
    ttl = int(config.get("IDEMPOTENCY_TTL_S"))

    async with eng.begin() as conn:
        claimed = await conn.execute(
            text(
                "INSERT INTO idempotency_keys (key, fingerprint, state, expires_at) "
                "VALUES (:k, :f, 'in_progress', now() + make_interval(secs => :ttl)) "
                "ON CONFLICT (key) DO UPDATE "
                "  SET fingerprint = EXCLUDED.fingerprint, "
                "      created_at  = now(), "
                "      expires_at  = EXCLUDED.expires_at "
                "  WHERE idempotency_keys.state = 'in_progress' "
                "    AND idempotency_keys.expires_at < now() "
                "RETURNING key"
            ),
            {"k": key, "f": fp, "ttl": ttl},
        )
        mine = claimed.first() is not None

    if not mine:
        return await _replay(eng, key, fp, wait_s=float(config.get("CLIENT_TIMEOUT_MS")) / 1000.0)

    # We hold the claim. Do the work, then publish, in one transaction so a
    # crash between them leaves the claim expired rather than the charge lost.
    async with eng.begin() as conn:
        await conn.execute(text("SELECT pg_sleep(:s)"),
                           {"s": max(0.0, int(config.get("SERVICE_MS")) / 1000.0)})
        charge_id = await _insert_charge(conn, key, body)
        payload = {"charge_id": charge_id, "replayed": False, "mode": "correct"}
        await conn.execute(
            text("UPDATE idempotency_keys SET state = 'done', response = CAST(:r AS jsonb) "
                 "WHERE key = :k"),
            {"r": json.dumps(payload), "k": key},
        )
    return 201, payload


async def _replay(eng, key: str, fp: str, wait_s: float) -> tuple[int, dict]:
    """Someone else holds the claim. Wait, briefly, then return THEIR answer.

    This bounded wait is what turns "exactly one charge" into "exactly one
    charge and fifty byte-identical responses", which is the assertion the
    experiment actually makes. Returning 409 immediately would satisfy the
    first half and fail the second: the caller learns nothing about its own
    charge and has no choice but to retry, which is more load for an answer
    that already exists a few milliseconds away.

    The wait is bounded, because an unbounded one is the pool queue again -
    and if the holder died, the TTL is what unblocks this, not patience.
    """
    deadline = time.monotonic() + max(0.05, wait_s)
    poll_s = 0.02
    while True:
        async with eng.begin() as conn:
            row = (await conn.execute(
                text("SELECT fingerprint, state, response FROM idempotency_keys "
                     "WHERE key = :k"), {"k": key})).first()
        if row is None:
            # The holder's transaction rolled back between our INSERT and this
            # SELECT. Treat it as a conflict; the client's retry will claim it.
            counters.inc("conflicts")
            return 409, {"error": "in_progress", "mode": "correct"}
        stored_fp, state, response = row[0], row[1], row[2]
        if stored_fp != fp:
            counters.inc("fingerprint_rejects")
            return 422, {"error": "key_reused_with_different_body", "mode": "correct"}
        if state == "done" and response is not None:
            payload = dict(response if isinstance(response, dict) else json.loads(response))
            payload["replayed"] = True
            return 200, payload
        if time.monotonic() >= deadline:
            counters.inc("conflicts")
            return 409, {"error": "in_progress", "mode": "correct"}
        await asyncio.sleep(poll_s)


async def charge(key: str, body: dict) -> tuple[int, dict]:
    if config.get("IDEMPOTENCY_MODE") == "naive":
        return await charge_naive(key, body)
    return await charge_correct(key, body)


async def report() -> dict:
    """What the k6 script and the psql line both want to know, in one place."""
    eng = await engine()
    async with eng.connect() as conn:
        rows = (await conn.execute(text("SELECT count(*) FROM charges"))).scalar_one()
        keys = (await conn.execute(text("SELECT count(*) FROM idempotency_keys"))).scalar_one()
        orphaned = (await conn.execute(
            text("SELECT count(*) FROM idempotency_keys "
                 "WHERE state = 'in_progress' AND expires_at < now()"))).scalar_one()
        distinct = (await conn.execute(
            text("SELECT count(DISTINCT response::text) FROM idempotency_keys "
                 "WHERE state = 'done'"))).scalar_one()
    return {
        "mode": config.get("IDEMPOTENCY_MODE"),
        "charge_rows": int(rows),
        "idempotency_keys": int(keys),
        "orphaned_in_progress": int(orphaned),
        "distinct_responses": int(distinct),
        "409s": counters.snapshot()["conflicts"],
        "422s": counters.snapshot()["fingerprint_rejects"],
    }


async def reset() -> dict:
    """Truncate both tables. Each mode's run has to start from a clean slate."""
    eng = await engine()
    async with eng.begin() as conn:
        await conn.execute(text("TRUNCATE charges, idempotency_keys"))
    return {"reset": True}


__all__ = ["charge", "report", "reset", "fingerprint", "do_work"]
