-- Layer 10 lab - schema for the `api` service (topics 3 and 5).
--
-- Deliberately boring: one table with an index and one without, and a
-- function that sleeps. Topic 3 needs a query whose service time you can
-- set exactly, because Little's Law and Kingman are statements about
-- service time distributions and you cannot check them against a query
-- whose duration you do not control.

CREATE TABLE IF NOT EXISTS items (
    id          bigserial PRIMARY KEY,
    tenant_id   int         NOT NULL,
    payload     text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS items_tenant_idx ON items (tenant_id);

-- 200k rows so a sequential scan is slow enough to be a service time and
-- an index lookup is fast enough to be a different one.
INSERT INTO items (tenant_id, payload)
SELECT (g % 97), md5(g::text) || repeat('x', 64)
FROM generate_series(1, 200000) g
ON CONFLICT DO NOTHING;

-- Fixed service time, for the deterministic arm of topic 3's Kingman
-- experiment: Cs^2 = 0.
CREATE OR REPLACE FUNCTION work_fixed(ms int) RETURNS int AS $$
BEGIN
    PERFORM pg_sleep(ms / 1000.0);
    RETURN ms;
END;
$$ LANGUAGE plpgsql;

-- Exponential service time, mean `ms`: Cs^2 = 1. Same mean as work_fixed,
-- and Kingman says the queue in front of it is twice as deep.
CREATE OR REPLACE FUNCTION work_exponential(ms int) RETURNS int AS $$
DECLARE
    d double precision;
BEGIN
    d := -ln(greatest(random(), 1e-9)) * ms / 1000.0;
    PERFORM pg_sleep(least(d, 10.0));
    RETURN (d * 1000)::int;
END;
$$ LANGUAGE plpgsql;
