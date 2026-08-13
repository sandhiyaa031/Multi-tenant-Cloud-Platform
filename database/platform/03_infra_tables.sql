-- 03_infra_tables.sql
--
-- PURPOSE: Represents the customer databases DBPilot monitors, their
-- read replicas, and the governance policy applied to each company.
--
-- ENTITIES: DatabaseInstance, ReplicaDatabase, QuotaPolicy
--
-- DEPENDENCIES: 02_tenant_core.sql (all three FK to Company, either
-- directly or via DatabaseInstance)
--
-- IMPORTANT LINES:
--   * DatabaseInstance deliberately has NO password/credential column —
--     see comment below.
--   * ReplicaDatabase.replication_lag_seconds CHECK >= 0.
--   * QuotaPolicy's partial unique index — FLAGGED ISSUE 4, same pattern
--     as Subscription.
--
-- DBMS CONCEPTS: FK, CHECK constraints, partial unique index,
-- self-contained 1:M (DatabaseInstance -> ReplicaDatabase).

SET search_path TO dbpilot, public;

CREATE TABLE DatabaseInstance (
    database_instance_id   SERIAL PRIMARY KEY,
    company_id              INT NOT NULL REFERENCES Company(company_id) ON DELETE RESTRICT,
    instance_name            TEXT NOT NULL,
    host                      TEXT NOT NULL,
    port                       INT NOT NULL CHECK (port BETWEEN 1 AND 65535),
    db_name                    TEXT NOT NULL,
    status                      TEXT NOT NULL CHECK (status IN ('active', 'paused', 'decommissioned')),
    UNIQUE (company_id, instance_name)
);

COMMENT ON TABLE DatabaseInstance IS
    'Represents a customer''s registered database (e.g. their "cartnova" '
    'DB in the workload subsystem). Deliberately has NO password/'
    'credential column: a real deployment resolves credentials through a '
    'secrets manager (Vault, AWS Secrets Manager, k8s Secret) at runtime, '
    'keyed by database_instance_id — never stored in this table.';

CREATE TABLE ReplicaDatabase (
    replica_id                INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    primary_instance_id        INT NOT NULL REFERENCES DatabaseInstance(database_instance_id) ON DELETE CASCADE,
    host                         TEXT NOT NULL,
    port                          INT NOT NULL CHECK (port BETWEEN 1 AND 65535),
    replication_lag_seconds        NUMERIC(10, 3) NOT NULL DEFAULT 0 CHECK (replication_lag_seconds >= 0),
    health_status                    TEXT NOT NULL CHECK (health_status IN ('healthy', 'degraded', 'unreachable')),
    last_checked_at                    TIMESTAMPTZ
);

COMMENT ON TABLE ReplicaDatabase IS
    'CASCADE on primary_instance_id is correct here: a replica has no '
    'independent meaning once its primary DatabaseInstance is gone.';

CREATE TABLE QuotaPolicy (
    quota_policy_id     SERIAL PRIMARY KEY,
    company_id          INT NOT NULL REFERENCES Company(company_id) ON DELETE RESTRICT,
    cost_threshold      DOUBLE PRECISION NOT NULL CHECK (cost_threshold > 0),
    delay_threshold_ms  INT NOT NULL CHECK (delay_threshold_ms >= 0),
    effective_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    effective_to        TIMESTAMPTZ,
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

-- FLAGGED ISSUE 4 resolution, same pattern as Subscription: "current"
-- means effective_to IS NULL. Historical (closed-out) policy versions
-- are unaffected by this index.
CREATE UNIQUE INDEX ux_quotapolicy_one_current_per_company
    ON QuotaPolicy (company_id)
    WHERE effective_to IS NULL;

COMMENT ON INDEX ux_quotapolicy_one_current_per_company IS
    'Enforces "one current policy per company, history preserved" via a '
    'partial index on the open-ended (effective_to IS NULL) row only.';
