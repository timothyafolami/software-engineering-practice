-- Topic 3, experiment 2 -- B-tree skip scan, and the cardinality that decides it.
--
--   psql -d sep_lab_03_data -f 03-indexes/sql/01_skip_scan.sql
--
-- WHAT IT DEMONSTRATES: the same query shape -- equality on the TRAILING column
-- of a two-column index, nothing at all constraining the leading one -- against
-- two indexes that differ in exactly one property: how many distinct values the
-- leading column has.
--
--   (status, customer_id)   leading column: 4 distinct values      -> skip scan pays
--   (customer_id, status)   leading column: ~50,000 distinct       -> it does not
--
-- WHAT TO LOOK FOR: `Index Searches: N` in the PG18 plan. That is the probe
-- count. A HANDFUL of searches is a skip scan doing its job -- measured on PG18.6
-- it is one per distinct leading value plus a boundary probe, so four statuses
-- reports 9 on an Index Scan and 5 on an Index Only Scan, not 4. `Index
-- Searches: 1` means the opposite: the scan descended once and read the leaf
-- level straight through -- it did NOT skip.
--
-- The refusal at high cardinality does not have to be a sequential scan. On this
-- seed the two candidates -- a bitmap scan over the whole index, and a parallel
-- sequential scan -- cost within about 1% of each other, so which one case B
-- prints FLIPS between ANALYZE runs on the same unchanged data. What does not
-- flip is the search count: whenever the index is used at all it reports
-- `Index Searches: 1`. Read the search count, not the node name -- that is the
-- part that tells you whether skipping happened.
--
-- REQUIRES POSTGRES 18. Skip scan does not exist before it, and `Index Searches`
-- is not reported before it. On PG17 this script still runs and is still worth
-- running once -- it shows you the pre-18 behaviour that PG18 is an improvement
-- on -- but the headline result is not available, and the version check below
-- says so rather than letting you read a PG17 plan as a PG18 result.

\timing off
\pset pager off

SET random_page_cost = 1.1;         -- SSD. The 4.0 default distorts every plan here.
SET effective_cache_size = '1GB';

\echo
\echo '=============================================================================='
\echo ' Topic 3 / experiment 2 -- skip scan, and the cardinality that decides it'
\echo '=============================================================================='
SELECT version();

SELECT current_setting('server_version_num')::int >= 180000 AS skip_scan_available,
       CASE WHEN current_setting('server_version_num')::int >= 180000
            THEN 'skip scan and Index Searches are both available on this server'
            ELSE 'BLOCKED on this server: needs PG18. unblock: brew install postgresql@18 '
                 'and re-point LAB_DSN at it, or use lab/docker/compose.yml which pins '
                 'postgres:18. Everything below is then the pre-18 baseline, not the result.'
       END AS verdict;

\echo
\echo '-- cardinality of the two candidate leading columns ---------------------------'
SELECT 'status'      AS leading_column, count(DISTINCT status)      AS n_distinct FROM orders
UNION ALL
SELECT 'customer_id' AS leading_column, count(DISTINCT customer_id) AS n_distinct FROM orders;

DROP INDEX IF EXISTS idx_skip_status_cust;
DROP INDEX IF EXISTS idx_skip_cust_status;
DROP INDEX IF EXISTS idx_skip_status_created;

-- ---------------------------------------------------------------------------
-- Case A: low-cardinality leading column. Four distinct statuses, so a scan
-- that skips to each of them in turn does four probes and reads a tight
-- customer_id range inside each.
-- ---------------------------------------------------------------------------
\echo
\echo '=== A. leading column has 4 distinct values: (status, customer_id) ============'
\echo '=== query: WHERE customer_id = 4242   -- nothing constrains status ============'
\echo '-- the seed already indexes orders(customer_id), and that index would serve this'
\echo '-- query outright on ANY version -- so case A would show you a plain index scan'
\echo '-- and tell you nothing about skipping. It is dropped for the duration of this'
\echo '-- case inside a transaction that is ROLLED BACK, which puts it back exactly:'
\echo '-- DDL is transactional in Postgres, and a rollback is safer here than a'
\echo '-- CREATE INDEX at the end that a failed script would never reach.'
BEGIN;
DROP INDEX idx_orders_customer;
CREATE INDEX idx_skip_status_cust ON orders (status, customer_id);
ANALYZE orders;

\echo '-- warm run (the first execution measures the cache, not the plan) --'
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), max(total_cents) FROM orders WHERE customer_id = 4242;
\echo '-- the measured run --'
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), max(total_cents) FROM orders WHERE customer_id = 4242;

ROLLBACK;
\echo '-- rolled back: idx_skip_status_cust is gone and idx_orders_customer is back.'
SELECT count(*) AS idx_orders_customer_restored
FROM pg_indexes WHERE indexname = 'idx_orders_customer';

-- ---------------------------------------------------------------------------
-- Case B: high-cardinality leading column. Same shape -- equality on the
-- trailing column, leading column unconstrained -- but now there are ~50,000
-- distinct leading values to skip through.
-- ---------------------------------------------------------------------------
\echo
\echo '=== B. leading column has ~50,000 distinct values: (customer_id, status) ======'
\echo '=== query: WHERE status = ''failed''   -- nothing constrains customer_id ========'
CREATE INDEX idx_skip_cust_status ON orders (customer_id, status);
ANALYZE orders;

\echo '-- warm run --'
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), max(total_cents) FROM orders WHERE status = 'failed';
\echo '-- the measured run --'
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), max(total_cents) FROM orders WHERE status = 'failed';

\echo
\echo '-- what the planner was comparing against. NOTE: if the plan above came out as'
\echo '-- a BITMAP scan (it and the parallel seq scan are within ~1% here, and which'
\echo '-- one you get flips between ANALYZE runs), then enable_seqscan = off alone'
\echo '-- reprints it unchanged -- it was never a seq scan to begin with. Turning the'
\echo '-- bitmap off too is what exposes the plain index scan underneath, and that'
\echo '-- is the one to compare: same index, no skipping, Index Searches: 1.'
SET enable_seqscan = off;
SET enable_bitmapscan = off;
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), max(total_cents) FROM orders WHERE status = 'failed';
RESET enable_bitmapscan;
RESET enable_seqscan;

DROP INDEX idx_skip_cust_status;
\echo '-- and with no usable index at all: the sequential scan, for scale. If the'
\echo '-- measured run above already chose this plan, that is the tie being decided'
\echo '-- the other way -- compare the costs, not just the shapes.'
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), max(total_cents) FROM orders WHERE status = 'failed';

\echo
\echo '-- one more: a RANGE on the trailing column ----------------------------------'
\echo '-- A widely-repeated claim -- and one an earlier draft of THIS FILE made -- is'
\echo '-- that skip scan is equality-only. On PG18.6 that is false, and the two plans'
\echo '-- below are the disproof: the predicate is a range in both, and the only'
\echo '-- variable is again the cardinality of the LEADING column.'
\echo '--   (status, created_at)      4 distinct leading values  -> it skips'
\echo '--   (customer_id, created_at) ~50,000                    -> it does not'
\echo '-- created_at is the trailing column because no seed index covers it, so'
\echo '-- nothing but the index under test can win.'
CREATE INDEX idx_skip_status_created ON orders (status, created_at);
ANALYZE orders;
\echo '-- low-cardinality leading column, range on the trailing one --'
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), max(total_cents) FROM orders
WHERE created_at > timestamptz '2025-12-01';
DROP INDEX idx_skip_status_created;

CREATE INDEX idx_skip_cust_created ON orders (customer_id, created_at);
ANALYZE orders;
\echo '-- high-cardinality leading column, the SAME range predicate --'
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), max(total_cents) FROM orders
WHERE created_at > timestamptz '2025-12-01';
DROP INDEX idx_skip_cust_created;

\echo
\echo 'Every index this script created has been dropped -- orders comes out as it'
\echo 'went in. Verify:'
SELECT indexname FROM pg_indexes WHERE tablename = 'orders' ORDER BY indexname;
\echo 'Read Index Searches on A vs B, and on the two range cases. A search count of'
\echo '1 is a scan that did not skip, whatever node it is wearing.'
