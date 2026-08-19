-- Write skew by hand, session B. See session A's file for the setup and the rule
-- about not running these with -f.

-- step 2 (B)
BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE;

-- step 4 (B): B reads the same rows, in its own snapshot, and also sees two.
SELECT count(*) FROM oncall WHERE shift_id = 1 AND on_call;

-- step 6 (B): B writes a DIFFERENT row. No row-level conflict exists, so this
-- does not block -- at any isolation level.
UPDATE oncall SET on_call = false WHERE shift_id = 1 AND doctor_id = 2;

-- step 8 (B): at SERIALIZABLE this fails here, at COMMIT, with
--   ERROR: could not serialize access due to read/write dependencies among transactions
--   DETAIL: Reason code: Canceled on identification as a pivot, during commit attempt.
-- At REPEATABLE READ and READ COMMITTED it succeeds and the shift is left empty.
COMMIT;
