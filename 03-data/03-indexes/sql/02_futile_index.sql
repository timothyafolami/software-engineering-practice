-- Topic 3, experiment 3 -- the index the planner is right to ignore, and the
-- one habit that tells you which of you is wrong.
--
--   psql -d sep_lab_03_data -f 03-indexes/sql/02_futile_index.sql
--
-- WHAT IT DEMONSTRATES: an index on orders(status), where ~92% of rows are
-- 'complete' and ~1% are 'failed'. The same index, the same query shape, and
-- two opposite correct answers depending only on which value you ask for.
--
-- THE HABIT: when the planner ignores your index, do not add a hint. Run the
-- query, then run it again with `SET enable_seqscan = off`, and compare ACTUAL
-- times:
--
--   forcing the index made it SLOWER  -> the planner was right; your index is
--                                        the problem, not its cost model
--   forcing the index made it FASTER  -> the planner was wrong; your statistics
--                                        or your cost settings are the problem
--
-- That is a two-minute check that answers a question people otherwise argue
-- about for a day, and it works on any query on any server.
--
-- WHAT TO LOOK FOR: the four blocks below are (common value, full index),
-- (common value, forced index), (rare value, full index), and (rare value,
-- partial index). Read the actual times against each other, not the costs.

\timing off
\pset pager off

SET random_page_cost = 1.1;
SET effective_cache_size = '1GB';

\echo
\echo '=============================================================================='
\echo ' Topic 3 / experiment 3 -- the index the planner is right to ignore'
\echo '=============================================================================='

\echo
\echo '-- the distribution this whole experiment turns on ----------------------------'
SELECT status,
       count(*)                                        AS rows,
       round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct
FROM orders GROUP BY status ORDER BY rows DESC;

DROP INDEX IF EXISTS idx_orders_status;
DROP INDEX IF EXISTS idx_orders_status_failed;

CREATE INDEX idx_orders_status ON orders (status);
ANALYZE orders;

SELECT pg_size_pretty(pg_relation_size('idx_orders_status')) AS full_index_size;

-- ---------------------------------------------------------------------------
-- 1. The common value. 92% of the table. An index scan here would produce
--    nearly every row in random heap order -- strictly more work than reading
--    the heap start to finish.
-- ---------------------------------------------------------------------------
\echo
\echo '=== 1. status = ''complete'' (~92% of rows), planner free to choose ============'
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), sum(total_cents) FROM orders WHERE status = 'complete';
\echo '-- measured run --'
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), sum(total_cents) FROM orders WHERE status = 'complete';

\echo
\echo '=== 2. the same query, index forced. Compare ACTUAL time with block 1. ========'
SET enable_seqscan = off;
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), sum(total_cents) FROM orders WHERE status = 'complete';
\echo '-- measured run --'
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), sum(total_cents) FROM orders WHERE status = 'complete';
RESET enable_seqscan;

-- ---------------------------------------------------------------------------
-- 2. The rare value. Same index, same shape, ~1% of rows. Now the index earns
--    its keep -- and this is why "low-cardinality columns are never worth
--    indexing" is too crude a rule to be useful.
-- ---------------------------------------------------------------------------
\echo
\echo '=== 3. status = ''failed'' (~1% of rows), same index, planner free ============='
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), sum(total_cents) FROM orders WHERE status = 'failed';
\echo '-- measured run --'
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), sum(total_cents) FROM orders WHERE status = 'failed';

-- ---------------------------------------------------------------------------
-- 3. The partial index: the same selectivity, at a fraction of the size and a
--    fraction of the write cost, because it only has entries for the rows you
--    actually query. Compare the two index sizes printed below.
-- ---------------------------------------------------------------------------
\echo
\echo '=== 4. partial index WHERE status = ''failed'' ================================='
DROP INDEX idx_orders_status;
CREATE INDEX idx_orders_status_failed ON orders (id) WHERE status = 'failed';
ANALYZE orders;

SELECT pg_size_pretty(pg_relation_size('idx_orders_status_failed')) AS partial_index_size;

EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), sum(total_cents) FROM orders WHERE status = 'failed';
\echo '-- measured run --'
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), sum(total_cents) FROM orders WHERE status = 'failed';

\echo
\echo '-- and the partial index against the common value: it cannot help, by ---------'
\echo '-- construction. The rows are not in it. -------------------------------------'
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), sum(total_cents) FROM orders WHERE status = 'complete';

DROP INDEX idx_orders_status_failed;

\echo
\echo 'Indexes dropped -- orders comes out as it went in.'
\echo
\echo 'The three numbers worth writing down: full index size vs partial index size,'
\echo 'and the ratio between block 1 and block 2 actual times. If block 2 was slower,'
\echo 'you have just watched the planner be right about an index you would have'
\echo 'defended in review.'
