-- 5. The same join for a whole MONTH. ~28,000 orders, ~83,000 line items.
--
-- PREDICT FIRST: same two tables, same join column, 1,400x more outer rows than
-- query 4. Does the planner still nest-loop it?
--
-- Do not assume it flips here. A nested loop over 28,000 outer rows is 28,000
-- index lookups into line_items, and when line_items(order_id) is indexed and
-- the table is warm in shared_buffers, those lookups are cheap enough that the
-- nested loop can still win. plan_drill.py runs this query at one month, six
-- months and twelve and prints the join type each time, so you can see where
-- the flip actually happens on YOUR machine rather than where a book says it
-- should.
--
-- Whatever it picks: read `loops=` on the inner node and multiply it by that
-- node's per-loop actual time. If that product is most of the query's total,
-- you have found where the time went, and "why is the outer side that big" is
-- the next question -- not "why is the inner index slow".
-- @explain
SELECT o.status, count(*) AS items, sum(li.price_cents * li.qty) AS cents
FROM orders o
JOIN line_items li ON li.order_id = o.id
WHERE o.created_at >= timestamptz '2024-06-01'
  AND o.created_at <  timestamptz '2024-07-01'
GROUP BY o.status;
