-- 7. The same query after CREATE STATISTICS on the pair.
--
-- Extended statistics teach the planner three things about a GROUP of columns:
--   ndistinct    -- how many distinct combinations actually occur
--   dependencies -- functional dependency, "status implies country here"
--   mcv          -- the most common COMBINATIONS and their frequencies
-- The mcv list is what repairs this particular estimate, because ('NG',
-- 'failed') is itself a common combination and now has its own measured
-- frequency rather than a product of two frequencies assumed independent.
--
-- PREDICT FIRST: does the estimate land exactly on 25,000, or merely close?
-- Extended statistics are still statistics -- sampled, not counted.
--
-- plan_drill.py runs `CREATE STATISTICS ... ; ANALYZE` between query 6 and this
-- one. Standalone, the setup below does it for you -- including the table, so
-- this file also runs on its own without 06 having been run first.
CREATE TABLE IF NOT EXISTS plan_drill_corr AS
SELECT g AS id,
       CASE WHEN mod(g, 100) < 10 THEN 'NG'
            WHEN mod(g, 100) < 40 THEN 'US'
            WHEN mod(g, 100) < 70 THEN 'GB'
            ELSE 'KE' END AS country,
       CASE WHEN mod(g, 100) < 5 THEN 'failed'
            ELSE 'complete' END AS status,
       100 + mod(g::bigint * 7919, 90000) AS amount_cents
FROM generate_series(1, 500000) g;
CREATE STATISTICS IF NOT EXISTS plan_drill_corr_stats (ndistinct, dependencies, mcv)
    ON country, status FROM plan_drill_corr;
ANALYZE plan_drill_corr;
-- @explain
SELECT count(*), sum(amount_cents)
FROM plan_drill_corr
WHERE country = 'NG' AND status = 'failed';
