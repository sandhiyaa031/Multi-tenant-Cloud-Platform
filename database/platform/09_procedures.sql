-- 09_procedures.sql
--
-- PURPOSE: Two procedures, each encapsulating real DBPilot multi-step
-- logic that must either fully happen or not happen at all.
--
-- DEPENDENCIES: 00 through 08 (does not call the functions in 08, but
-- lives after them by file-organization convention: functions before
-- the procedures/triggers that might use them).
--
-- PostgreSQL 18 compatible. PROCEDURE (not FUNCTION) is used here
-- specifically because these perform multi-table writes and are invoked
-- with CALL, not SELECT — matching the semantic difference the project
-- spec asks to demonstrate (functions compute/return, procedures act).

SET search_path TO dbpilot, public;

-- ─────────────────────────────────────────────────────────────────────
-- sp_record_query_execution
--
-- PURPOSE: This is the "natural transaction" from the project design
-- doc: QueryHistory -> Prediction -> PolicyDecision must be recorded
-- together. A workload event without a Prediction is meaningless (the
-- whole point of DBPilot is the corrected cost estimate), and a
-- Prediction without a PolicyDecision means governance never actually
-- ran. Wrapping all three inserts in one procedure means a partial
-- write (e.g. QueryHistory saved but Prediction failed) is impossible —
-- either all three rows exist or none do.
--
-- ATOMICITY IN PRACTICE: a bare CALL to a procedure runs as a single
-- implicit transaction (unless the procedure body itself COMMITs,
-- which this one deliberately does NOT do). If the PolicyDecision
-- insert fails its CHECK constraint (action='ROUTE_TO_REPLICA' with a
-- NULL replica_id), PostgreSQL aborts the whole procedure call and the
-- QueryHistory/Prediction inserts that already happened are undone too.
-- See test_functions_procedures_triggers.sql for this exact failure
-- case exercised and verified.
--
-- DEFENSIVE LOGIC: if p_is_cold_start is true, corrected_cost is forced
-- to equal planner_estimated_cost regardless of what was passed in —
-- this mirrors the Prediction table's own CHECK constraint
-- (prediction_check) so a caller mistake surfaces as "the value was
-- corrected" rather than "the whole call failed", while the CHECK
-- constraint remains the actual source of truth/enforcement.
--
-- PARAMETERS:
--   p_database_instance_id, p_team_id, p_user_id, p_query_template_hash,
--   p_planning_time_ms, p_execution_time_ms   -> QueryHistory columns
--   p_planner_estimated_cost, p_corrected_cost,
--   p_confidence_score, p_is_cold_start        -> Prediction columns
--   p_policy_action, p_policy_replica_id,
--   p_policy_delay_ms                           -> PolicyDecision columns
--   INOUT p_query_history_id                     -> returns the new PK
--         to the caller
--
-- EXAMPLE CALL:
--   CALL sp_record_query_execution(
--       1, 1, 1, 'point_lookup_customer_orders_new',
--       4.2, 55.0,
--       9000.0, 9500.0, 0.80, false,
--       'ALLOW', NULL, 0,
--       NULL);
-- ─────────────────────────────────────────────────────────────────────
CREATE PROCEDURE sp_record_query_execution(
    p_database_instance_id   INT,
    p_team_id                INT,
    p_user_id                INT,
    p_query_template_hash    TEXT,
    p_planning_time_ms       DOUBLE PRECISION,
    p_execution_time_ms      DOUBLE PRECISION,
    p_planner_estimated_cost DOUBLE PRECISION,
    p_corrected_cost         DOUBLE PRECISION,
    p_confidence_score       NUMERIC,
    p_is_cold_start          BOOLEAN,
    p_policy_action          TEXT,
    p_policy_replica_id      INT,
    p_policy_delay_ms        INT,
    INOUT p_query_history_id BIGINT DEFAULT NULL
)
LANGUAGE plpgsql
SET search_path = dbpilot, public
AS $$
DECLARE
    v_corrected_cost DOUBLE PRECISION := p_corrected_cost;
BEGIN
    IF p_is_cold_start THEN
        v_corrected_cost := p_planner_estimated_cost;
    END IF;

    INSERT INTO QueryHistory (
        database_instance_id, team_id, user_id, query_template_hash,
        planning_time_ms, execution_time_ms
    ) VALUES (
        p_database_instance_id, p_team_id, p_user_id, p_query_template_hash,
        p_planning_time_ms, p_execution_time_ms
    )
    RETURNING query_history_id INTO p_query_history_id;

    INSERT INTO Prediction (
        query_history_id, planner_estimated_cost, corrected_cost,
        confidence_score, is_cold_start
    ) VALUES (
        p_query_history_id, p_planner_estimated_cost, v_corrected_cost,
        p_confidence_score, p_is_cold_start
    );

    INSERT INTO PolicyDecision (
        query_history_id, replica_id, action, delay_ms
    ) VALUES (
        p_query_history_id, p_policy_replica_id, p_policy_action,
        COALESCE(p_policy_delay_ms, 0)
    );
END;
$$;

COMMENT ON PROCEDURE sp_record_query_execution IS
    'Atomically records QueryHistory + Prediction + PolicyDecision. If '
    'any insert fails its constraints, all three are rolled back — no '
    'partial workload record can exist.';

-- ─────────────────────────────────────────────────────────────────────
-- sp_acknowledge_alert
--
-- PURPOSE: Safely acknowledges an Alert. "Safely" means two things:
--   1. It uses SELECT ... FOR UPDATE to lock the specific Alert row
--      before checking/changing it, so two admins clicking "acknowledge"
--      on the same alert at the same instant cannot both succeed and
--      silently overwrite each other's acknowledged_by — see the
--      concurrency demo in test_concurrency_locking.sql, which calls
--      this exact procedure from two sessions.
--   2. It enforces the business rule "an already-acknowledged alert
--      cannot be re-acknowledged" with a real error, not a silent
--      no-op — the Alert.acknowledged_at/acknowledged_by CHECK
--      constraint only guarantees the two columns are null/non-null
--      together, it does NOT prevent re-acknowledging with a different
--      user, which is a business rule, not a data-integrity rule, so it
--      belongs here rather than as a table CHECK.
--
-- PARAMETERS: p_alert_id BIGINT, p_user_id INT
-- RAISES: exception if the alert does not exist, or is already
--         acknowledged
--
-- EXAMPLE CALL:
--   CALL sp_acknowledge_alert(2, 1);
-- ─────────────────────────────────────────────────────────────────────
CREATE PROCEDURE sp_acknowledge_alert(
    p_alert_id BIGINT,
    p_user_id  INT
)
LANGUAGE plpgsql
SET search_path = dbpilot, public
AS $$
DECLARE
    v_already_acked TIMESTAMPTZ;
BEGIN
    -- FOR UPDATE locks this row until the enclosing transaction ends
    -- (COMMIT or ROLLBACK). A second concurrent CALL against the same
    -- p_alert_id blocks here, on this SELECT, until the first
    -- transaction finishes.
    SELECT acknowledged_at INTO v_already_acked
    FROM Alert
    WHERE alert_id = p_alert_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Alert % does not exist', p_alert_id;
    END IF;

    IF v_already_acked IS NOT NULL THEN
        RAISE EXCEPTION 'Alert % is already acknowledged (at %)',
            p_alert_id, v_already_acked;
    END IF;

    UPDATE Alert
    SET acknowledged_by = p_user_id,
        acknowledged_at = now()
    WHERE alert_id = p_alert_id;
END;
$$;

COMMENT ON PROCEDURE sp_acknowledge_alert IS
    'Locks the target Alert row with SELECT ... FOR UPDATE before '
    'reading/changing it, and raises an explicit exception on '
    're-acknowledgement rather than silently allowing it. This is the '
    'procedure the two-session concurrency demo calls.';
