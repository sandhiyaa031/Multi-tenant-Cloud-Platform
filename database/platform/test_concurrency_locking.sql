-- test_concurrency_locking.sql
--
-- WHY THIS CANNOT BE ONE SCRIPT: blocking is a property of two
-- transactions running AT THE SAME TIME. A single script, run
-- top-to-bottom in one session, can never show one statement waiting
-- on another session's lock — there is only ever one session. You must
-- open TWO separate Query Tool tabs in pgAdmin, each connected to
-- dbpilot_platform, and run the two halves below in parallel by hand.
--
-- SCENARIO: two admins both click "acknowledge" on the same Alert at
-- roughly the same moment. sp_acknowledge_alert (09_procedures.sql)
-- uses SELECT ... FOR UPDATE specifically so this cannot silently
-- result in both admins "succeeding" and the second one's click
-- silently overwriting the first admin's acknowledged_by. Instead: one
-- of them waits, then gets a clear error explaining someone already
-- handled it.
--
-- ═══════════════════════════════════════════════════════════════════
-- SETUP (run this once, in EITHER tab, before starting the demo)
-- ═══════════════════════════════════════════════════════════════════

SET search_path TO dbpilot, public;

INSERT INTO Alert (company_id, severity, message)
VALUES (1, 'warning', 'Concurrency demo target alert')
RETURNING alert_id;

-- Note the alert_id returned above. Use that exact number in place of
-- <ALERT_ID> in both Session A and Session B below.


-- ═══════════════════════════════════════════════════════════════════
-- SESSION A — run in pgAdmin Query Tool TAB 1
-- ═══════════════════════════════════════════════════════════════════
-- Run these three statements as ONE execution (select all three lines,
-- hit Execute). The pg_sleep(15) holds this transaction open — and
-- with it, the row lock — for 15 seconds so you have time to switch to
-- Tab 2 and start Session B while Session A is still running.

SET search_path TO dbpilot, public;
BEGIN;
CALL sp_acknowledge_alert(<ALERT_ID>, 1);   -- user_id 1 = Alice
SELECT pg_sleep(15);                         -- hold the lock; switch to Tab 2 now
COMMIT;

-- WHERE THE LOCK IS HELD: the moment CALL sp_acknowledge_alert runs,
-- its internal "SELECT ... FOR UPDATE" row-locks this specific Alert
-- row. The lock is held until COMMIT (or ROLLBACK) — the pg_sleep(15)
-- above is only there to give you a visible window to run Session B
-- while the lock is still held; the lock itself comes from FOR UPDATE,
-- not from the sleep.


-- ═══════════════════════════════════════════════════════════════════
-- SESSION B — run in pgAdmin Query Tool TAB 2, WHILE Session A's
-- pg_sleep(15) is still running
-- ═══════════════════════════════════════════════════════════════════
-- Run this immediately after starting Session A (within the 15-second
-- window). pgAdmin's Query Tool will show this as still executing
-- (spinner / "Query is running") — that visible wait IS the blocking
-- behavior. It will not return until Session A finishes.

SET search_path TO dbpilot, public;
BEGIN;
CALL sp_acknowledge_alert(<ALERT_ID>, 2);   -- user_id 2 = Bob, same alert_id as Session A
COMMIT;

-- WHAT YOU WILL OBSERVE:
--   1. Session B's CALL appears to hang for the remainder of Session
--      A's pg_sleep(15) — this is Session B's own SELECT ... FOR UPDATE
--      blocked, waiting for Session A's transaction to end.
--   2. The instant Session A's COMMIT runs (either the sleep finishes
--      naturally, or you switch back to Tab 1 and manually run COMMIT
--      early), Session B unblocks immediately.
--   3. Session B then raises: "Alert <id> is already acknowledged
--      (at <timestamp from Session A>)" — because after unblocking,
--      Session B's SELECT sees Session A's now-committed update, and
--      sp_acknowledge_alert's own check (acknowledged_at IS NOT NULL)
--      correctly refuses to re-acknowledge it.
--   4. Run COMMIT in Session B's tab too (or ROLLBACK — either is fine,
--      since the CALL already failed and there is nothing to commit).
--
-- WHY THIS MATTERS: without FOR UPDATE, both sessions could read
-- acknowledged_at as NULL at the same time, both pass the "not already
-- acknowledged" check, and both UPDATE — the second UPDATE would win
-- and silently overwrite the first admin's acknowledged_by with no
-- error and no record that two people raced on the same alert. FOR
-- UPDATE turns that silent race into a serialized, correctly-ordered
-- outcome with a clear error for the loser.


-- ═══════════════════════════════════════════════════════════════════
-- VERIFY (run in either tab after both sessions finish)
-- ═══════════════════════════════════════════════════════════════════

SELECT alert_id, acknowledged_by, acknowledged_at
FROM Alert
WHERE message = 'Concurrency demo target alert';

-- Expected: acknowledged_by = 1 (Alice, from Session A). Session B's
-- attempt (user_id 2) was correctly rejected, not silently applied.
