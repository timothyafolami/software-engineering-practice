-- 2. A created_at range returning about 0.1% of orders (one day out of three
--    years). No index on created_at exists in the lab's base schema, so the
--    honest first answer here is "seq scan, because there is nothing else" --
--    and that is worth seeing before the index is added below.
--
-- PREDICT FIRST: with the index in place, index scan or bitmap heap scan?
-- The rows are physically clustered (created_at is monotonic in the seed's id),
-- which is exactly the condition that makes an index scan cheap.
-- @explain
SELECT count(*), sum(total_cents)
FROM orders
WHERE created_at >= timestamptz '2024-06-01'
  AND created_at <  timestamptz '2024-06-02';
