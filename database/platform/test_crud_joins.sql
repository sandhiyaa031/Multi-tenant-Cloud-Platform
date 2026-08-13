-- test_crud_joins.sql
-- DBPilot platform database
-- PostgreSQL / pgAdmin compatible
--
-- Demonstrates:
-- 1. CREATE
-- 2. READ
-- 3. UPDATE
-- 4. DELETE
-- 5. JOIN
-- 6. AGGREGATION
-- 7. Prediction + PolicyDecision 1:1 relationship
-- 8. EXPLAIN for the unacknowledged-alert query
--
-- CRUD changes are wrapped in a transaction and rolled back so the
-- test does not permanently modify the seed database.

SET search_path TO dbpilot, public;


-- ============================================================
-- TEST 1: CREATE
-- Insert a temporary Company.
-- ============================================================

BEGIN;

INSERT INTO Company (
    name,
    industry
)
VALUES (
    'DBPilot CRUD Test Company',
    'Technology'
);

-- Show the inserted row.
SELECT
    company_id,
    name,
    industry,
    is_active
FROM Company
WHERE name = 'DBPilot CRUD Test Company';

ROLLBACK;


-- ============================================================
-- TEST 2: READ
-- Retrieve companies with subscription information.
-- Demonstrates JOIN.
-- ============================================================

SELECT
    c.company_id,
    c.name AS company_name,
    sp.plan_name,
    s.status AS subscription_status
FROM Company c
LEFT JOIN Subscription s
    ON s.company_id = c.company_id
LEFT JOIN SubscriptionPlan sp
    ON sp.plan_id = s.plan_id
ORDER BY c.company_id;


-- ============================================================
-- TEST 3: UPDATE
-- Update a temporary company, then roll it back.
-- ============================================================

BEGIN;

INSERT INTO Company (
    name,
    industry
)
VALUES (
    'DBPilot Update Test',
    'Technology'
);

UPDATE Company
SET industry = 'Cloud Computing'
WHERE name = 'DBPilot Update Test';

SELECT
    company_id,
    name,
    industry
FROM Company
WHERE name = 'DBPilot Update Test';

ROLLBACK;


-- ============================================================
-- TEST 4: DELETE
-- Create a temporary company, then delete it.
-- The rollback makes the whole test non-destructive.
-- ============================================================

BEGIN;

INSERT INTO Company (
    name,
    industry
)
VALUES (
    'DBPilot Delete Test',
    'Temporary'
);

DELETE FROM Company
WHERE name = 'DBPilot Delete Test';

SELECT
    COUNT(*) AS rows_remaining
FROM Company
WHERE name = 'DBPilot Delete Test';

ROLLBACK;


-- ============================================================
-- TEST 5: THREE-WAY JOIN
-- Company -> Team -> TeamMembership -> app_user
-- Shows team membership and users.
-- ============================================================

SELECT
    c.name AS company_name,
    t.team_name,
    u.full_name,
    u.email,
    r.role_name
FROM Company c
JOIN Team t
    ON t.company_id = c.company_id
JOIN TeamMembership tm
    ON tm.team_id = t.team_id
JOIN app_user u
    ON u.user_id = tm.user_id
JOIN Role r
    ON r.role_id = tm.role_id
ORDER BY
    c.name,
    t.team_name,
    u.full_name;


-- ============================================================
-- TEST 6: AGGREGATE QUERY
-- Count queries and calculate average execution time per database.
-- Demonstrates:
-- COUNT()
-- AVG()
-- GROUP BY
-- JOIN
-- ============================================================

SELECT
    d.database_instance_id,
    d.instance_name,
    COUNT(q.query_history_id) AS query_count,
    ROUND(AVG(q.execution_time_ms)::numeric, 2) AS avg_execution_ms
FROM DatabaseInstance d
LEFT JOIN QueryHistory q
    ON q.database_instance_id = d.database_instance_id
GROUP BY
    d.database_instance_id,
    d.instance_name
ORDER BY
    query_count DESC;


-- ============================================================
-- TEST 7: QUERY HISTORY -> PREDICTION -> POLICY DECISION
-- Demonstrates the two shared-primary-key 1:1 relationships.
-- ============================================================

SELECT
    q.query_history_id,
    q.query_template_hash,
    q.planning_time_ms,
    q.execution_time_ms,
    p.planner_estimated_cost,
    p.corrected_cost,
    p.confidence_score,
    p.is_cold_start,
    pd.action,
    pd.delay_ms,
    pd.decided_at
FROM QueryHistory q
JOIN Prediction p
    ON p.query_history_id = q.query_history_id
JOIN PolicyDecision pd
    ON pd.query_history_id = q.query_history_id
ORDER BY q.executed_at DESC;


-- ============================================================
-- TEST 8: RESOURCE USAGE AGGREGATION
-- Demonstrates a dashboard-style summary.
-- ============================================================

SELECT
    d.instance_name,
    r.window_start,
    r.window_end,
    r.avg_cpu_percent,
    r.query_count
FROM ResourceUsage r
JOIN DatabaseInstance d
    ON d.database_instance_id = r.database_instance_id
ORDER BY r.window_start DESC;


-- ============================================================
-- TEST 9: UNACKNOWLEDGED ALERTS
-- This is the query our partial index is intended to support.
-- ============================================================

SELECT
    alert_id,
    company_id,
    severity,
    message,
    acknowledged_at
FROM Alert
WHERE company_id = (
    SELECT company_id
    FROM Company
    LIMIT 1
)
AND acknowledged_at IS NULL;


-- ============================================================
-- TEST 10: EXPLAIN
-- Lets PostgreSQL show which execution plan it chooses for
-- the unacknowledged-alert query.
--
-- IMPORTANT:
-- With a tiny seed database, PostgreSQL may legitimately choose a
-- sequential scan instead of the partial index.
-- That is NOT an error. EXPLAIN is what we are demonstrating here.
-- ============================================================

EXPLAIN
SELECT
    alert_id,
    company_id,
    severity,
    message,
    acknowledged_at
FROM Alert
WHERE company_id = (
    SELECT company_id
    FROM Company
    LIMIT 1
)
AND acknowledged_at IS NULL;


-- ============================================================
-- TEST 11: INDEX EXISTENCE CHECK
-- Confirms that our partial index exists.
-- ============================================================

SELECT
    indexname,
    indexdef
FROM pg_indexes
WHERE schemaname = 'dbpilot'
AND tablename = 'alert'
ORDER BY indexname;