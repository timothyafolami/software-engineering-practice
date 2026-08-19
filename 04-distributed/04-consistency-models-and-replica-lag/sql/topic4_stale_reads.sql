-- Layer 4 Topic 4 -- stale reads on a replica. This file is the deliverable.
--
-- WHAT THIS DEMONSTRATES: the stale-read rate as a function of (apply delay x
-- read-after-write gap), and what each of the two fixes costs.
--
-- HONEST STATUS: this file now returns real rows. It was run 2026-08-19
-- against the compose stack in ../../docker/ -- pg-primary + a real streaming
-- standby with recovery_min_apply_delay, driven by k6. Before that date it had
-- never returned a row and said so here.
--
-- The rw_probe rows live in the COMPOSE PRIMARY, not in the host's
-- sep_lab_04_dist -- that is where the api service writes them. Run it with:
--     APPLY_DELAY=500ms docker compose -p l4-t4 up -d pg-primary pg-standby api
--     GAP_MS=0 docker compose -p l4-t4 run --rm k6 run /scripts/topic4_rw.js
--     psql -h localhost -p 55432 -U postgres -d lab -f sql/topic4_stale_reads.sql
--
-- WHAT THE api SERVICE MUST WRITE: one row per probe. The columns below are the
-- contract; the FastAPI service fills them. The important one is
-- read_in_recovery -- pg_is_in_recovery() captured INSIDE the read path, on
-- every request. It costs nothing, and it is the only way to know your routing
-- is happening rather than both DSNs resolving to the same host. That single
-- omission is the most likely way to "prove" that a bug which exists does not.

CREATE TABLE IF NOT EXISTS rw_probe (
    id                bigserial PRIMARY KEY,
    run_id            text        NOT NULL,   -- one per compose configuration
    fix               text        NOT NULL    -- none | sticky | lsn
                      CHECK (fix IN ('none', 'sticky', 'lsn')),
    apply_delay       text        NOT NULL,   -- APPLY_DELAY as configured
    gap_ms            integer     NOT NULL,   -- read fired this long after write
    entity_id         text        NOT NULL,
    written_value     text        NOT NULL,
    read_value        text,                   -- NULL = the row was not there yet
    write_lsn         pg_lsn      NOT NULL,   -- pg_current_wal_insert_lsn() at commit
    read_replay_lsn   pg_lsn,                 -- pg_last_wal_replay_lsn() at read
    read_replay_ts    timestamptz,            -- pg_last_xact_replay_timestamp() at read
    read_in_recovery  boolean     NOT NULL,   -- pg_is_in_recovery() in the read path
    poll_iterations   integer     NOT NULL DEFAULT 0,   -- fix=lsn only
    fell_back         boolean     NOT NULL DEFAULT false,
    read_ms           numeric     NOT NULL,
    observed_at       timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS rw_probe_run_idx ON rw_probe (run_id, fix);

\echo
\echo === 0. Is the routing real? Nothing below matters if it is not. ===
-- If read_in_recovery is false for every row on a fix=none run, you were reading
-- the PRIMARY and any stale-read rate of 0% is meaningless. Check this first,
-- every time, before looking at a single percentage.
SELECT run_id,
       fix,
       count(*)                                          AS reads,
       count(*) FILTER (WHERE read_in_recovery)          AS on_standby,
       count(*) FILTER (WHERE NOT read_in_recovery)      AS on_primary,
       round(100.0 * count(*) FILTER (WHERE read_in_recovery)
             / greatest(count(*), 1), 1)                 AS pct_on_standby
FROM   rw_probe
GROUP  BY run_id, fix
ORDER  BY run_id, fix;

\echo
\echo === 1. THE RESULT: stale reads by apply delay and read-after-write gap ===
-- A read is STALE when it did not observe the write that preceded it in the same
-- session. Both "row missing" and "row present with an older value" count: the
-- user cannot tell them apart and neither should this query.
SELECT fix,
       apply_delay,
       gap_ms,
       count(*)                                                       AS reads,
       count(*) FILTER (WHERE read_value IS DISTINCT FROM written_value) AS stale,
       round(100.0 * count(*) FILTER (WHERE read_value IS DISTINCT FROM written_value)
             / greatest(count(*), 1), 2)                              AS pct_stale
FROM   rw_probe
GROUP  BY fix, apply_delay, gap_ms
ORDER  BY fix, apply_delay, gap_ms;

\echo
\echo === 2. The cost of Fix A: primary QPS ===
-- Sticky reads work by not using the replica. This query is that sentence as a
-- number, and it is why people do not do it.
SELECT fix,
       apply_delay,
       count(*) FILTER (WHERE NOT read_in_recovery)      AS primary_reads,
       count(*) FILTER (WHERE read_in_recovery)          AS standby_reads,
       round(100.0 * count(*) FILTER (WHERE NOT read_in_recovery)
             / greatest(count(*), 1), 1)                 AS pct_hitting_primary
FROM   rw_probe
GROUP  BY fix, apply_delay
ORDER  BY fix, apply_delay;

\echo
\echo === 3. The cost of Fix B: poll iterations and fallback rate ===
-- If poll_iterations is 1 everywhere, the replica was already caught up and the
-- wait never happened -- raise the apply delay above your poll interval before
-- concluding the token is cheap.
SELECT apply_delay,
       count(*)                                             AS reads,
       count(*) FILTER (WHERE fell_back)                    AS fell_back,
       round(100.0 * count(*) FILTER (WHERE fell_back)
             / greatest(count(*), 1), 1)                    AS pct_fallback,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY poll_iterations) AS polls_p50,
       percentile_disc(0.99) WITHIN GROUP (ORDER BY poll_iterations) AS polls_p99,
       max(poll_iterations)                                 AS polls_max
FROM   rw_probe
WHERE  fix = 'lsn'
GROUP  BY apply_delay
ORDER  BY apply_delay;

\echo
\echo === 4. Read latency, which is the other half of every row in table 1 ===
SELECT fix,
       apply_delay,
       gap_ms,
       round(percentile_disc(0.50) WITHIN GROUP (ORDER BY read_ms), 2) AS p50_ms,
       round(percentile_disc(0.99) WITHIN GROUP (ORDER BY read_ms), 2) AS p99_ms,
       round(max(read_ms), 2)                                          AS max_ms
FROM   rw_probe
GROUP  BY fix, apply_delay, gap_ms
ORDER  BY fix, apply_delay, gap_ms;

\echo
\echo === 5. Observed lag. TIME first -- bytes cannot answer the question. ===
-- The delay-honouring check has to be a TIME measurement:
--   observed_at - read_replay_ts  is what recovery_min_apply_delay sets.
-- Measured 2026-08-19 UNDER CONTINUOUS WRITE LOAD: 534 ms at APPLY_DELAY
-- '500ms', 2051 ms at '2s'. The qualifier is not decoration -- see below.
--
-- BOTH COLUMNS IN THIS TABLE ARE CONFOUNDED, IN DIFFERENT WAYS. Read the
-- fix='lsn' rows to check the setting and ignore the rest, because the lsn
-- reads are the only ones taken at a known moment relative to a write:
--   lsn/500ms -> 512 ms      lsn/2s -> 2010 ms      (tracks the setting)
--   none/500ms -> 2004 ms mean, 7929 ms max         (does not)
-- pg_last_xact_replay_timestamp() is the timestamp of the last transaction the
-- standby REPLAYED. On an idle primary it just ages, so now() - that value
-- grows without bound and reports "lag" on a standby that is perfectly caught
-- up. This is the single most misread replica-lag metric in production
-- monitoring, and the none/500ms row above is it misreading in miniature: 600
-- reads spanning three k6 runs and the quiet gaps between them.
--
-- write_lsn - read_replay_lsn (bytes of WAL) is kept as a secondary column, but
-- READ IT ONLY WITHIN ONE FIX AT ONE OFFERED LOAD. It is confounded by write
-- throughput: a burst of 200 writes in 0.3 s leaves more unreplayed WAL behind
-- a 500 ms delay than a slow trickle leaves behind a 2 s one, so byte-lag can
-- move the WRONG WAY when you raise the delay. That is not the standby failing
-- to honour the setting -- the time column above shows it does.
--
-- For fix='lsn' the byte figure goes NEGATIVE, and that is the fix working:
-- the read blocked until replay passed the session's own write LSN, so replay
-- is ahead of it by the time the row is recorded.
SELECT fix,
       apply_delay,
       count(*) FILTER (WHERE read_replay_ts IS NOT NULL)              AS with_ts,
       round(avg(extract(epoch FROM (observed_at - read_replay_ts))*1000))
                                                                       AS mean_time_lag_ms,
       round(max(extract(epoch FROM (observed_at - read_replay_ts))*1000))
                                                                       AS max_time_lag_ms,
       round(avg(write_lsn - read_replay_lsn))                         AS mean_lag_bytes
FROM   rw_probe
WHERE  read_replay_lsn IS NOT NULL
GROUP  BY fix, apply_delay
ORDER  BY fix, apply_delay;
