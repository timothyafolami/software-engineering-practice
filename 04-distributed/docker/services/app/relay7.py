"""Topic 7 -- relay-a / relay-b, an ELECTED SINGLETON in two containers.

Same schema and the same fencing arithmetic as python/fencing_demo.py. What the
container version adds is the one thing a paused thread cannot do: the pause is
`docker kill -s SIGSTOP` on a whole process, so it can land INSIDE an in-flight
statement, and the process wakes up with an open socket to Postgres and a lease
it still believes it holds.

Election is a lease row with a database-issued monotonic epoch. Not a timestamp:
a clock can go backwards and an epoch cannot, which is Topic 3 arriving in
Topic 7.

FENCING=0  the payout UPDATE carries the epoch but does not check it
FENCING=1  the UPDATE is guarded by  AND fence <= %(epoch)s -- validated BY THE
           RESOURCE, which is the only place that works, because the stale
           writer by definition does not know it is stale.
"""
import os, time, socket
from .db import pool

RUN_ID = os.environ.get("RUN_ID", "compose")
WORKER = os.environ.get("WORKER", socket.gethostname())
FENCING = os.environ.get("FENCING", "0") == "1"
TTL = int(os.environ.get("LEASE_TTL", "10"))
RENEW = float(os.environ.get("RENEW_INTERVAL", "1"))
KEYS = int(os.environ.get("PAYOUT_KEYS", "40"))
# A relay publishes a BATCH, not one row, and the topic README's part 2 is
# literally "watch it publish the batch it believed it owned". With a batch of
# 1 the woken worker gets exactly one stale write in before its renewal timer
# fires -- SIGSTOP does not pause CLOCK_MONOTONIC, so a woken process knows
# immediately that 15s passed -- and one write is too few to reliably land on a
# key the new leader has already paid. Measured with batch=1: 1 stale attempt,
# 0 duplicate payouts, which reads as "fencing was not needed".
BATCH = int(os.environ.get("PAYOUT_BATCH", "10"))

DDL = """
CREATE TABLE IF NOT EXISTS t7_leases (
    name text PRIMARY KEY, holder text, epoch bigint NOT NULL DEFAULT 0,
    expires_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS t7_payouts (
    id bigserial PRIMARY KEY, run_id text NOT NULL, payout_key text NOT NULL,
    status text NOT NULL DEFAULT 'pending', fence bigint NOT NULL DEFAULT 0,
    sent_by text, sent_at timestamptz, UNIQUE (run_id, payout_key));
CREATE TABLE IF NOT EXISTS t7_payout_attempts (
    id bigserial PRIMARY KEY, run_id text NOT NULL, fencing boolean NOT NULL,
    worker text NOT NULL, payout_key text NOT NULL, epoch bigint NOT NULL,
    rows_updated integer NOT NULL,
    attempted_at timestamptz NOT NULL DEFAULT clock_timestamp());
"""


# A SIGSTOP can land INSIDE an in-flight statement -- that is the whole reason
# the container version of this experiment exists, and a paused thread cannot do
# it. When it lands while the holder has the lease row locked in an open
# transaction, the other worker's takeover UPDATE blocks on that row lock for as
# long as the pause lasts, so NO TAKEOVER HAPPENS and the run has no split brain
# in it to observe (query 0 of the deliverable is exactly this check). Measured:
# the first fencing=0 run here held epoch 1 on one worker for the whole 15s
# pause. lock_timeout turns that indefinite block into a fast retry.
LOCK_TIMEOUT_MS = os.environ.get("LOCK_TIMEOUT_MS", "1500")


def acquire_or_renew(c, my_epoch: int | None) -> int | None:
    """Returns the epoch we hold, or None. Renewal does NOT bump the epoch --
    only a takeover does, which is what makes the epoch a fence rather than a
    counter of heartbeats."""
    if my_epoch is not None:
        r = c.execute(
            "UPDATE t7_leases SET expires_at = now() + make_interval(secs => %s)"
            " WHERE name=%s AND holder=%s AND epoch=%s RETURNING epoch",
            (TTL, RUN_ID, WORKER, my_epoch)).fetchone()
        if r:
            return r[0]
    r = c.execute(
        "INSERT INTO t7_leases(name,holder,epoch,expires_at)"
        " VALUES(%s,%s,1, now() + make_interval(secs => %s))"
        " ON CONFLICT (name) DO UPDATE"
        "   SET holder = EXCLUDED.holder,"
        "       epoch  = t7_leases.epoch + 1,"
        "       expires_at = EXCLUDED.expires_at"
        " WHERE t7_leases.expires_at < now()"
        " RETURNING epoch", (RUN_ID, WORKER, TTL)).fetchone()
    return r[0] if r else None


def do_payout(c, epoch: int, key: str) -> int:
    if FENCING:
        # fence <= epoch, NOT fence < epoch. The resource must reject writes
        # from a STRICTLY OLDER epoch and accept the current holder's own
        # re-drives. `fence < epoch` rejects the live leader the second time it
        # touches a key it already paid, which looks like fencing working
        # (108 rejections in the first run here) while measuring nothing about
        # split brain at all.
        sql = ("UPDATE t7_payouts SET status='sent', fence=%s, sent_by=%s,"
               " sent_at=clock_timestamp()"
               " WHERE run_id=%s AND payout_key=%s AND fence <= %s")
        args = (epoch, WORKER, RUN_ID, key, epoch)
    else:
        sql = ("UPDATE t7_payouts SET status='sent', fence=%s, sent_by=%s,"
               " sent_at=clock_timestamp()"
               " WHERE run_id=%s AND payout_key=%s")
        args = (epoch, WORKER, RUN_ID, key)
    n = c.execute(sql, args).rowcount
    c.execute("INSERT INTO t7_payout_attempts"
              "(run_id,fencing,worker,payout_key,epoch,rows_updated)"
              " VALUES(%s,%s,%s,%s,%s,%s)",
              (RUN_ID, FENCING, WORKER, key, epoch, n))
    return n


def main() -> None:
    with pool("LAB_DSN").connection() as c:
        c.execute(DDL)
        c.execute("INSERT INTO t7_payouts(run_id,payout_key)"
                  " SELECT %s, 'k'||g FROM generate_series(1,%s) g"
                  " ON CONFLICT DO NOTHING", (RUN_ID, KEYS))

    epoch = None
    i = 0
    last_renew = 0.0
    while True:
        try:
            with pool("LAB_DSN").connection() as c:
                c.execute(f"SET lock_timeout = {int(LOCK_TIMEOUT_MS)}")

                # ---- ACT FIRST, ON THE LEASE WE BELIEVE WE HOLD -------------
                # Renewing before every action makes the worker discover it was
                # deposed before it ever writes, and then there is no stale
                # write to observe: the first version of this file did that and
                # the deliverable's query 2 returned ZERO stale attempts, which
                # its own comment calls a broken run rather than a clean one.
                # No real relay re-validates its lease before every row -- it
                # renews on a timer and does work in between, and the window
                # between the two is where split brain lives.
                if epoch is not None:
                    for _ in range(BATCH):
                        do_payout(c, epoch, f"k{(i % KEYS) + 1}")
                        i += 1

                # ---- then, on the renewal timer, find out ------------------
                now = time.monotonic()
                if now - last_renew >= RENEW:
                    last_renew = now
                    if epoch is not None:
                        renewed = acquire_or_renew(c, epoch)
                        if renewed is None:
                            # Deposed. Drop the epoch rather than silently
                            # acquiring a new one and carrying on as if the
                            # pause never happened.
                            print(f"{WORKER}: LOST THE LEASE (was epoch {epoch})",
                                  flush=True)
                            epoch = None
                        else:
                            epoch = renewed
                    if epoch is None:
                        epoch = acquire_or_renew(c, None)
                        if epoch is not None:
                            print(f"{WORKER}: acquired epoch {epoch}", flush=True)
        except Exception as e:                               # noqa: BLE001
            time.sleep(0.2)
        time.sleep(0.25)


if __name__ == "__main__":
    main()
