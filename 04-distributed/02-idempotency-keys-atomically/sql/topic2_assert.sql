-- Layer 4 Topic 2 -- the assertion. This file is the deliverable, not a log line.
--
-- WHAT THIS DEMONSTRATES: whether any idempotency key was charged more than
-- once. Query 1 is the whole test and it must return zero rows.
--
-- WHAT TO LOOK FOR: query 1 empty, and query 2's `extra_charges` column zero for
-- B and C. A is expected to be non-zero -- that is why A is in the experiment.
--
--   psql -d sep_lab_04_dist -f sql/topic2_assert.sql
--
-- Runs are kept apart by run_id (impl + a random suffix), so this reports every
-- run still in the table rather than only the last one. Truncate with
-- `python3 python/idempotency_race.py --impl B --reset ...` if you want a clean
-- sheet.

\echo
\echo === 1. THE TEST: keys charged more than once. Must be empty. ===
SELECT run_id,
       idempotency_key,
       count(*)              AS charges,
       min(created_at)       AS first_charge,
       max(created_at)       AS last_charge,
       max(created_at) - min(created_at) AS window
FROM   charges
GROUP  BY run_id, idempotency_key
HAVING count(*) > 1
ORDER  BY count(*) DESC, run_id
LIMIT  20;

\echo
\echo === 2. Per run: requests reconciled against rows ===
SELECT c.run_id,
       c.impl,
       count(DISTINCT c.idempotency_key)          AS keys_charged,
       count(*)                                   AS charge_rows,
       count(*) - count(DISTINCT c.idempotency_key) AS extra_charges,
       round(100.0 * (count(*) - count(DISTINCT c.idempotency_key))
             / greatest(count(*), 1), 2)          AS pct_duplicate
FROM   charges c
GROUP  BY c.run_id, c.impl
ORDER  BY c.impl, c.run_id;

\echo
\echo === 3. Key rows: the three-state machine, per run ===
-- in_flight rows left behind after a run finished are not a harness bug. They
-- are a poisoned key: some transaction inserted the claim and never resolved it.
-- Under implementation B that cannot outlive the transaction; if you see one,
-- find out which implementation wrote it before doing anything else.
SELECT split_part(k.key, '-key-', 1) AS run_id,
       k.state,
       count(*)                      AS keys,
       count(k.response)             AS with_stored_response
FROM   idempotency_keys k
GROUP  BY 1, 2
ORDER  BY 1, 2;

\echo
\echo === 4. Charged with no key row, or key row with no charge ===
-- Both directions of the structural rule "the key row and the effect commit in
-- the SAME transaction". A row on either side of this query is that rule broken.
SELECT 'charge with no key row' AS problem, count(*) AS n
FROM   charges c
WHERE  NOT EXISTS (SELECT 1 FROM idempotency_keys k
                   WHERE k.tenant_id = c.tenant_id AND k.key = c.idempotency_key)
UNION ALL
SELECT 'succeeded key with no charge', count(*)
FROM   idempotency_keys k
WHERE  k.state = 'succeeded'
  AND  NOT EXISTS (SELECT 1 FROM charges c
                   WHERE c.tenant_id = k.tenant_id AND c.idempotency_key = k.key);

\echo
\echo === 5. Index health on the key table ===
-- The unique index IS the idempotency. If it is missing, every result above is
-- meaningless. charges deliberately has no unique constraint -- see the header
-- of python/idempotency_race.py for why, and put one back in production.
SELECT tablename, indexname, indexdef
FROM   pg_indexes
WHERE  tablename IN ('idempotency_keys', 'charges')
ORDER  BY tablename, indexname;
