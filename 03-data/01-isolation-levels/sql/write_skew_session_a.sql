-- Write skew by hand, session A. Run session B in a second terminal, alternating
-- steps in the numbered order. Both files are annotated with which step comes next.
--
--   psql -d sep_lab_03_data -f 01-isolation-levels/sql/write_skew_session_a.sql   -- no: run it interactively
--
-- Do NOT run this file with -f. Open two terminals:
--   terminal 1:  psql -d sep_lab_03_data
--   terminal 2:  psql -d sep_lab_03_data
-- and paste the steps one at a time, alternating between the two files. Running
-- them as scripts would remove the only thing this demonstrates: the interleaving.
--
-- Set up first (either session):
--   TRUNCATE oncall;
--   INSERT INTO oncall (shift_id, doctor_id, on_call)
--   SELECT s, d, true FROM generate_series(1,8) s, generate_series(1,2) d;
--
-- Change SERIALIZABLE to REPEATABLE READ or READ COMMITTED and repeat: the first
-- two both break the invariant, the third does not.

-- step 1 (A)
BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE;

-- step 3 (A): A reads both doctors and sees two on call.
SELECT count(*) FROM oncall WHERE shift_id = 1 AND on_call;

-- step 5 (A): A decides its own write is safe, and writes ITS OWN row.
UPDATE oncall SET on_call = false WHERE shift_id = 1 AND doctor_id = 1;

-- step 7 (A)
COMMIT;

-- step 9 (either): the invariant. 0 means write skew happened.
SELECT shift_id, sum(on_call::int) AS still_on_call
FROM oncall WHERE shift_id = 1 GROUP BY shift_id;

-- step 10 (either): what SSI was tracking while both transactions were open.
-- Run this from a THIRD session between steps 5 and 7 to see it live:
--   SELECT locktype, relation::regclass, mode FROM pg_locks WHERE mode = 'SIReadLock';
