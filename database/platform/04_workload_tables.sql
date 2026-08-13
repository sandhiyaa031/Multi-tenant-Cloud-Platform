-- 04_workload_tables.sql
--
-- PURPOSE: The analytical core of the platform. QueryHistory is the
-- central fact table; Prediction and PolicyDecision are its strict 1:1
-- extensions.
--
-- ENTITIES: QueryHistory, Prediction, PolicyDecision, ResourceUsage
--
-- DEPENDENCIES: 02_tenant_core.sql (Team, app_user), 03_infra_tables.sql
-- (DatabaseInstance, ReplicaDatabase)
--
-- IMPORTANT LINES:
--   * Prediction/PolicyDecision PK = FK = query_history_id. This IS the
--     1:1 relationship — there is no separate surrogate key (FLAGGED
--     ISSUE 3 resolution). Sharing the primary key is what makes it 1:1
--     rather than 1:M at the schema level.
--   * QueryHistory.query_template_hash is a plain TEXT column, NOT a FK.
--     Query templates live in query_templates.py in the separate Python
--     workload subsystem (a different database entirely) — do not
--     invent a query_templates table here to "complete" the FK. That
--     would be scope creep across two intentionally separate systems.
--   * PolicyDecision's CHECK enforcing "ROUTE_TO_REPLICA implies
--     replica_id NOT NULL" — a conditional business rule expressed as a
--     CHECK, which is a good example of "not everything needs a
--     trigger."
--
-- DBMS CONCEPTS: shared-PK 1:1 relationship, nullable FK (optional
-- participation), CHECK constraints encoding conditional business rules,
-- composite UNIQUE.

SET search_path TO dbpilot, public;

CREATE TABLE QueryHistory (
    query_history_id     BIGSERIAL PRIMARY KEY,
    database_instance_id  INT NOT NULL REFERENCES DatabaseInstance(database_instance_id) ON DELETE RESTRICT,
    team_id                INT REFERENCES Team(team_id) ON DELETE SET NULL,
    user_id                 INT REFERENCES app_user(user_id) ON DELETE SET NULL,
    query_template_hash      TEXT NOT NULL,
    executed_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    planning_time_ms             DOUBLE PRECISION NOT NULL CHECK (planning_time_ms >= 0),
    execution_time_ms              DOUBLE PRECISION NOT NULL CHECK (execution_time_ms >= 0)
);

CREATE INDEX idx_queryhistory_instance_time
    ON QueryHistory (database_instance_id, executed_at);
CREATE INDEX idx_queryhistory_template_hash
    ON QueryHistory (query_template_hash);

COMMENT ON COLUMN QueryHistory.team_id IS
    'Nullable: scheduled/system queries may not map to a human team.';
COMMENT ON COLUMN QueryHistory.user_id IS
    'Nullable: same reasoning as team_id. SET NULL on delete (not '
    'RESTRICT/CASCADE) so deleting a user does not destroy or block '
    'deletion of workload history that references them — the history '
    'just becomes attributionless, which is correct for audit purposes.';

CREATE TABLE Prediction (
    query_history_id       BIGINT PRIMARY KEY REFERENCES QueryHistory(query_history_id) ON DELETE CASCADE,
    planner_estimated_cost   DOUBLE PRECISION NOT NULL,
    corrected_cost             DOUBLE PRECISION NOT NULL,
    confidence_score              NUMERIC(4, 3) NOT NULL CHECK (confidence_score BETWEEN 0 AND 1),
    is_cold_start                   BOOLEAN NOT NULL DEFAULT false,
    CHECK (NOT is_cold_start OR corrected_cost = planner_estimated_cost)
);

COMMENT ON TABLE Prediction IS
    'Strict 1:1 with QueryHistory via shared PK/FK (FLAGGED ISSUE 3). '
    'Every QueryHistory row is assumed to get exactly one Prediction row '
    '(app-layer responsibility to enforce "always insert one" — Postgres '
    'cannot force a matching child row to exist, only that if one exists '
    'it is unique and valid). Cold-start rows still get a real row: '
    'is_cold_start=true forces corrected_cost = planner_estimated_cost '
    'via CHECK, so "no model opinion yet" is represented honestly rather '
    'than left null.';

CREATE TABLE PolicyDecision (
    query_history_id   BIGINT PRIMARY KEY REFERENCES QueryHistory(query_history_id) ON DELETE CASCADE,
    replica_id          INT REFERENCES ReplicaDatabase(replica_id) ON DELETE RESTRICT,
    action                TEXT NOT NULL CHECK (action IN ('ALLOW', 'DELAY', 'ROUTE_TO_REPLICA', 'BLOCK')),
    delay_ms                INT NOT NULL DEFAULT 0 CHECK (delay_ms >= 0),
    decided_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (action <> 'ROUTE_TO_REPLICA' OR replica_id IS NOT NULL)
);

COMMENT ON TABLE PolicyDecision IS
    'Strict 1:1 with QueryHistory, same shared-PK pattern as Prediction. '
    'The trailing CHECK is the "if action = ROUTE_TO_REPLICA then '
    'replica_id must NOT be null" rule from the spec, expressed as a '
    'single-row CHECK rather than a trigger since it only ever needs to '
    'see columns within the same row.';

CREATE TABLE ResourceUsage (
    resource_usage_id       BIGSERIAL PRIMARY KEY,
    database_instance_id     INT NOT NULL REFERENCES DatabaseInstance(database_instance_id) ON DELETE RESTRICT,
    window_start               TIMESTAMPTZ NOT NULL,
    window_end                   TIMESTAMPTZ NOT NULL,
    avg_cpu_percent                 NUMERIC(5, 2) NOT NULL CHECK (avg_cpu_percent BETWEEN 0 AND 100),
    query_count                       INT NOT NULL CHECK (query_count >= 0),
    CHECK (window_end > window_start),
    UNIQUE (database_instance_id, window_start, window_end)
);
