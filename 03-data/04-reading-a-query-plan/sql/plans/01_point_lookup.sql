-- 1. Primary-key point lookup. The cheapest thing a database does.
--
-- PREDICT FIRST: scan type, rows, and how many buffers. Then run it.
-- A point lookup on a btree of a million rows touches the root, one or two
-- internal pages, one leaf, and one heap page -- so the buffer count is a
-- small single-digit number, and it does not grow with the table.
-- @explain
SELECT id, customer_id, status, total_cents
FROM orders
WHERE id = 424242;
