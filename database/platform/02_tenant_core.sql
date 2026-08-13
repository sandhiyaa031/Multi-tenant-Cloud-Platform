-- 02_tenant_core.sql
--
-- PURPOSE: The tenant/identity backbone. Every other table in the system
-- ultimately traces back to Company (directly or through Team/app_user).
--
-- ENTITIES: Company, Team, app_user, TeamMembership, Permission,
-- Subscription
--
-- DEPENDENCIES: 00_schema_and_extensions.sql, 01_lookup_tables.sql
-- (Subscription -> SubscriptionPlan, TeamMembership -> Role)
--
-- IMPORTANT LINES:
--   * Permission's composite PK (role_id, permission_name) — weak entity
--     owned by Role, cannot exist without its owner.
--   * TeamMembership's composite PK (team_id, user_id) — FLAGGED ISSUE 2
--     resolution: no surrogate key, no duplicate uniqueness rule.
--   * The partial unique index on Subscription — FLAGGED ISSUE 4
--     resolution: "one active subscription per company" enforced without
--     blocking historical (non-active) rows.
--
-- DBMS CONCEPTS: 1NF (Permission — atomic values, not a CSV column),
-- 2NF (TeamMembership — composite PK prevents partial dependency),
-- 3NF (Subscription vs SubscriptionPlan — plan_price lives only in
-- SubscriptionPlan, not duplicated onto Subscription), weak entity
-- (Permission), associative entity (TeamMembership), M:N (app_user <->
-- Team via TeamMembership), partial index (Subscription).

SET search_path TO dbpilot, public;

CREATE TABLE Company (
    company_id    SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    industry      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active     BOOLEAN NOT NULL DEFAULT true
);

COMMENT ON COLUMN Company.is_active IS
    'Soft-delete flag. Company rows are never hard-deleted (FLAGGED '
    'ISSUE 5) because every child table below RESTRICTs on company_id — '
    'a hard delete would fail anyway once any workload/audit history '
    'exists, which is the point.';

CREATE TABLE Team (
    team_id      SERIAL PRIMARY KEY,
    company_id   INT NOT NULL REFERENCES Company(company_id) ON DELETE RESTRICT,
    team_name    TEXT NOT NULL,
    UNIQUE (company_id, team_name)
);

CREATE TABLE app_user (
    user_id         SERIAL PRIMARY KEY,
    company_id      INT NOT NULL REFERENCES Company(company_id) ON DELETE RESTRICT,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    full_name       TEXT NOT NULL
);

COMMENT ON COLUMN app_user.password_hash IS
    'Never store plaintext passwords. This column holds a hash (e.g. '
    'bcrypt/argon2) computed by the application layer, never raw input.';

CREATE TABLE Permission (
    role_id           INT NOT NULL REFERENCES Role(role_id) ON DELETE CASCADE,
    permission_name   TEXT NOT NULL,
    PRIMARY KEY (role_id, permission_name)
);

COMMENT ON TABLE Permission IS
    'Weak entity owned by Role. CASCADE is correct here (FLAGGED ISSUE 5 '
    'resolution): a permission row has no meaning without its owning '
    'role. 1NF demonstration: each row is one atomic permission, not a '
    'comma-separated list stuffed into a single Role.permissions column.';

CREATE TABLE TeamMembership (
    team_id     INT NOT NULL REFERENCES Team(team_id) ON DELETE CASCADE,
    user_id     INT NOT NULL REFERENCES app_user(user_id) ON DELETE CASCADE,
    role_id     INT NOT NULL REFERENCES Role(role_id) ON DELETE RESTRICT,
    joined_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (team_id, user_id)
);

COMMENT ON TABLE TeamMembership IS
    'Associative entity resolving the app_user <-> Team M:N relationship. '
    'PK is (team_id, user_id) with no surrogate key (FLAGGED ISSUE 2). '
    'CASCADE on team_id/user_id: a membership cannot outlive the team or '
    'user it links. RESTRICT on role_id: you should not be able to '
    'delete a Role that is still assigned to someone.';

CREATE TABLE Subscription (
    subscription_id   SERIAL PRIMARY KEY,
    company_id        INT NOT NULL REFERENCES Company(company_id) ON DELETE RESTRICT,
    plan_id           INT NOT NULL REFERENCES SubscriptionPlan(plan_id) ON DELETE RESTRICT,
    start_date        DATE NOT NULL,
    end_date          DATE,
    status            TEXT NOT NULL CHECK (status IN ('active', 'cancelled', 'expired')),
    CHECK (end_date IS NULL OR end_date >= start_date)
);

-- FLAGGED ISSUE 4 resolution: partial unique index, not a plain UNIQUE.
-- A plain UNIQUE(company_id) would make a company's SECOND subscription
-- ever (even after the first expired) a constraint violation. This
-- version only forbids two *simultaneously active* subscriptions.
CREATE UNIQUE INDEX ux_subscription_one_active_per_company
    ON Subscription (company_id)
    WHERE status = 'active';

COMMENT ON INDEX ux_subscription_one_active_per_company IS
    'Enforces "one active subscription per company" (business rule from '
    'project spec) via a partial index rather than a table-wide UNIQUE, '
    'so cancelled/expired historical rows are never blocked.';
