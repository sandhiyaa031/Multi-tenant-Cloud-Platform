-- 001_foundation.sql
-- Control-plane schema: who the customers are and what they manage.
--
--   organization (a DBPilot customer)
--     └─ membership ── user            (M:N, carries the RBAC role)
--     └─ cluster (a managed PostgreSQL data plane)
--          └─ tenant (one of the customer's own customers, sharing that cluster)
--               └─ slo
--
-- Every org-owned table carries org_id so that row-level security (003) can
-- isolate organizations with a single predicate, and composite foreign keys
-- make it impossible for a child row to point at another organization's parent.

CREATE EXTENSION IF NOT EXISTS citext;      -- case-insensitive email / slug
CREATE EXTENSION IF NOT EXISTS btree_gist;  -- "=" inside a GiST exclusion constraint

CREATE SCHEMA cp;

-- Declared low-to-high so that role comparisons (role >= 'OPERATOR') work.
CREATE TYPE cp.org_role AS ENUM ('VIEWER', 'OPERATOR', 'ADMIN');

CREATE TABLE cp.organizations (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL CHECK (length(btrim(name)) BETWEEN 2 AND 80),
    slug        citext NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$'),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cp.users (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email          citext NOT NULL UNIQUE CHECK (email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
    -- NULL means "invited, has not set a password yet"; such a user cannot log in.
    password_hash  text,
    has_password   boolean GENERATED ALWAYS AS (password_hash IS NOT NULL) STORED,
    full_name      text NOT NULL CHECK (length(btrim(full_name)) BETWEEN 1 AND 120),
    is_active      boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cp.memberships (
    org_id      uuid NOT NULL REFERENCES cp.organizations (id) ON DELETE CASCADE,
    user_id     uuid NOT NULL REFERENCES cp.users (id) ON DELETE CASCADE,
    role        cp.org_role NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, user_id)
);
CREATE INDEX memberships_user_idx ON cp.memberships (user_id);

-- Only the SHA-256 of a token is stored, so a database leak does not leak usable tokens.
CREATE TABLE cp.password_reset_tokens (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES cp.users (id) ON DELETE CASCADE,
    token_hash  bytea NOT NULL UNIQUE,
    purpose     text NOT NULL CHECK (purpose IN ('reset', 'invite')),
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX password_reset_tokens_user_idx ON cp.password_reset_tokens (user_id) WHERE used_at IS NULL;

CREATE TABLE cp.clusters (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL REFERENCES cp.organizations (id) ON DELETE RESTRICT,
    name          text NOT NULL CHECK (name ~ '^[a-z][a-z0-9-]{1,38}[a-z0-9]$'),
    pooler_host   text NOT NULL,
    pooler_port   integer NOT NULL CHECK (pooler_port BETWEEN 1 AND 65535),
    database_name text NOT NULL,
    status        text NOT NULL DEFAULT 'PENDING'
                  CHECK (status IN ('PENDING', 'ACTIVE', 'DEGRADED', 'RETIRED')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, name),
    UNIQUE (id, org_id)           -- target of the composite FK from tenants
);

CREATE TABLE cp.tenants (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL,
    cluster_id    uuid NOT NULL,
    name          text NOT NULL CHECK (name ~ '^[a-z][a-z0-9-]{1,38}[a-z0-9]$'),
    -- The PostgreSQL role this tenant connects as on the data plane. One role per
    -- tenant is what lets pg_stat_statements attribute every query to a tenant.
    db_role       text NOT NULL CHECK (db_role ~ '^[a-z][a-z0-9_]{2,62}$'),
    -- Tenants share tables; a tenant owns a contiguous range of warehouse ids.
    warehouse_lo  integer NOT NULL CHECK (warehouse_lo >= 1),
    warehouse_hi  integer NOT NULL,
    profile       text NOT NULL CHECK (profile IN ('STEADY_OLTP', 'BURSTY_OLTP', 'ANALYTICAL', 'MIXED')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    CHECK (warehouse_hi >= warehouse_lo),
    UNIQUE (cluster_id, name),
    UNIQUE (cluster_id, db_role),
    UNIQUE (id, org_id),          -- target of the composite FK from slos
    FOREIGN KEY (cluster_id, org_id) REFERENCES cp.clusters (id, org_id) ON DELETE RESTRICT,
    -- No two tenants in one cluster may own overlapping warehouse ranges. A UNIQUE
    -- constraint cannot express "ranges must not overlap"; an exclusion constraint can.
    CONSTRAINT tenants_no_overlapping_ranges
        EXCLUDE USING gist (cluster_id WITH =, int4range(warehouse_lo, warehouse_hi, '[]') WITH &&)
);
CREATE INDEX tenants_org_idx ON cp.tenants (org_id);

CREATE TABLE cp.slos (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL,
    tenant_id     uuid NOT NULL,
    query_class   text NOT NULL CHECK (query_class IN ('OLTP', 'OLAP')),
    percentile    smallint NOT NULL CHECK (percentile IN (50, 95, 99)),
    threshold_ms  numeric(12, 3) NOT NULL CHECK (threshold_ms > 0),
    -- Fraction of measurement windows that must meet the threshold.
    target_ratio  numeric(5, 4) NOT NULL DEFAULT 0.99 CHECK (target_ratio BETWEEN 0.5 AND 1),
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, query_class, percentile),
    FOREIGN KEY (tenant_id, org_id) REFERENCES cp.tenants (id, org_id) ON DELETE CASCADE
);
CREATE INDEX slos_org_idx ON cp.slos (org_id);
