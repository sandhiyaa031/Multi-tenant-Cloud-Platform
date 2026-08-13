-- 05_governance_tables.sql
--
-- PURPOSE: Notification and compliance-history tables. Both are
-- deliberately RESTRICT-heavy — this data should never silently vanish.
--
-- ENTITIES: Alert, AuditLog
--
-- DEPENDENCIES: 02_tenant_core.sql (Company, app_user), 04_workload_tables.sql
-- (QueryHistory)
--
-- IMPORTANT LINES:
--   * AuditLog.entity_type CHECK — FLAGGED ISSUE 1 resolution. entity_id
--     itself is NOT a conventional FK (it can't be — it references a
--     different table depending on entity_type). The CHECK constrains
--     which table entity_type is allowed to name; entity_id's
--     referential integrity to that table is enforced at the
--     application layer, not the database layer. Be ready to explain
--     this tradeoff explicitly in viva — it is the correct, standard
--     answer for polymorphic references in a relational (non-document)
--     database, not a modeling mistake.
--   * Alert's acknowledged_at/acknowledged_by CHECK — a two-column
--     conditional constraint within a single row.
--
-- DBMS CONCEPTS: polymorphic association tradeoff, CHECK across two
-- columns in the same row, append-only table pattern (AuditLog has no
-- UPDATE/DELETE grants planned at the app layer — enforced later via
-- RBAC/roles, not in this file).

SET search_path TO dbpilot, public;

CREATE TABLE Alert (
    alert_id                  BIGSERIAL PRIMARY KEY,
    company_id                 INT NOT NULL REFERENCES Company(company_id) ON DELETE RESTRICT,
    source_query_history_id     BIGINT REFERENCES QueryHistory(query_history_id) ON DELETE SET NULL,
    severity                     TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    message                        TEXT NOT NULL,
    acknowledged_by                  INT REFERENCES app_user(user_id) ON DELETE SET NULL,
    acknowledged_at                    TIMESTAMPTZ,
    CHECK ((acknowledged_at IS NULL) = (acknowledged_by IS NULL))
);

COMMENT ON CONSTRAINT alert_check ON Alert IS
    'Placeholder name — Postgres auto-names this; see \d Alert for the '
    'real constraint name. Enforces "if acknowledged_at is non-null, '
    'acknowledged_by must be non-null" AND the reverse, via column '
    'equality on the two nullability states.';

CREATE INDEX idx_alert_unacknowledged
    ON Alert (company_id)
    WHERE acknowledged_at IS NULL;

COMMENT ON INDEX idx_alert_unacknowledged IS
    'Partial index sized to the actual hot-query shape ("show me '
    'unacknowledged alerts for this company"), not the whole table.';

CREATE TABLE AuditLog (
    audit_log_id     BIGSERIAL PRIMARY KEY,
    user_id            INT REFERENCES app_user(user_id) ON DELETE SET NULL,
    company_id           INT NOT NULL REFERENCES Company(company_id) ON DELETE RESTRICT,
    action_type            TEXT NOT NULL,
    entity_type               TEXT NOT NULL CHECK (
        entity_type IN (
            'Company', 'Team', 'app_user', 'DatabaseInstance',
            'ReplicaDatabase', 'QuotaPolicy', 'Subscription',
            'QueryHistory', 'Alert', 'Role', 'Permission'
        )
    ),
    entity_id                   BIGINT NOT NULL,
    action_details                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_auditlog_entity ON AuditLog (entity_type, entity_id);
CREATE INDEX idx_auditlog_company_time ON AuditLog (company_id, created_at);

COMMENT ON COLUMN AuditLog.entity_id IS
    'FLAGGED ISSUE 1: cannot be a conventional FK — the table it points '
    'into varies with entity_type. Referential integrity for entity_id '
    'is an accepted application-layer responsibility in this design, not '
    'database-enforced. State this plainly in viva rather than implying '
    'the DB guarantees it.';
