SET search_path TO dbpilot, public;


-- ============================================================
-- 1. FUNCTIONS
-- ============================================================

-- 1a. Active quota policy for CartNova.
SELECT *
FROM fn_get_active_quota_policy(1);

-- 1b. Nonexistent company should return no matching policy,
-- without raising an exception.
SELECT *
FROM fn_get_active_quota_policy(999);

-- 1c. Resolve company for QueryHistory 1.
SELECT
    fn_resolve_company_for_query_history(1) AS resolved_company_id;


-- ============================================================
-- 2. PROCEDURE: sp_record_query_execution
--    SUCCESS PATH
-- ============================================================

-- Baseline counts.
SELECT
    (SELECT count(*) FROM QueryHistory)   AS qh_before,
    (SELECT count(*) FROM Prediction)     AS pred_before,
    (SELECT count(*) FROM PolicyDecision) AS pd_before;

BEGIN;

CALL sp_record_query_execution(
    1, 1, 1,
    'test_normal_query_low_cost',
    2.0, 20.0,
    480.0, 500.0,
    0.9, false,
    'ALLOW', NULL, 0,
    NULL
);

COMMIT;

-- Verify one query/prediction/decision was created.
SELECT
    (SELECT count(*) FROM QueryHistory)   AS qh_after,
    (SELECT count(*) FROM Prediction)     AS pred_after,
    (SELECT count(*) FROM PolicyDecision) AS pd_after;

SELECT
    query_history_id,
    query_template_hash
FROM QueryHistory
WHERE query_template_hash = 'test_normal_query_low_cost';


-- ============================================================
-- 3. TRANSACTION ATOMICITY
--    INTENTIONAL FAILURE
-- ============================================================

SELECT
    count(*) AS qh_count_before_failure
FROM QueryHistory;

DO $$
BEGIN
    BEGIN
        -- Deliberately invalid:
        -- ROUTE_TO_REPLICA requires a non-NULL replica_id.
        CALL sp_record_query_execution(
            1, 1, 1,
            'test_intentional_failure',
            2.0, 20.0,
            480.0, 500.0,
            0.9, false,
            'ROUTE_TO_REPLICA', NULL, 0,
            NULL
        );

        RAISE NOTICE
            'TEST 3 FAILED: invalid ROUTE_TO_REPLICA call was accepted.';
    EXCEPTION
        WHEN OTHERS THEN
            RAISE NOTICE
                'TEST 3 PASSED: procedure failure was caught and rolled back. Error: %',
                SQLERRM;
    END;
END
$$;

-- The failed procedure must not leave an orphan QueryHistory row.
SELECT
    count(*) AS qh_count_after_failure
FROM QueryHistory;

SELECT
    count(*) AS leaked_rows_should_be_zero
FROM QueryHistory
WHERE query_template_hash = 'test_intentional_failure';


-- ============================================================
-- 4. TRIGGER: trg_prediction_cost_alert
-- ============================================================

-- Baseline.
SELECT
    count(*) AS alert_count_before
FROM Alert
WHERE company_id = 1;


-- 4a. Critical alert.
BEGIN;

CALL sp_record_query_execution(
    1, 1, 1,
    'test_trigger_critical_cost',
    5.0, 300.0,
    9000.0, 25000.0,
    0.6, false,
    'ALLOW', NULL, 0,
    NULL
);

COMMIT;

SELECT
    a.severity,
    a.message,
    qh.query_template_hash
FROM Alert a
JOIN QueryHistory qh
    ON qh.query_history_id = a.source_query_history_id
WHERE qh.query_template_hash = 'test_trigger_critical_cost';


-- 4b. Warning alert.
BEGIN;

CALL sp_record_query_execution(
    1, 1, 1,
    'test_trigger_warning_cost',
    5.0, 100.0,
    9000.0, 12000.0,
    0.7, false,
    'ALLOW', NULL, 0,
    NULL
);

COMMIT;

SELECT
    a.severity,
    qh.query_template_hash
FROM Alert a
JOIN QueryHistory qh
    ON qh.query_history_id = a.source_query_history_id
WHERE qh.query_template_hash = 'test_trigger_warning_cost';


-- 4c. Below threshold: should create no alert.
BEGIN;

CALL sp_record_query_execution(
    1, 1, 1,
    'test_trigger_no_alert',
    5.0, 20.0,
    900.0, 950.0,
    0.9, false,
    'ALLOW', NULL, 0,
    NULL
);

COMMIT;

SELECT
    count(*) AS should_be_zero
FROM Alert a
JOIN QueryHistory qh
    ON qh.query_history_id = a.source_query_history_id
WHERE qh.query_template_hash = 'test_trigger_no_alert';


-- Final Alert count.
SELECT
    count(*) AS alert_count_after
FROM Alert
WHERE company_id = 1;


-- ============================================================
-- 5. TRIGGER: trg_replica_health_alert
-- ============================================================

SELECT
    count(*) AS alert_count_before
FROM Alert
WHERE company_id = 1;


-- 5a. Healthy -> unreachable should create an alert.
BEGIN;

UPDATE ReplicaDatabase
SET health_status = 'unreachable'
WHERE replica_id = 1;

COMMIT;

SELECT
    severity,
    message,
    source_query_history_id
FROM Alert
WHERE company_id = 1
  AND message LIKE 'Replica%'
ORDER BY alert_id DESC
LIMIT 1;


-- 5b. Updating unreachable -> unreachable again should not
-- create another alert.
SELECT
    count(*) AS alert_count_after_first_transition
FROM Alert
WHERE company_id = 1;

BEGIN;

UPDATE ReplicaDatabase
SET health_status = 'unreachable'
WHERE replica_id = 1;

COMMIT;

SELECT
    count(*) AS alert_count_after_duplicate_update
FROM Alert
WHERE company_id = 1;


-- 5c. Restore replica health.
BEGIN;

UPDATE ReplicaDatabase
SET health_status = 'healthy'
WHERE replica_id = 1;

COMMIT;


-- ============================================================
-- 6. TRIGGER: trg_auditlog_immutable
-- ============================================================

-- 6a. UPDATE must fail.
DO $$
BEGIN
    BEGIN
        UPDATE AuditLog
        SET action_type = 'TAMPERED'
        WHERE audit_log_id = 1;

        RAISE NOTICE
            'TEST 6a FAILED: AuditLog UPDATE was accepted.';
    EXCEPTION
        WHEN OTHERS THEN
            RAISE NOTICE
                'TEST 6a PASSED: AuditLog UPDATE was rejected. Error: %',
                SQLERRM;
    END;
END
$$;


-- 6b. DELETE must fail.
DO $$
BEGIN
    BEGIN
        DELETE FROM AuditLog
        WHERE audit_log_id = 1;

        RAISE NOTICE
            'TEST 6b FAILED: AuditLog DELETE was accepted.';
    EXCEPTION
        WHEN OTHERS THEN
            RAISE NOTICE
                'TEST 6b PASSED: AuditLog DELETE was rejected. Error: %',
                SQLERRM;
    END;
END
$$;


-- 6c. Confirm row remains unchanged.
SELECT
    audit_log_id,
    action_type
FROM AuditLog
WHERE audit_log_id = 1;


-- ============================================================
-- 7. PROCEDURE: sp_acknowledge_alert
-- ============================================================

DO $$
DECLARE
    test_alert_id BIGINT;
BEGIN

    -- Find an unacknowledged alert for company 1.
    SELECT alert_id
    INTO test_alert_id
    FROM Alert
    WHERE company_id = 1
      AND acknowledged_at IS NULL
    ORDER BY alert_id
    LIMIT 1;

    IF test_alert_id IS NULL THEN

        RAISE NOTICE
            'TEST 7 SKIPPED: no unacknowledged Alert exists for company 1.';

    ELSE

        RAISE NOTICE
            'TEST 7: using alert_id = %',
            test_alert_id;


        -- 7a. First acknowledgement should succeed.
        CALL sp_acknowledge_alert(test_alert_id, 1);

        RAISE NOTICE
            'TEST 7a PASSED: alert was acknowledged successfully.';


        -- 7b. Re-acknowledgement should fail.
        BEGIN
            CALL sp_acknowledge_alert(test_alert_id, 1);

            RAISE NOTICE
                'TEST 7b FAILED: already acknowledged alert was accepted.';
        EXCEPTION
            WHEN OTHERS THEN
                RAISE NOTICE
                    'TEST 7b PASSED: re-acknowledgement was rejected. Error: %',
                    SQLERRM;
        END;

    END IF;

END
$$;


-- Verify the chosen alert is now acknowledged.
SELECT
    alert_id,
    acknowledged_by,
    acknowledged_at
FROM Alert
WHERE company_id = 1
  AND acknowledged_at IS NOT NULL
ORDER BY alert_id DESC
LIMIT 1;


-- 7c. Nonexistent Alert should fail.
DO $$
BEGIN
    BEGIN
        CALL sp_acknowledge_alert(999999, 1);

        RAISE NOTICE
            'TEST 7c FAILED: nonexistent alert was accepted.';
    EXCEPTION
        WHEN OTHERS THEN
            RAISE NOTICE
                'TEST 7c PASSED: nonexistent alert was rejected. Error: %',
                SQLERRM;
    END;
END
$$;


-- ============================================================
-- FINAL SUMMARY
-- ============================================================

SELECT
    'Functions / Procedures / Triggers test suite completed.' AS result;