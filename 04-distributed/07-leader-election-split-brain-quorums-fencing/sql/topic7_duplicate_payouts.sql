-- Layer 4 Topic 7 -- duplicate payouts from a paused leader. The deliverable.
--
-- WHAT THIS DEMONSTRATES: that a lease with a timeout does not prevent two
-- workers acting at once, and that a fencing token validated BY THE RESOURCE
-- does.
--
-- THE DEFINITION, derived rather than assumed. A payout written under two
-- different epochs is NOT automatically a duplicate: a new leader re-driving a
-- pending payout is the system working, and a metric that cannot tell that apart
-- from the bug is worse than no metric. A duplicate is an ACCEPTED write from a
-- STALE epoch:
--
--     the write was accepted (rows_updated > 0), and a write with a HIGHER
--     epoch had already been accepted for that same key, EARLIER in real time
--
-- There is no reading of that under which the stale write should have landed.
--
-- WHAT TO LOOK FOR: query 2, with fencing off and on. And query 3 -- "fencing
-- works but you never saw a rejected write" means the stale writer never tried,
-- and the run tested nothing.
--
--   python3 python/fencing_demo.py --fencing 0
--   python3 python/fencing_demo.py --fencing 1
--   psql -d sep_lab_04_dist -f sql/topic7_duplicate_payouts.sql
--
-- The compose version of the same experiment SIGSTOPs a container instead of
-- pausing a thread and is blocked while the Docker daemon is down. What it adds
-- is a pause landing inside an in-flight statement; what it does not change is
-- any of the arithmetic below.

\echo
\echo === 0. Did a takeover actually happen? Nothing below matters if not. ===
-- The lease epoch must have advanced past 1, and there must be attempts from
-- more than one worker. A run where one worker held the lease throughout has
-- no split brain in it to observe.
SELECT run_id,
       fencing,
       count(DISTINCT worker)  AS workers_that_wrote,
       min(epoch)              AS first_epoch,
       max(epoch)              AS last_epoch
FROM   t7_payout_attempts
GROUP  BY run_id, fencing
ORDER  BY run_id;

\echo
\echo === 1. THE RESULT: duplicate payouts, by run ===
SELECT a.run_id,
       a.fencing,
       count(*)                                          AS attempts,
       count(*) FILTER (WHERE a.rows_updated > 0)        AS accepted,
       count(*) FILTER (WHERE a.rows_updated = 0)        AS rejected_by_resource,
       count(*) FILTER (
           WHERE a.rows_updated > 0
             AND EXISTS (SELECT 1 FROM t7_payout_attempts b
                          WHERE b.run_id = a.run_id
                            AND b.payout_key = a.payout_key
                            AND b.rows_updated > 0
                            AND b.epoch > a.epoch
                            AND b.attempted_at < a.attempted_at))
                                                         AS DUPLICATE_PAYOUTS
FROM   t7_payout_attempts a
GROUP  BY a.run_id, a.fencing
ORDER  BY a.fencing, a.run_id;

\echo
\echo === 2. The stale writer: what it attempted, and what the resource did ===
-- With fencing OFF, rejected should be 0 and every stale attempt landed.
-- With fencing ON, every stale attempt must be rejected. A stale attempt count
-- of 0 in either case is a broken run, not a clean one.
--
-- "Stale" here means stale AT THE TIME OF THE ATTEMPT: a higher epoch had
-- already written something, earlier in real time. Comparing against the FINAL
-- epoch instead would mark the old leader's perfectly legitimate pre-pause
-- writes as stale, and then fencing would look broken in every run.
SELECT a.run_id,
       a.fencing,
       a.worker,
       a.epoch,
       count(*)                                   AS stale_attempts,
       count(*) FILTER (WHERE a.rows_updated = 0) AS rejected,
       count(*) FILTER (WHERE a.rows_updated > 0) AS ACCEPTED_WHILE_STALE
FROM   t7_payout_attempts a
WHERE  EXISTS (SELECT 1 FROM t7_payout_attempts b
                WHERE b.run_id = a.run_id
                  AND b.epoch > a.epoch
                  AND b.attempted_at < a.attempted_at)
GROUP  BY a.run_id, a.fencing, a.worker, a.epoch
ORDER  BY a.run_id, a.worker;

\echo
\echo === 3. Every payout that two epochs both claimed to have sent ===
SELECT run_id,
       payout_key,
       count(DISTINCT epoch) FILTER (WHERE rows_updated > 0) AS epochs_that_sent_it,
       min(epoch) FILTER (WHERE rows_updated > 0)            AS lowest,
       max(epoch) FILTER (WHERE rows_updated > 0)            AS highest
FROM   t7_payout_attempts
GROUP  BY run_id, payout_key
HAVING count(DISTINCT epoch) FILTER (WHERE rows_updated > 0) > 1
ORDER  BY run_id, payout_key
LIMIT  25;

\echo
\echo === 4. Final state of the payouts table ===
-- fence is the epoch of the write that last touched each row. A payout whose
-- fence went DOWN over the run is a stale write that landed, visible in the
-- table itself rather than only in the attempt log.
SELECT p.run_id,
       count(*)                                    AS payouts,
       count(*) FILTER (WHERE p.status = 'sent')   AS sent,
       min(p.fence)                                AS min_fence,
       max(p.fence)                                AS max_fence,
       count(DISTINCT p.sent_by)                   AS distinct_senders
FROM   t7_payouts p
GROUP  BY p.run_id
ORDER  BY p.run_id;

\echo
\echo === 5. The lease row itself ===
-- epoch increments on ACQUISITION, not on renewal, so it names a leadership
-- term. It is an integer from the database, never a timestamp: a clock can go
-- backwards (Topic 3) and a sequence cannot, which is the whole reason etcd
-- hands you CreateRevision rather than a lease expiry time.
SELECT name, holder, epoch, expires_at, expires_at < now() AS expired
FROM   t7_leases
ORDER  BY name;
