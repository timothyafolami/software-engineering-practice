-- 8. A function wrapped around an indexed column.
--
-- customers.email carries a UNIQUE index, so an equality lookup on it is a
-- point lookup. Wrap it in lower() and the index is unusable: the index stores
-- email, not lower(email), and the btree has no way to find rows by a value it
-- never sorted on. Every row must be read and lower()'d.
--
-- This is the single most common accidental full scan in application code,
-- because case-insensitive comparison is such a reasonable thing to want.
--
-- PREDICT FIRST: seq scan, and roughly the whole table's buffer count. Then
-- read query 8's second line in plan_drill.py's output -- the same query with
-- `CREATE INDEX ON customers (lower(email))` present, which restores the point
-- lookup because now the index really does store lower(email).
-- @explain
SELECT id, country
FROM customers
WHERE lower(email) = 'user4242@example.com';
