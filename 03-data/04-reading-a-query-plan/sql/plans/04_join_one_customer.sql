-- 4. orders JOIN line_items for ONE customer. ~20 orders, ~60 line items.
--
-- PREDICT FIRST: a nested loop is correct here -- the outer side is genuinely
-- tiny, so ~20 index lookups on line_items beats hashing a 3-million-row table.
-- Read `loops=` on the inner node and multiply it by the per-loop actual time.
-- That multiplication is the single most common plan-reading mistake there is.
-- @explain
SELECT o.id, o.status, count(li.id) AS items, sum(li.price_cents * li.qty) AS cents
FROM orders o
JOIN line_items li ON li.order_id = o.id
WHERE o.customer_id = 4242
GROUP BY o.id, o.status;
