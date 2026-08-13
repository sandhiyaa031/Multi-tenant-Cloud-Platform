-- 07_views.sql
--
-- PURPOSE: Read-side views that back real DBPilot functionality — the
-- dashboard's "query performance" page, the company health summary, and
-- the billing/active-subscriptions list. These are the kind of queries
-- the application backend will run directly against, rather than
-- reassembling the same joins by hand in every endpoint.
--
-- DEPENDENCIES: 00 through 06 (reads only, no new tables).
--
-- DBMS CONCEPT: views as a stable read interface over a normalized
-- schema — the whole point of normalizing QuotaPolicy/Subscription/etc.
-- out into separate tables is that these views can re-join them however
-- a given screen needs, without denormalizing the base tables.

SET search_path TO dbpilot, public;

-- ─────────────────────────────────────────────────────────────────────
-- vw_query_performance
--
-- One row per QueryHistory entry, with its Prediction and PolicyDecision
-- flattened alongside it. This is the direct backing query for the
-- "query performance" dashboard screen and for ad-hoc "which queries is
-- the planner getting wrong" investigation.
-- ─────────────────────────────────────────────────────────────────────
CREATE VIEW vw_query_performance AS
SELECT
    qh.query_history_id,
    c.company_id,
    c.name                          AS company_name,
    di.instance_name,
    qh.query_template_hash,
    qh.executed_at,
    qh.planning_time_ms,
    qh.execution_time_ms,
    p.planner_estimated_cost,
    p.corrected_cost,
    p.confidence_score,
    p.is_cold_start,
    ROUND((p.corrected_cost - p.planner_estimated_cost)::numeric, 2)
                                     AS cost_correction_delta,
    pd.action                       AS policy_action,
    pd.delay_ms                     AS policy_delay_ms,
    rd.host                         AS routed_replica_host
FROM QueryHistory qh
JOIN DatabaseInstance di ON di.database_instance_id = qh.database_instance_id
JOIN Company c            ON c.company_id = di.company_id
LEFT JOIN Prediction p     ON p.query_history_id = qh.query_history_id
LEFT JOIN PolicyDecision pd ON pd.query_history_id = qh.query_history_id
LEFT JOIN ReplicaDatabase rd ON rd.replica_id = pd.replica_id;

COMMENT ON VIEW vw_query_performance IS
    'LEFT JOINs on Prediction/PolicyDecision on purpose: even though the '
    'app is expected to always insert both (strict 1:1), a view that '
    'INNER JOINs would silently hide a QueryHistory row if that '
    'invariant is ever violated — better to surface a NULL and let '
    'someone notice than to hide the row entirely.';

-- ─────────────────────────────────────────────────────────────────────
-- vw_company_health
--
-- One row per company: current plan, active alert counts by severity,
-- replica health, and recent query volume. Backs the top-level
-- dashboard "company health" card — the first thing an admin sees.
-- ─────────────────────────────────────────────────────────────────────
CREATE VIEW vw_company_health AS
SELECT
    c.company_id,
    c.name                                   AS company_name,
    sp.plan_name,
    COUNT(DISTINCT di.database_instance_id)   AS database_count,
    COUNT(DISTINCT qh.query_history_id)
        FILTER (WHERE qh.executed_at > now() - INTERVAL '24 hours')
                                                AS queries_last_24h,
    COUNT(DISTINCT a.alert_id)
        FILTER (WHERE a.acknowledged_at IS NULL AND a.severity = 'critical')
                                                AS unacknowledged_critical_alerts,
    COUNT(DISTINCT a.alert_id)
        FILTER (WHERE a.acknowledged_at IS NULL)
                                                AS unacknowledged_alerts_total,
    COUNT(DISTINCT rd.replica_id)
        FILTER (WHERE rd.health_status <> 'healthy')
                                                AS unhealthy_replicas
FROM Company c
LEFT JOIN Subscription s   ON s.company_id = c.company_id AND s.status = 'active'
LEFT JOIN SubscriptionPlan sp ON sp.plan_id = s.plan_id
LEFT JOIN DatabaseInstance di ON di.company_id = c.company_id
LEFT JOIN QueryHistory qh      ON qh.database_instance_id = di.database_instance_id
LEFT JOIN Alert a               ON a.company_id = c.company_id
LEFT JOIN ReplicaDatabase rd     ON rd.primary_instance_id = di.database_instance_id
WHERE c.is_active
GROUP BY c.company_id, c.name, sp.plan_name;

COMMENT ON VIEW vw_company_health IS
    'Uses the active-subscription partial unique index implicitly: the '
    'LEFT JOIN to Subscription with status=''active'' can match at most '
    'one row per company because of ux_subscription_one_active_per_company, '
    'so this GROUP BY never double-counts a company from that join.';

-- ─────────────────────────────────────────────────────────────────────
-- vw_active_subscriptions
--
-- One row per company's currently-active subscription with plan
-- details flattened in. Backs the billing screen. Trivial join, but
-- worth having as a named view since "active subscription" is a
-- concept (backed by the partial index) that multiple screens/reports
-- will want, and re-deriving "status = 'active'" everywhere invites
-- someone eventually forgetting the WHERE clause.
-- ─────────────────────────────────────────────────────────────────────
CREATE VIEW vw_active_subscriptions AS
SELECT
    c.company_id,
    c.name              AS company_name,
    sp.plan_name,
    sp.price_monthly,
    sp.max_databases,
    sp.max_queries_per_day,
    s.subscription_id,
    s.start_date
FROM Subscription s
JOIN Company c          ON c.company_id = s.company_id
JOIN SubscriptionPlan sp ON sp.plan_id = s.plan_id
WHERE s.status = 'active';