-- One indexed table, one row that /db reads. Small on purpose: this topic
-- is about the container, not the query plan. If /db ever shows up as a
-- sequential scan you are measuring Postgres, not CFS.
CREATE TABLE IF NOT EXISTS lab_rows (
    id      integer PRIMARY KEY,
    payload text NOT NULL
);

INSERT INTO lab_rows (id, payload)
SELECT g, repeat('x', 200)
FROM generate_series(1, 1000) AS g
ON CONFLICT (id) DO NOTHING;
