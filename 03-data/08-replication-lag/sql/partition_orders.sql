-- Topic 8, experiment 5 -- declarative RANGE partitioning, as its own artifact.
--
--   psql -q -d sep_lab_03_data -f 08-replication-lag/sql/partition_orders.sql
--
-- WHAT THIS IS: the DDL, separated from the measurement so you can read it as a
-- thing rather than as a step. python/partition_pruning.py builds the same
-- structure at 12, 36 and 120 partitions and measures planning time; this file
-- builds the 36-partition monthly version once, so the shape is legible.
--
-- WHY orders_part AND NOT orders: the seeded `orders` table is read by Topics 3,
-- 4 and 6, and converting it to a partitioned table in place would silently
-- change every plan those topics teach. A lab that quietly rewrites its own
-- fixtures is a lab you cannot trust twice.
--
-- THE THREE SHARP EDGES, all visible in the DDL below:
--
--   1. Pruning happens only when the PARTITION KEY is in the WHERE clause and
--      is comparable at plan time. A query filtering on `id` touches every
--      partition, and the plan says `Subplans Removed: 0`.
--   2. Every UNIQUE constraint and PRIMARY KEY must INCLUDE the partition key.
--      `PRIMARY KEY (id)` alone is rejected outright -- see the primary key
--      below, which is (id, created_at) and not because anyone wanted it to be.
--   3. Planning time grows with partition count. At 12 it is nothing. At 120 it
--      is a number you can see, and it is paid on every execution.
--
-- THE PAYOFF, which is why you do it anyway: dropping a month of data becomes
-- `DROP TABLE orders_part_2023_01` -- an instant catalogue operation -- instead
-- of a DELETE that generates a million dead tuples for Topic 2's vacuum to
-- chase around for the next hour.

\set ON_ERROR_STOP on
SET client_min_messages = warning;

DROP TABLE IF EXISTS orders_part CASCADE;

CREATE TABLE orders_part (
    id          bigint      NOT NULL,
    customer_id bigint      NOT NULL,
    status      text        NOT NULL,
    total_cents bigint      NOT NULL,
    created_at  timestamptz NOT NULL,
    -- (id) alone would be rejected: "unique constraint on partitioned table
    -- must include all partitioning columns". This is the constraint you can no
    -- longer enforce the way you wanted, and it is the same shape of loss that
    -- a shard key imposes -- arriving here first, on one node, for free.
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

-- 36 monthly partitions covering the seed's three years, generated rather than
-- typed. In production this is pg_partman's job: partitions have to exist
-- BEFORE the rows that belong in them arrive, and "we forgot to create next
-- month" is a real outage with a real name.
DO $body$
DECLARE
    start_month date := date '2023-01-01';
    i int;
    lo date;
    hi date;
BEGIN
    FOR i IN 0..35 LOOP
        lo := start_month + (i || ' months')::interval;
        hi := start_month + ((i + 1) || ' months')::interval;
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF orders_part FOR VALUES FROM (%L) TO (%L)',
            'orders_part_' || to_char(lo, 'YYYY_MM'), lo, hi);
    END LOOP;

    -- A DEFAULT partition catches rows outside every range. Without one, an
    -- INSERT with a created_at nobody planned for fails at run time. With one,
    -- it succeeds and lands somewhere that will never prune -- and attaching a
    -- new partition later requires scanning the default to prove no row belongs
    -- in it. Both behaviours are defensible; not choosing is not.
    EXECUTE 'CREATE TABLE orders_part_default PARTITION OF orders_part DEFAULT';
END
$body$;

\echo 'copying seeded orders into the partitioned table...'
INSERT INTO orders_part (id, customer_id, status, total_cents, created_at)
SELECT id, customer_id, status, total_cents, created_at FROM orders;

-- Indexes on a partitioned table are created on every partition. This one
-- statement is 37 CREATE INDEX statements, and each one takes a lock on its own
-- partition -- which is why CREATE INDEX on a heavily partitioned table is a
-- migration to plan rather than a line to type.
CREATE INDEX idx_orders_part_customer ON orders_part (customer_id);
ANALYZE orders_part;

\echo
\echo '=== partition sizes (the first five) ========================================='
SELECT c.relname AS partition,
       pg_size_pretty(pg_relation_size(c.oid)) AS size,
       (SELECT count(*) FROM pg_inherits WHERE inhrelid = c.oid) AS is_partition
FROM pg_class c
JOIN pg_inherits i ON i.inhrelid = c.oid
WHERE i.inhparent = 'orders_part'::regclass
ORDER BY c.relname
LIMIT 5;

SELECT count(*) AS total_partitions
FROM pg_inherits WHERE inhparent = 'orders_part'::regclass;

\echo '-- how many rows landed in the DEFAULT partition (i.e. outside every range):'
SELECT count(*) AS rows_in_default FROM orders_part_default;
\echo '-- if that is not zero, those rows have a created_at nobody planned a partition'
\echo '-- for. They are stored correctly and they will never prune, because the default'
\echo '-- partition can contain anything and the planner has to check it every time.'

\echo
\echo '=== 1. filtering on the PARTITION KEY -- expect pruning ======================'
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), sum(total_cents) FROM orders_part
WHERE created_at >= timestamptz '2024-06-01' AND created_at < timestamptz '2024-07-01';

\echo
\echo '=== 2. filtering on id only -- expect every partition to be touched =========='
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*), sum(total_cents) FROM orders_part WHERE id = 424242;

\echo
\echo '=== 3. ORDER BY partition key DESC LIMIT -- does pruning survive it? ========='
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, created_at FROM orders_part ORDER BY created_at DESC LIMIT 10;

\echo
\echo '=== 4. the payoff: dropping a month ========================================='
\echo '-- DROP TABLE orders_part_2023_01;   -- instant, no dead tuples, no vacuum'
\echo '-- vs DELETE FROM orders WHERE created_at < ...  -- ~28,000 dead tuples here,'
\echo '--    and every one of them is Topic 2 work that has to happen later.'
SELECT count(*) AS rows_that_a_DROP_would_remove_instantly
FROM orders_part_2023_01;

\echo
\echo 'orders_part is left in place so you can poke at it in psql -- it is about'
\echo '80 MB. python/partition_pruning.py builds its own table and does not need it.'
\echo 'Drop it with: DROP TABLE orders_part CASCADE;'
