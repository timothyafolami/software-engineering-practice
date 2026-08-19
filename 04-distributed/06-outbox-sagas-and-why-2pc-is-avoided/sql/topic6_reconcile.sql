-- Layer 4 Topic 6 -- outbox reconciliation. This file is the deliverable.
--
-- WHAT THIS DEMONSTRATES: whether every charge ended up with at least one event,
-- and whether the consumer's EFFECT count equals the charge count exactly.
-- Those are two different questions and conflating them is the usual mistake:
-- duplicates are expected at the DELIVERY layer and are a bug only at the
-- EFFECT layer.
--
-- WHAT TO LOOK FOR: query 2. The high-water-mark relay leaves rows unpublished
-- forever and query 3 proves they are unreachable rather than merely late. The
-- SKIP LOCKED relay leaves none.
--
--   python3 python/hwm_skip.py --writers 3 --hold-seconds 2 --duration 60
--   python3 python/outbox_relay.py --seconds 30
--   node nodejs/idempotent_consumer.js --seconds 30
--   psql -d sep_lab_04_dist -f sql/topic6_reconcile.sql
--
-- Table names carry a t6_ prefix because the whole layer shares one scratch
-- database and Topic 2 already owns a table called `charges`.

\echo
\echo === 0. Did the id / commit-order inversion happen at all? ===
-- Pairs where the LOWER id committed LATER. If this is zero, the run proved
-- nothing -- ids and commit order coincided and there was nothing to skip.
-- Raise --hold-seconds or --writers and run it again. This is the README's
-- "broken experiment rather than wrong prediction" check, in SQL.
SELECT a.run_id,
       count(*) AS id_commit_inversions
FROM   t6_outbox a
JOIN   t6_outbox b
  ON   a.run_id = b.run_id AND a.id < b.id AND a.committed_at > b.committed_at
GROUP  BY a.run_id
ORDER  BY a.run_id;

\echo
\echo === 1. Every charge must have at least one event ===
SELECT c.run_id,
       count(*)                                        AS charges,
       count(o.id)                                     AS with_outbox_row,
       count(*) - count(o.id)                          AS MISSING_EVENTS
FROM   t6_charges c
LEFT   JOIN t6_outbox o ON o.run_id = c.run_id AND o.aggregate_id = c.id
GROUP  BY c.run_id
ORDER  BY c.run_id;

\echo
\echo === 2. THE RESULT: what each relay could deliver, over the same rows ===
-- One row per (run, relay). The LEFT JOIN is what makes the skipped count
-- appear at all: an INNER JOIN can only ever count rows that WERE delivered,
-- which is the same mistake as monitoring a relay by what it published.
SELECT o.run_id,
       r.relay,
       count(DISTINCT o.id)                               AS outbox_rows,
       count(DISTINCT d.outbox_id)                        AS delivered,
       count(DISTINCT o.id) - count(DISTINCT d.outbox_id) AS permanently_skipped,
       count(d.outbox_id) - count(DISTINCT d.outbox_id)   AS duplicate_deliveries
FROM   t6_outbox o
JOIN   (SELECT DISTINCT run_id, relay FROM t6_delivered) r ON r.run_id = o.run_id
LEFT   JOIN t6_delivered d
  ON   d.run_id = o.run_id AND d.outbox_id = o.id AND d.relay = r.relay
GROUP  BY o.run_id, r.relay
ORDER  BY o.run_id, r.relay;

\echo
\echo === 3. The skipped rows, and the proof they are unreachable ===
-- A row is unreachable rather than late when its id is BELOW the highest id the
-- relay has already delivered. `WHERE id > last_seen` can never return it again,
-- however long you wait and however often you poll.
WITH mark AS (
    SELECT run_id, max(outbox_id) AS high_water
    FROM   t6_delivered WHERE relay = 'high-water-mark'
    GROUP  BY run_id
)
SELECT o.run_id,
       o.id            AS skipped_outbox_id,
       m.high_water,
       o.committed_at,
       (o.id < m.high_water) AS below_the_mark_forever
FROM   t6_outbox o
JOIN   mark m ON m.run_id = o.run_id
WHERE  NOT EXISTS (SELECT 1 FROM t6_delivered d
                    WHERE d.run_id = o.run_id AND d.outbox_id = o.id
                      AND d.relay = 'high-water-mark')
ORDER  BY o.run_id, o.id
LIMIT  25;

\echo
\echo === 4. Unpublished backlog, by age ===
-- Under the SKIP LOCKED relay this drains. A backlog that stops shrinking while
-- the relay is running is either a relay that has died or rows it cannot see.
SELECT run_id,
       count(*)                                                   AS unpublished,
       round(extract(epoch FROM max(now() - committed_at)))::int  AS oldest_seconds
FROM   t6_outbox
WHERE  published_at IS NULL AND committed_at IS NOT NULL
GROUP  BY run_id
ORDER  BY run_id;

\echo
\echo === 5. charge -> event latency, measured from the COMMIT ===
-- Measured from committed_at, not from when the relay read the row. Measuring
-- from the read is how a polling relay ends up reporting a sub-millisecond p99,
-- which your poll interval says is impossible.
SELECT o.run_id,
       d.relay,
       coalesce(d.woken_by, '-')                                             AS woken_by,
       count(*)                                                              AS events,
       round(1000 * percentile_disc(0.50) WITHIN GROUP (
             ORDER BY extract(epoch FROM d.delivered_at - o.committed_at)))::int AS p50_ms,
       round(1000 * percentile_disc(0.99) WITHIN GROUP (
             ORDER BY extract(epoch FROM d.delivered_at - o.committed_at)))::int AS p99_ms
FROM   t6_delivered d
JOIN   t6_outbox o ON o.id = d.outbox_id
WHERE  o.committed_at IS NOT NULL
GROUP  BY o.run_id, d.relay, d.woken_by
ORDER  BY o.run_id, d.relay, d.woken_by;

\echo
\echo === 6. The EFFECT count, the only one that may not duplicate ===
-- Duplicates at the delivery layer are expected and fine -- Topic 2 made the
-- consumer idempotent precisely so they would be. A duplicate HERE means the
-- consumer is check-then-act, and the fix is in Topic 2, not in this topic.
SELECT run_id,
       count(*)                             AS effects,
       count(DISTINCT outbox_id)            AS distinct_outbox_ids,
       count(*) - count(DISTINCT outbox_id) AS DUPLICATE_EFFECTS
FROM   t6_consumer_effects
GROUP  BY run_id
ORDER  BY run_id;
