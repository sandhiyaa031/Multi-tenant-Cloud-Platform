-- test_constraints.sql
-- PostgreSQL / pgAdmin compatible
--
-- Every test intentionally attempts an invalid operation.
-- Expected failures are caught inside PL/pgSQL so the script
-- continues to the next test.

SET search_path TO dbpilot, public;


-- ============================================================
-- TEST 1
-- Second active subscription for same company
-- Expected: UNIQUE violation
-- ============================================================

DO $$
BEGIN
    BEGIN
        INSERT INTO Subscription (
            company_id,
            plan_id,
            start_date,
            status
        )
        SELECT
            company_id,
            plan_id,
            CURRENT_DATE,
            'active'
        FROM Subscription
        WHERE status = 'active'
        LIMIT 1;

        RAISE EXCEPTION 'TEST 1 FAILED: duplicate active subscription was accepted';
    EXCEPTION
        WHEN unique_violation THEN
            RAISE NOTICE 'TEST 1 PASSED: duplicate active subscription rejected.';
    END;
END
$$;


-- ============================================================
-- TEST 2
-- Second current quota policy for same company
-- Expected: UNIQUE violation
-- ============================================================

DO $$
BEGIN
    BEGIN
        INSERT INTO QuotaPolicy (
            company_id,
            cost_threshold,
            delay_threshold_ms
        )
        SELECT
            company_id,
            999.0,
            100
        FROM QuotaPolicy
        WHERE effective_to IS NULL
        LIMIT 1;

        RAISE EXCEPTION 'TEST 2 FAILED: duplicate current quota policy was accepted';
    EXCEPTION
        WHEN unique_violation THEN
            RAISE NOTICE 'TEST 2 PASSED: duplicate current quota policy rejected.';
    END;
END
$$;


-- ============================================================
-- TEST 3
-- ROUTE_TO_REPLICA with NULL replica_id
-- Expected: CHECK violation
-- ============================================================

DO $$
DECLARE
    new_query_id BIGINT;
BEGIN
    BEGIN
        INSERT INTO QueryHistory (
            database_instance_id,
            query_template_hash,
            planning_time_ms,
            execution_time_ms
        )
        SELECT
            database_instance_id,
            'test_route_without_replica',
            1.0,
            1.0
        FROM DatabaseInstance
        LIMIT 1
        RETURNING query_history_id INTO new_query_id;

        INSERT INTO PolicyDecision (
            query_history_id,
            replica_id,
            action
        )
        VALUES (
            new_query_id,
            NULL,
            'ROUTE_TO_REPLICA'
        );

        RAISE EXCEPTION 'TEST 3 FAILED: invalid ROUTE_TO_REPLICA was accepted';
    EXCEPTION
        WHEN check_violation THEN
            RAISE NOTICE 'TEST 3 PASSED: ROUTE_TO_REPLICA without replica rejected.';
    END;
END
$$;


-- ============================================================
-- TEST 4
-- acknowledged_at set while acknowledged_by is NULL
-- Expected: CHECK violation
-- ============================================================

DO $$
BEGIN
    BEGIN
        INSERT INTO Alert (
            company_id,
            severity,
            message,
            acknowledged_by,
            acknowledged_at
        )
        SELECT
            company_id,
            'info',
            'Constraint test',
            NULL,
            now()
        FROM Company
        LIMIT 1;

        RAISE EXCEPTION 'TEST 4 FAILED: invalid acknowledgement state was accepted';
    EXCEPTION
        WHEN check_violation THEN
            RAISE NOTICE 'TEST 4 PASSED: invalid alert acknowledgement rejected.';
    END;
END
$$;


-- ============================================================
-- TEST 5
-- Duplicate TeamMembership
-- Expected: UNIQUE / PRIMARY KEY violation
-- ============================================================

DO $$
BEGIN
    BEGIN
        INSERT INTO TeamMembership (
            team_id,
            user_id,
            role_id
        )
        SELECT
            team_id,
            user_id,
            role_id
        FROM TeamMembership
        LIMIT 1;

        RAISE EXCEPTION 'TEST 5 FAILED: duplicate TeamMembership was accepted';
    EXCEPTION
        WHEN unique_violation THEN
            RAISE NOTICE 'TEST 5 PASSED: duplicate TeamMembership rejected.';
    END;
END
$$;


-- ============================================================
-- TEST 6
-- Invalid AuditLog.entity_type
-- Expected: CHECK violation
-- ============================================================

DO $$
BEGIN
    BEGIN
        INSERT INTO AuditLog (
            company_id,
            action_type,
            entity_type,
            entity_id
        )
        SELECT
            company_id,
            'CREATE',
            'NotARealTable',
            1
        FROM Company
        LIMIT 1;

        RAISE EXCEPTION 'TEST 6 FAILED: invalid entity_type was accepted';
    EXCEPTION
        WHEN check_violation THEN
            RAISE NOTICE 'TEST 6 PASSED: invalid audit entity_type rejected.';
    END;
END
$$;


-- ============================================================
-- TEST 7
-- Delete a company that still has dependent rows
-- Expected: RESTRICT violation
-- ============================================================

DO $$
BEGIN
    BEGIN
        DELETE FROM Company
        WHERE company_id = (
            SELECT company_id
            FROM Company
            LIMIT 1
        );

        RAISE NOTICE 'TEST 7 FAILED: company with dependent rows was deleted.';

    EXCEPTION
        WHEN restrict_violation THEN
            RAISE NOTICE 'TEST 7 PASSED: company deletion was correctly restricted.';
    END;
END
$$;


-- ============================================================
-- TEST 8
-- confidence_score outside [0,1]
-- Expected: CHECK violation
-- ============================================================

DO $$
DECLARE
    new_query_id BIGINT;
BEGIN
    BEGIN
        INSERT INTO QueryHistory (
            database_instance_id,
            query_template_hash,
            planning_time_ms,
            execution_time_ms
        )
        SELECT
            database_instance_id,
            'test_invalid_confidence',
            1.0,
            1.0
        FROM DatabaseInstance
        LIMIT 1
        RETURNING query_history_id INTO new_query_id;

        INSERT INTO Prediction (
            query_history_id,
            planner_estimated_cost,
            corrected_cost,
            confidence_score
        )
        VALUES (
            new_query_id,
            1.0,
            1.0,
            1.5
        );

        RAISE EXCEPTION 'TEST 8 FAILED: invalid confidence score was accepted';
    EXCEPTION
        WHEN check_violation THEN
            RAISE NOTICE 'TEST 8 PASSED: confidence score outside [0,1] rejected.';
    END;
END
$$;


-- ============================================================
-- TEST 9
-- Cold-start prediction with different corrected cost
-- Expected: CHECK violation
-- ============================================================

DO $$
DECLARE
    new_query_id BIGINT;
BEGIN
    BEGIN
        INSERT INTO QueryHistory (
            database_instance_id,
            query_template_hash,
            planning_time_ms,
            execution_time_ms
        )
        SELECT
            database_instance_id,
            'test_invalid_cold_start',
            1.0,
            1.0
        FROM DatabaseInstance
        LIMIT 1
        RETURNING query_history_id INTO new_query_id;

        INSERT INTO Prediction (
            query_history_id,
            planner_estimated_cost,
            corrected_cost,
            confidence_score,
            is_cold_start
        )
        VALUES (
            new_query_id,
            100.0,
            200.0,
            0.5,
            true
        );

        RAISE EXCEPTION 'TEST 9 FAILED: invalid cold-start prediction was accepted';
    EXCEPTION
        WHEN check_violation THEN
            RAISE NOTICE 'TEST 9 PASSED: invalid cold-start prediction rejected.';
    END;
END
$$;

