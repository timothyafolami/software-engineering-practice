-- The one lock query to keep. Run it in psql while a migration is stuck.
--
--   psql -d sep_lab_03_data -f 05-locking-and-deadlocks/sql/lock_queue.sql
--   \watch 1                                  -- to keep it refreshing
--
-- WHAT TO LOOK FOR: the `granted` column, and nothing else first. One ungranted
-- ACCESS EXCLUSIVE row with a growing pile of ungranted ACCESS SHARE rows behind
-- it is the entire shape of a migration outage. The `SELECT`s in that pile are
-- individually compatible with the lock currently held -- they are queued behind
-- the ALTER, not behind each other.
--
-- For a live production incident the first query is the one to reach for
-- (`wait_event_type = 'Lock'` across the whole server); the second narrows to
-- one table; the third names who is blocking whom, which is the question you
-- actually need answered before you can decide what to cancel.

\pset pager off
\timing off

\echo '=== 1. everything on this server currently waiting on a lock =================='
SELECT pid, usename, application_name, state, wait_event_type, wait_event,
       now() - query_start AS waiting_for,
       left(query, 60) AS query
FROM pg_stat_activity
WHERE wait_event_type = 'Lock'
ORDER BY query_start;

\echo
\echo '=== 2. the lock queue on `orders`, in request order =========================='
\echo '=== granted = f is the story. Read this column first. ========================'
SELECT a.pid,
       a.application_name,
       a.state,
       a.wait_event_type,
       l.mode,
       l.granted,
       now() - a.query_start AS age,
       left(a.query, 50) AS query
FROM pg_locks l
JOIN pg_stat_activity a USING (pid)
WHERE l.relation = 'orders'::regclass
ORDER BY l.granted DESC, a.query_start;

\echo
\echo '=== 3. who is blocking whom, by pid =========================================='
\echo '=== pg_blocking_pids() resolves the chain for you; the FIRST pid in the ======='
\echo '=== list is the one to cancel, and it is often not the one complaining. ======='
SELECT a.pid,
       pg_blocking_pids(a.pid) AS blocked_by,
       a.application_name,
       a.wait_event_type,
       now() - a.query_start AS waiting_for,
       left(a.query, 50) AS query
FROM pg_stat_activity a
WHERE cardinality(pg_blocking_pids(a.pid)) > 0
ORDER BY a.query_start;

\echo
\echo '-- to end the incident (choose deliberately, and know the difference):'
\echo '--   SELECT pg_cancel_backend(PID);     cancels the statement, keeps the session'
\echo '--   SELECT pg_terminate_backend(PID);  kills the session, rolls back its transaction'
\echo '-- Cancel first. Terminate only when cancelling did not work, because a'
\echo '-- terminated session takes any uncommitted work with it.'
