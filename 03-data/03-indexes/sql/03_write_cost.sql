-- Topic 3, experiment 4 -- what each index costs on the write path, in bytes,
-- in throughput, and in whether HOT is still available to you.
--
--   psql -d sep_lab_03_data -f 03-indexes/sql/03_write_cost.sql
--
-- WHAT IT DEMONSTRATES: the same insert workload run four times against the same
-- table with 0, 1, 3 and 6 indexes on it, and then two UPDATE workloads that
-- differ only in WHICH COLUMN they touch -- one indexed, one not.
--
-- The physical claim being tested: every INSERT writes an entry into every index
-- on the table, and every UPDATE that touches an INDEXED column has to write a
-- new entry into EVERY index, because the new row version lives at a new
-- location that all of them must now point to. An UPDATE that touches only
-- non-indexed columns can use HOT (Heap-Only Tuple) and skip index maintenance
-- entirely -- which is why "just add an index on updated_at" is far more
-- expensive than it looks: it converts your cheapest updates into your most
-- expensive ones.
--
-- WHAT TO LOOK FOR:
--   * rows_per_sec and wal_bytes_per_row across the 0/1/3/6 rows. WAL is the
--     honest currency here -- it is what replication ships and what your disk
--     must absorb, and it is not hidden by a warm cache.
--   * hot_updates in the last table. Not "did it get slower" -- did HOT apply
--     AT ALL. That is a yes/no, and pg_stat_user_tables answers it.
--
-- TWO HONEST LIMITS ON THESE NUMBERS, both deliberate:
--   1. The insert phases run inside one transaction each, so per-batch latency
--      isolates index-maintenance cost and EXCLUDES per-commit fsync. Real
--      inserts pay that too. This measures the part indexes are responsible for.
--   2. wal_bytes is a pg_current_wal_lsn() delta, which is cluster-wide. Run
--      this on an otherwise idle server or another backend's writes land in
--      your number.
--
-- To change the workload size, edit `n_batches` and `rows_per_batch` in the DO
-- block below. psql does not interpolate variables into dollar-quoted bodies,
-- so they are constants on purpose rather than \set values that would silently
-- not apply.

\timing off
\pset pager off

-- Notices from `DROP ... IF EXISTS` would bury the output. RAISE INFO is sent
-- to the client regardless of this setting, so progress lines still appear.
SET client_min_messages = warning;

\echo
\echo '=============================================================================='
\echo ' Topic 3 / experiment 4 -- the write cost of an index'
\echo '=============================================================================='

DROP TABLE IF EXISTS idx_write_cost;

-- fillfactor 90 leaves room on each page for a new row version to land on the
-- SAME page, which is HOT's precondition. At the default 100 a table with no
-- free space cannot do a HOT update even when every rule is otherwise met, and
-- the last section of this script would measure page pressure rather than
-- index maintenance.
CREATE TABLE idx_write_cost (
    id          bigserial PRIMARY KEY,
    customer_id bigint      NOT NULL,
    status      text        NOT NULL,
    country     text        NOT NULL,
    created_at  timestamptz NOT NULL,
    total_cents bigint      NOT NULL,
    sku         text        NOT NULL,
    note        text                     -- never indexed: the HOT-eligible column
) WITH (fillfactor = 90);

CREATE TEMP TABLE write_cost_results (
    n_indexes        int,
    rows_inserted    bigint,
    seconds          double precision,
    rows_per_sec     double precision,
    wal_bytes        bigint,
    wal_bytes_per_row double precision,
    p50_batch_ms     double precision,
    p99_batch_ms     double precision
);
CREATE TEMP TABLE batch_latency (n_indexes int, ms double precision);

DO $body$
DECLARE
    -- The six indexes, added cumulatively. Note the PRIMARY KEY exists on top of
    -- all of these, so "0 indexes" means "0 SECONDARY indexes" -- there is no
    -- such thing as an indexless table with a primary key, and pretending
    -- otherwise would make the 0 row a fiction.
    idx_defs text[] := ARRAY[
        'CREATE INDEX wc_i1 ON idx_write_cost (customer_id)',
        'CREATE INDEX wc_i2 ON idx_write_cost (created_at)',
        'CREATE INDEX wc_i3 ON idx_write_cost (status, created_at)',
        'CREATE INDEX wc_i4 ON idx_write_cost (sku)',
        'CREATE INDEX wc_i5 ON idx_write_cost (country, customer_id)',
        'CREATE INDEX wc_i6 ON idx_write_cost (total_cents)'
    ];
    idx_names text[] := ARRAY['wc_i1','wc_i2','wc_i3','wc_i4','wc_i5','wc_i6'];
    phases    int[]  := ARRAY[0, 1, 3, 6];

    n_batches      int := 100;
    rows_per_batch int := 500;

    k int; i int; b int;
    lsn0 pg_lsn; t_phase timestamptz; t_batch timestamptz;
    elapsed double precision; wal bigint;
BEGIN
    FOREACH k IN ARRAY phases LOOP
        FOR i IN 1..array_length(idx_names, 1) LOOP
            EXECUTE format('DROP INDEX IF EXISTS %I', idx_names[i]);
        END LOOP;
        TRUNCATE idx_write_cost;
        FOR i IN 1..k LOOP
            EXECUTE idx_defs[i];
        END LOOP;

        lsn0    := pg_current_wal_lsn();
        t_phase := clock_timestamp();

        FOR b IN 1..n_batches LOOP
            t_batch := clock_timestamp();
            INSERT INTO idx_write_cost
                (customer_id, status, country, created_at, total_cents, sku, note)
            SELECT 1 + mod(g * 7919, 50000),
                   (ARRAY['complete','pending','refunded','failed'])[1 + mod(g, 4)],
                   (ARRAY['NG','US','GB','GH','KE'])[1 + mod(g, 5)],
                   timestamptz '2024-01-01' + (g || ' seconds')::interval,
                   100 + mod(g * 13, 500000),
                   'SKU-' || lpad(mod(g, 5000)::text, 5, '0'),
                   'note for row ' || g
            FROM generate_series((b - 1) * rows_per_batch + 1, b * rows_per_batch) g;

            INSERT INTO batch_latency
            VALUES (k, extract(epoch FROM clock_timestamp() - t_batch) * 1000);
        END LOOP;

        elapsed := extract(epoch FROM clock_timestamp() - t_phase);
        wal     := pg_wal_lsn_diff(pg_current_wal_lsn(), lsn0);

        INSERT INTO write_cost_results
        SELECT k,
               n_batches::bigint * rows_per_batch,
               elapsed,
               (n_batches::bigint * rows_per_batch) / elapsed,
               wal,
               wal::double precision / (n_batches::bigint * rows_per_batch),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY ms),
               percentile_cont(0.99) WITHIN GROUP (ORDER BY ms)
        FROM batch_latency WHERE n_indexes = k;

        RAISE INFO 'phase % secondary index(es): % rows in %s',
            k, n_batches * rows_per_batch, round(elapsed::numeric, 2);
    END LOOP;
END
$body$;

\echo
\echo '=== insert cost by index count ==============================================='
\echo '(rows_per_sec is 500-row batches inside one transaction: index maintenance'
\echo ' cost, without per-commit fsync. p99 is per BATCH, not per row.)'
SELECT n_indexes,
       rows_inserted,
       round(seconds::numeric, 2)                AS seconds,
       round(rows_per_sec::numeric, 0)           AS rows_per_sec,
       pg_size_pretty(wal_bytes)                 AS wal,
       round(wal_bytes_per_row::numeric, 1)      AS wal_bytes_per_row,
       round(p50_batch_ms::numeric, 2)           AS p50_batch_ms,
       round(p99_batch_ms::numeric, 2)           AS p99_batch_ms,
       round(100.0 * (1 - rows_per_sec /
             max(rows_per_sec) OVER ())::numeric, 1) AS pct_slower_than_best
FROM write_cost_results ORDER BY n_indexes;

\echo
\echo '=== where the space went ====================================================='
SELECT pg_size_pretty(pg_relation_size('idx_write_cost'))        AS heap,
       pg_size_pretty(pg_indexes_size('idx_write_cost'))         AS all_indexes,
       pg_size_pretty(pg_total_relation_size('idx_write_cost'))  AS total;

SELECT indexrelname AS index, pg_size_pretty(pg_relation_size(indexrelid)) AS size
FROM pg_stat_user_indexes WHERE relname = 'idx_write_cost' ORDER BY 1;

-- ---------------------------------------------------------------------------
-- HOT. These run at top level, not inside a DO block, because n_tup_hot_upd is
-- collected by the stats machinery and only becomes visible after the
-- transaction commits and the stats are flushed.
-- ---------------------------------------------------------------------------
\echo
\echo '=== HOT: the same UPDATE workload, two different columns ====================='
\echo '-- table currently carries 6 secondary indexes; `status` is one of them,'
\echo '-- `note` is in none of them. That is the only difference between the runs.'

VACUUM (ANALYZE) idx_write_cost;
SELECT pg_stat_force_next_flush();

CREATE TEMP TABLE hot_marks (label text, n_tup_upd bigint, n_tup_hot_upd bigint,
                             lsn pg_lsn, at timestamptz);

INSERT INTO hot_marks
SELECT 'start', n_tup_upd, n_tup_hot_upd, pg_current_wal_lsn(), clock_timestamp()
FROM pg_stat_user_tables WHERE relname = 'idx_write_cost';

-- Run A: touch an INDEXED column. Every one of the six indexes must be updated.
UPDATE idx_write_cost SET status = 'pending' WHERE id % 10 = 0;

SELECT pg_stat_force_next_flush();
INSERT INTO hot_marks
SELECT 'after indexed-column UPDATE', n_tup_upd, n_tup_hot_upd,
       pg_current_wal_lsn(), clock_timestamp()
FROM pg_stat_user_tables WHERE relname = 'idx_write_cost';

-- Run B: touch a NON-indexed column, same rows, same count.
UPDATE idx_write_cost SET note = note || '!' WHERE id % 10 = 0;

SELECT pg_stat_force_next_flush();
INSERT INTO hot_marks
SELECT 'after non-indexed-column UPDATE', n_tup_upd, n_tup_hot_upd,
       pg_current_wal_lsn(), clock_timestamp()
FROM pg_stat_user_tables WHERE relname = 'idx_write_cost';

\echo
\echo '-- deltas between consecutive marks. hot_updates is the yes/no answer. --------'
SELECT label,
       n_tup_upd     - lag(n_tup_upd)     OVER w AS rows_updated,
       n_tup_hot_upd - lag(n_tup_hot_upd) OVER w AS hot_updates,
       pg_size_pretty(pg_wal_lsn_diff(lsn, lag(lsn) OVER w)) AS wal_generated,
       round(extract(epoch FROM at - lag(at) OVER w)::numeric * 1000, 1) AS ms
FROM hot_marks WINDOW w AS (ORDER BY at) ORDER BY at OFFSET 1;

\echo
\echo 'Read the last two rows against each other. Same row count, same table, same'
\echo 'six indexes. The run that touched an indexed column had to write a new entry'
\echo 'into all six; the run that did not could keep the new row version on the same'
\echo 'page and skip every index. If hot_updates is 0 on BOTH rows, the table has no'
\echo 'free space on its pages -- VACUUM it and run again, that is a precondition'
\echo 'failure, not a result.'

DROP TABLE idx_write_cost;
\echo
\echo 'idx_write_cost dropped -- this script owns its own table and cleans it up.'
