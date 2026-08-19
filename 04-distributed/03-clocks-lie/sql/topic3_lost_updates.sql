-- Layer 4 Topic 3 -- lost updates from clock skew. This file is the deliverable.
--
-- WHAT THIS DEMONSTRATES: that "last write wins on a client timestamp" discards
-- user writes silently, and that the same load with the same skew loses nothing
-- once the comparison stops consulting a client clock.
--
-- THE DEFINITION, derived rather than assumed. A rejected write is only a LOSS
-- if it was not concurrent with the write that beat it. If the two overlapped in
-- real time, either could legitimately have won and no clock is at fault. So:
--
--     lost update  <=>  the rejected write was SUBMITTED (submitted_db_ts)
--                       after the winning write had FINISHED (winner_db_ts)
--
-- Both timestamps come from the DATABASE's clock, never from a writer's. The
-- client clock is the thing under test and cannot also be the referee.
--
-- WHAT TO LOOK FOR: query 2. v0 at a non-zero offset should show losses; v0 at
-- offset 0 and v1 at any offset should show none. If v1 loses updates, read the
-- README's broken-experiment note before believing it -- the usual cause is an
-- application-computed timestamp passed in as a parameter, which is v0 wearing
-- a disguise.
--
--   python3 python/lww_writers.py --variant v0 --offset-ms 250
--   python3 python/lww_writers.py --variant v0 --offset-ms 0
--   python3 python/lww_writers.py --variant v1 --offset-ms 250
--   python3 python/lww_writers.py --variant v2 --offset-ms 250
--   psql -d sep_lab_04_dist -f sql/topic3_lost_updates.sql

\echo
\echo === 0. Did the offset take effect? Nothing below matters if it did not. ===
-- client_ts is the writer's own clock; executed_db_ts is the database's. The gap
-- between the two writers IS the independent variable, measured rather than
-- assumed. If these rows are not offset_ms apart, stop here.
SELECT run_id,
       variant,
       offset_ms,
       writer,
       count(*)                                                    AS writes,
       round(avg(extract(epoch FROM client_ts - executed_db_ts)) * 1000)::int
                                                                   AS mean_skew_ms
FROM   lww_write_log
GROUP  BY run_id, variant, offset_ms, writer
ORDER  BY run_id, writer;

\echo
\echo === 1. THE RESULT: lost updates per run ===
SELECT run_id,
       variant,
       offset_ms,
       count(*) FILTER (WHERE outcome = 'applied')          AS applied,
       count(*) FILTER (WHERE outcome = 'rejected_lww'
                          AND submitted_db_ts >  winner_db_ts) AS lost_updates,
       count(*) FILTER (WHERE outcome = 'rejected_lww'
                          AND submitted_db_ts <= winner_db_ts) AS concurrent_conflicts,
       count(*) FILTER (WHERE outcome = 'rejected_cas')     AS rejected_cas,
       round(100.0 * count(*) FILTER (WHERE outcome = 'rejected_lww'
                                        AND submitted_db_ts > winner_db_ts)
             / greatest(count(*), 1), 2)                    AS pct_lost
FROM   lww_write_log
GROUP  BY run_id, variant, offset_ms
ORDER  BY variant, offset_ms, run_id;

\echo
\echo === 2. How far back did the world go, per lost write? ===
-- The gap between a losing write's submission and the moment the value that beat
-- it was written. This is what the user experiences as "it reverted".
SELECT run_id,
       variant,
       offset_ms,
       count(*)                                                          AS lost,
       round(1000 * min(extract(epoch FROM submitted_db_ts - winner_db_ts)))::int  AS min_ms,
       round(1000 * percentile_disc(0.5) WITHIN GROUP (
             ORDER BY extract(epoch FROM submitted_db_ts - winner_db_ts)))::int    AS p50_ms,
       round(1000 * max(extract(epoch FROM submitted_db_ts - winner_db_ts)))::int  AS max_ms
FROM   lww_write_log
WHERE  outcome = 'rejected_lww' AND submitted_db_ts > winner_db_ts
GROUP  BY run_id, variant, offset_ms
ORDER  BY variant, offset_ms, run_id;

\echo
\echo === 3. Who won? A writer locked out of its own keys is the visible symptom ===
-- Under v0 with a skewed writer this is lopsided: the writer whose clock runs
-- ahead wins essentially every contest, regardless of who wrote last. Support
-- tickets describe this as "only some people's edits save".
SELECT run_id,
       variant,
       offset_ms,
       writer,
       count(*) FILTER (WHERE outcome = 'applied')  AS applied,
       count(*) FILTER (WHERE outcome <> 'applied') AS rejected,
       round(100.0 * count(*) FILTER (WHERE outcome = 'applied')
             / greatest(count(*), 1), 1)            AS pct_applied
FROM   lww_write_log
GROUP  BY run_id, variant, offset_ms, writer
ORDER  BY run_id, writer;

\echo
\echo === 4. Final stored value per key, and which writer owns it ===
SELECT run_id,
       writer,
       count(*) AS keys_held
FROM   lww_items
GROUP  BY run_id, writer
ORDER  BY run_id, writer;

\echo
\echo === 5. Sanity: rows with no winner recorded ===
-- A rejection with a NULL winner_db_ts means the harness could not identify what
-- beat it. Expect zero. Anything else and queries 1 and 2 are undercounting, so
-- fix this before reading them.
SELECT run_id, variant, count(*) AS rejections_with_no_winner
FROM   lww_write_log
WHERE  outcome <> 'applied' AND winner_db_ts IS NULL
GROUP  BY run_id, variant
ORDER  BY run_id;
