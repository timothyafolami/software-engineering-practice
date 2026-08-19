-- 3. The same query shape, same index, ~40% of the table.
--
-- PREDICT FIRST: this should NOT use the index. Reading 400,000 rows through an
-- index means 400,000 random heap accesses; reading the table start to finish
-- is one sequential pass. The planner switching to a seq scan here is it being
-- right, and 04-reading-a-query-plan/python/flip_threshold.py finds the exact
-- percentage where it changes its mind.
-- @explain
SELECT count(*), sum(total_cents)
FROM orders
WHERE created_at >= timestamptz '2024-10-01';
