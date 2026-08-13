-- 06_seed_data.sql
--
-- PURPOSE: Minimal but relationship-complete seed data — enough to
-- exercise every FK, every CHECK, every partial unique index, and every
-- join path at least once. Not meant to be "realistic volume," meant to
-- be "provably correct."
--
-- DEPENDENCIES: 00 through 05 must have already run.

SET search_path TO dbpilot, public;

INSERT INTO SubscriptionPlan (plan_name, max_databases, max_queries_per_day, price_monthly) VALUES
    ('Free', 1, 1000, 0.00),
    ('Pro', 5, 50000, 99.00),
    ('Enterprise', 50, 1000000, 999.00);

INSERT INTO Role (role_name) VALUES ('Admin'), ('Developer'), ('Viewer');

INSERT INTO Permission (role_id, permission_name) VALUES
    (1, 'manage_users'), (1, 'manage_billing'), (1, 'view_dashboard'),
    (2, 'view_dashboard'), (2, 'acknowledge_alerts'),
    (3, 'view_dashboard');

INSERT INTO Company (name, industry) VALUES
    ('CartNova', 'E-commerce'),
    ('Northwind Logistics', 'Logistics');

INSERT INTO Team (company_id, team_name) VALUES
    (1, 'Checkout'), (1, 'Inventory'), (2, 'Fleet Ops');

INSERT INTO app_user (company_id, email, password_hash, full_name) VALUES
    (1, 'alice@cartnova.com', 'hash_a', 'Alice Sharma'),
    (1, 'bob@cartnova.com', 'hash_b', 'Bob Iyer'),
    (2, 'carol@northwind.com', 'hash_c', 'Carol Mehta');

INSERT INTO TeamMembership (team_id, user_id, role_id) VALUES
    (1, 1, 1),  -- Alice: Admin on Checkout
    (2, 1, 2),  -- Alice: Developer on Inventory (same user, different role per team)
    (1, 2, 3),  -- Bob: Viewer on Checkout
    (3, 3, 1);  -- Carol: Admin on Fleet Ops

INSERT INTO Subscription (company_id, plan_id, start_date, status) VALUES
    (1, 2, '2025-01-01', 'active'),
    (2, 1, '2025-01-01', 'active');

INSERT INTO DatabaseInstance (company_id, instance_name, host, port, db_name, status) VALUES
    (1, 'cartnova-prod', 'db1.internal', 5432, 'cartnova', 'active'),
    (2, 'northwind-prod', 'db2.internal', 5432, 'northwind', 'active');

INSERT INTO ReplicaDatabase (primary_instance_id, host, port, replication_lag_seconds, health_status, last_checked_at) VALUES
    (1, 'db1-replica.internal', 5432, 0.8, 'healthy', now());

INSERT INTO QuotaPolicy (company_id, cost_threshold, delay_threshold_ms) VALUES
    (1, 10000.0, 500),
    (2, 5000.0, 300);

INSERT INTO QueryHistory (database_instance_id, team_id, user_id, query_template_hash, planning_time_ms, execution_time_ms) VALUES
    (1, 1, 1, 'point_lookup_customer_orders_a1b2c3', 5.863, 81.112),
    (1, 2, 2, 'category_aggregate_full_scan_d4e5f6', 12.4, 210.0),
    (1, NULL, NULL, 'scheduled_nightly_rollup_g7h8i9', 3.1, 44.0);

INSERT INTO Prediction (query_history_id, planner_estimated_cost, corrected_cost, confidence_score, is_cold_start) VALUES
    (1, 9435.0, 11020.5, 0.870, false),
    (2, 4200.0, 4200.0, 0.500, true),   -- cold start: corrected == planner estimate
    (3, 1500.0, 1620.0, 0.930, false);

INSERT INTO PolicyDecision (query_history_id, replica_id, action, delay_ms) VALUES
    (1, NULL, 'ALLOW', 0),
    (2, 1, 'ROUTE_TO_REPLICA', 0),
    (3, NULL, 'DELAY', 250);

INSERT INTO ResourceUsage (database_instance_id, window_start, window_end, avg_cpu_percent, query_count) VALUES
    (1, '2026-08-11 00:00:00+00', '2026-08-11 01:00:00+00', 42.5, 318);

INSERT INTO Alert (company_id, source_query_history_id, severity, message) VALUES
    (1, 2, 'warning', 'Query routed to replica due to high cost'),
    (1, NULL, 'info', 'Nightly rollup completed');

INSERT INTO AuditLog (user_id, company_id, action_type, entity_type, entity_id, action_details) VALUES
    (1, 1, 'CREATE', 'DatabaseInstance', 1, '{"instance_name": "cartnova-prod"}'),
    (1, 1, 'ACKNOWLEDGE', 'Alert', 1, '{}');
