-- 10. GROUP BY over a low-cardinality column, whole table.
--
-- PREDICT FIRST: four groups out of a million rows. HashAggregate or GroupAggregate,
-- and does it go parallel? Read `Workers Launched` against `Workers Planned` --
-- they differ when max_parallel_workers is already spoken for, and a plan that
-- planned four workers and launched none is a plan you are misreading if you
-- only look at the estimates.
-- @explain
SELECT status, count(*) AS orders, sum(total_cents) AS cents
FROM orders
GROUP BY status
ORDER BY orders DESC;
