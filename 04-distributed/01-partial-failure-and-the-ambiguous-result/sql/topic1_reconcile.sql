-- Layer 4 · Topic 1 — the reconciliation query.
--
-- WHAT THIS IS FOR: the compose version of this experiment (../docker/) runs a
-- real `ledger` service that writes every accepted charge to Postgres, while k6
-- writes what each request *appeared* to do to `t1_client_attempts`. The experiment
-- is the diff between those two tables, and this file is the diff.
--
-- An ORPHANED CHARGE is a row the server committed that the client recorded as a
-- failure. There is no bug in either side when this happens -- that is the point.
--
-- HONEST STATUS ON THIS MACHINE: this file has been run only against the EMPTY
-- schema it creates below, to confirm it parses and executes. It has never
-- returned a row here and cannot: it needs the compose stack's `ledger` service
-- and k6, the Docker daemon is down and k6 is not installed
-- (python3 ../lab/local/check_env.py). Nothing here is a measurement.
--
-- Run:  psql -d sep_lab_04_dist -f sql/topic1_reconcile.sql
--
-- The in-process programs in this folder need none of this; they hold both sides
-- in memory. This exists for the version driven by k6 through Toxiproxy, where
-- the two sides are genuinely separate processes.

\set ON_ERROR_STOP on

-- The table names carry a t1_ prefix because the whole layer shares ONE scratch
-- database (sep_lab_04_dist -- see ../../lab/README.md). Topic 2 owns an
-- unprefixed `charges` table with a completely different shape, and because
-- these are CREATE TABLE IF NOT EXISTS, an unprefixed name here does not
-- collide loudly -- it silently adopts topic 2's table and then fails on the
-- first query that references a column topic 2 does not have. Topics 6 and 7
-- prefix for the same reason.

CREATE TABLE IF NOT EXISTS t1_charges (
    id             bigserial PRIMARY KEY,
    request_id     text        NOT NULL,   -- the caller's id, NOT unique here on purpose
    toxic          text        NOT NULL,   -- which fault was active for this run
    amount_cents   bigint      NOT NULL,
    committed_at   timestamptz NOT NULL DEFAULT now()
);

-- What the client believed. Written by the k6 script's teardown, one row per
-- attempt, so a retried request contributes several rows.
CREATE TABLE IF NOT EXISTS t1_client_attempts (
    id           bigserial PRIMARY KEY,
    request_id   text        NOT NULL,
    toxic        text        NOT NULL,
    verdict      text        NOT NULL,   -- 'success' | 'safe' | 'ambiguous'
    error_kind   text,                   -- the client library's own name for it
    observed_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS t1_charges_request_idx ON t1_charges (toxic, request_id);
CREATE INDEX IF NOT EXISTS t1_attempts_request_idx ON t1_client_attempts (toxic, request_id);

\echo
\echo '--- what the client thought happened -----------------------------------'
SELECT toxic,
       count(*) FILTER (WHERE verdict = 'success')   AS client_2xx,
       count(*) FILTER (WHERE verdict = 'safe')      AS client_safe_errors,
       count(*) FILTER (WHERE verdict = 'ambiguous') AS client_ambiguous
FROM t1_client_attempts
GROUP BY toxic
ORDER BY toxic;

\echo
\echo '--- what the server actually did ---------------------------------------'
SELECT toxic, count(*) AS ledger_rows, count(DISTINCT request_id) AS distinct_requests
FROM t1_charges
GROUP BY toxic
ORDER BY toxic;

\echo
\echo '--- ORPHANED CHARGES: committed server-side, recorded as failed client-side'
\echo '--- These are the ambiguous outcome, counted. There is no fix at this layer.'
WITH client_verdict AS (
    -- One verdict per request: it succeeded if any attempt succeeded.
    SELECT toxic,
           request_id,
           bool_or(verdict = 'success') AS client_saw_success
    FROM t1_client_attempts
    GROUP BY toxic, request_id
)
SELECT c.toxic,
       count(*)                                   AS orphaned_charges,
       round(100.0 * count(*) / NULLIF(
             (SELECT count(*) FROM t1_charges x WHERE x.toxic = c.toxic), 0), 1) AS pct_of_ledger
FROM t1_charges c
JOIN client_verdict v USING (toxic, request_id)
WHERE NOT v.client_saw_success
GROUP BY c.toxic
ORDER BY c.toxic;

\echo
\echo '--- DUPLICATE CHARGES: the same request id committed more than once ------'
\echo '--- Every one of these was created by a client retry, not by the fault. --'
SELECT toxic, request_id, count(*) AS times_committed
FROM t1_charges
GROUP BY toxic, request_id
HAVING count(*) > 1
ORDER BY count(*) DESC, toxic, request_id
LIMIT 50;

\echo
\echo '--- totals --------------------------------------------------------------'
SELECT toxic,
       count(*)                                        AS ledger_rows,
       count(*) - count(DISTINCT request_id)           AS duplicate_rows
FROM t1_charges
GROUP BY toxic
ORDER BY toxic;
