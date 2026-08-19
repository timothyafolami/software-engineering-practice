-- Layer 6 lab - schema.
--
-- Note what is NOT here: an index on orders(customer_id, created_at). That
-- absence is the `missing_index` defect, and `api` creates or drops it at
-- startup depending on DEFECT_DISABLE. Everything else is indexed the way a
-- service of this shape normally would be, so the one missing index is the
-- variable rather than a general lack of care.

CREATE TABLE orders (
    id           bigserial PRIMARY KEY,
    customer_id  text        NOT NULL,
    total_cents  integer     NOT NULL,
    status       text        NOT NULL DEFAULT 'placed',
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE order_items (
    id        bigserial PRIMARY KEY,
    order_id  bigint  NOT NULL REFERENCES orders(id),
    sku       text    NOT NULL,
    qty       integer NOT NULL
);

-- The N+1 defect issues one of these per order, so this index has to exist:
-- without it the N+1 would be slow for two different reasons and Topic 2's
-- measurement would not separate them.
CREATE INDEX idx_order_items_order ON order_items (order_id);

-- The Postgres-backed queue `worker` consumes. `traceparent` is a column
-- because a queue has no headers -- Topic 3's whole point in one DDL line.
CREATE TABLE jobs (
    id          bigserial PRIMARY KEY,
    payload     jsonb       NOT NULL,
    traceparent text,
    state       text        NOT NULL DEFAULT 'pending',
    created_at  timestamptz NOT NULL DEFAULT now(),
    claimed_at  timestamptz
);

CREATE INDEX idx_jobs_pending ON jobs (state, id) WHERE state = 'pending';
