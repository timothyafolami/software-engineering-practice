-- Layer 6 lab - the seed. ~2M orders across 10,000 customers, plus items.
--
-- This runs ONCE, on an empty volume, and takes a few minutes. `docker compose
-- down` keeps it; `docker compose down -v` makes it run again.
--
-- Two million rows is not decoration. A sequential scan over 2M rows takes
-- long enough to own a p99 and short enough that the service still answers,
-- which is exactly the regime where a missing index is hard to spot from a
-- dashboard -- and that is the regime Topic 2 asks you to diagnose.

INSERT INTO orders (customer_id, total_cents, status, created_at)
SELECT
    'cust-' || lpad(((n % 10000) + 1)::text, 5, '0'),
    -- n::bigint is load bearing: generate_series() here yields int4, and
    -- n * 7919 overflows it at n = 271,410 -- long before 2,000,000. Without
    -- the cast the whole seed aborts with "integer out of range" and the
    -- container exits 3 on first boot with an empty database behind it.
    (100 + (n::bigint * 7919) % 90000)::int,
    (ARRAY['placed','shipped','delivered','refunded'])[1 + (n % 4)],
    now() - ((n % 5184000) || ' seconds')::interval   -- spread over ~60 days
FROM generate_series(1, 2000000) AS n;

-- One to three items per order, deterministic so two people running this lab
-- get the same query plans.
INSERT INTO order_items (order_id, sku, qty)
SELECT
    o.id,
    'SKU-' || lpad(((o.id * 13 + i) % 5000)::text, 4, '0'),
    1 + ((o.id + i) % 3)
FROM orders o
CROSS JOIN LATERAL generate_series(1, 1 + (o.id % 3)) AS i;

ANALYZE orders;
ANALYZE order_items;

-- What you should see, and what it means:
--   SELECT count(*) FROM orders;        -- 2,000,000
--   EXPLAIN (ANALYZE, BUFFERS)
--     SELECT * FROM orders WHERE customer_id = 'cust-00001'
--     ORDER BY created_at DESC LIMIT 25;
-- With the `missing_index` defect enabled that plan is a Seq Scan + Sort over
-- 2M rows. With it disabled (which makes `api` create the index at startup) it
-- is an Index Scan returning 25 rows. Run the EXPLAIN both ways before you run
-- any load test: the plan is the explanation, and the latency is only the
-- symptom.
