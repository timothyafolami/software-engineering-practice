-- 6. Two predicates the planner believes are independent, on a table where they
--    are anything but.
--
-- plan_drill_corr is built by plan_drill.py with country and status correlated
-- on purpose, and strongly enough that you can check the arithmetic by hand:
-- 10% of the 500,000 rows are country 'NG', 5% are status 'failed', and EVERY
-- failed row is an NG row.
--
--   what the planner computes:  0.10 x 0.05 x 500,000 =  2,500 rows
--   what is actually there:                              25,000 rows
--
-- It multiplies the two selectivities because it has no reason to believe they
-- are related. They are: once you have filtered to 'failed', the country
-- predicate removes nothing at all.
--
-- PREDICT FIRST: the estimate, the actual, and the ratio between them. That
-- ratio is the most diagnostic number in any plan, because every choice made
-- downstream of this node -- join order, join algorithm, whether to sort or
-- hash -- was made on the strength of the estimate and not on the truth.
-- STANDALONE: plan_drill.py builds this table for you, but the file has to run
-- on its own in psql too, so it builds it here if it is not already there. The
-- IF NOT EXISTS means running the program and running this file do not fight.
-- Drop it again with: DROP TABLE plan_drill_corr;
CREATE TABLE IF NOT EXISTS plan_drill_corr AS
SELECT g AS id,
       CASE WHEN mod(g, 100) < 10 THEN 'NG'
            WHEN mod(g, 100) < 40 THEN 'US'
            WHEN mod(g, 100) < 70 THEN 'GB'
            ELSE 'KE' END AS country,
       CASE WHEN mod(g, 100) < 5 THEN 'failed'      -- every failure is an NG row
            ELSE 'complete' END AS status,
       100 + mod(g::bigint * 7919, 90000) AS amount_cents
FROM generate_series(1, 500000) g;
ANALYZE plan_drill_corr;
-- @explain
SELECT count(*), sum(amount_cents)
FROM plan_drill_corr
WHERE country = 'NG' AND status = 'failed';
