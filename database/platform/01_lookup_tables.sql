-- 01_lookup_tables.sql
--
-- PURPOSE: Tables with NO foreign keys into tenant data — pure catalogs.
-- These must exist before Subscription and TeamMembership, which
-- reference them.
--
-- ENTITIES: SubscriptionPlan, Role
--
-- DEPENDENCIES: 00_schema_and_extensions.sql
--
-- DBMS CONCEPT: catalog/lookup table pattern; UNIQUE as a candidate key
-- distinct from the surrogate PK.

SET search_path TO dbpilot, public;

CREATE TABLE SubscriptionPlan (
    plan_id             SERIAL PRIMARY KEY,
    plan_name           TEXT NOT NULL UNIQUE,
    max_databases        INT NOT NULL CHECK (max_databases > 0),
    max_queries_per_day   INT NOT NULL CHECK (max_queries_per_day > 0),
    price_monthly          NUMERIC(10, 2) NOT NULL CHECK (price_monthly >= 0)
);

COMMENT ON TABLE SubscriptionPlan IS
    'Catalog of Free/Pro/Enterprise plans. plan_name is a candidate key '
    '(UNIQUE) distinct from the surrogate plan_id PK — good viva example '
    'of "PK vs candidate key".';

CREATE TABLE Role (
    role_id    SERIAL PRIMARY KEY,
    role_name  TEXT NOT NULL UNIQUE
);

COMMENT ON TABLE Role IS
    'Application-level RBAC role catalog (Admin/Developer/Viewer). '
    'Distinct from PostgreSQL server-level roles used by the workload '
    'subsystem (dbpilot_workload_ro, dbpilot_meta_rw) — do not conflate '
    'the two in viva. This Role table is app-layer authorization only.';
