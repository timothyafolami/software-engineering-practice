-- Topic 2's /checkout runs two queries against this. Small on purpose: the
-- point of the topic is the pool queue, not the query plan. Layer 3 owns
-- query plans.
CREATE TABLE IF NOT EXISTS orders (
  id      bigserial PRIMARY KEY,
  sku     text        NOT NULL,
  qty     int         NOT NULL,
  created timestamptz NOT NULL DEFAULT now()
);

INSERT INTO orders (sku, qty)
SELECT 'sku-' || (g % 100), (g % 5) + 1
FROM generate_series(1, 1000) g;

CREATE INDEX IF NOT EXISTS orders_sku_idx ON orders (sku);
