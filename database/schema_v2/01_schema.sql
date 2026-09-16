-- 01_schema.sql
-- Implements Phase 1 Design: App and Research Schemas

DROP SCHEMA IF EXISTS app CASCADE;
DROP SCHEMA IF EXISTS research CASCADE;
DROP SCHEMA IF EXISTS telemetry CASCADE;

-- Ensure robust UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Create schemas
CREATE SCHEMA IF NOT EXISTS app;
CREATE SCHEMA IF NOT EXISTS research;
CREATE SCHEMA IF NOT EXISTS telemetry;

CREATE TABLE telemetry.interactive_latency (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    p95_latency_ms REAL NOT NULL,
    qps INTEGER NOT NULL,
    active_analytical_queries INTEGER NOT NULL,
    active_interactive_queries INTEGER NOT NULL
);

-- ====================================================================
-- A. APPLICATION SCHEMA (Tenant-Isolated)
-- ====================================================================

-- 1. Organizations
CREATE TABLE app.organizations (
    org_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) UNIQUE NOT NULL
);

-- 2. Users
CREATE TABLE app.users (
    user_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id UUID NOT NULL REFERENCES app.organizations(org_id) ON DELETE CASCADE,
    email VARCHAR(255) UNIQUE NOT NULL,
    db_role VARCHAR(255) NOT NULL -- Links the user to a PostgreSQL role for RLS
);

-- 3. Security Events (CTU Hornet 65 Niner)
-- Maps precisely to the Zeek flow structure empirically decoded via DuckDB
CREATE TABLE app.security_events (
    event_id BIGSERIAL,
    org_id UUID NOT NULL REFERENCES app.organizations(org_id) ON DELETE CASCADE,
    
    ts TIMESTAMPTZ NOT NULL,                      -- DOUBLE epoch
    uid VARCHAR(128) NOT NULL,                    
    
    id_orig_h INET NOT NULL,                      
    id_orig_p INTEGER NOT NULL,                   
    id_resp_h INET NOT NULL,                      
    id_resp_p INTEGER NOT NULL,                   
    proto VARCHAR(16) NOT NULL,                   
    
    duration REAL,                                -- REAL
    orig_bytes BIGINT,                            -- Upcast to BIGINT to prevent overflow
    resp_bytes BIGINT,                            
    conn_state VARCHAR(32),                       
    orig_pkts INTEGER,
    resp_pkts INTEGER,
    
    source VARCHAR(64),                           
    
    PRIMARY KEY (org_id, event_id)
);

-- 4. Investigations
CREATE TABLE app.investigations (
    inv_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id UUID NOT NULL REFERENCES app.organizations(org_id) ON DELETE CASCADE,
    created_by UUID NOT NULL REFERENCES app.users(user_id),
    title VARCHAR(255) NOT NULL,
    status VARCHAR(50) DEFAULT 'OPEN',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 5. Investigation Evidence
CREATE TABLE app.investigation_evidence (
    evidence_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id UUID NOT NULL REFERENCES app.organizations(org_id) ON DELETE CASCADE,
    inv_id UUID NOT NULL REFERENCES app.investigations(inv_id) ON DELETE CASCADE,
    event_id BIGINT NOT NULL,
    FOREIGN KEY (org_id, event_id) REFERENCES app.security_events(org_id, event_id) ON DELETE CASCADE
);

-- 6. Notes
CREATE TABLE app.notes (
    note_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id UUID NOT NULL REFERENCES app.organizations(org_id) ON DELETE CASCADE,
    inv_id UUID NOT NULL REFERENCES app.investigations(inv_id) ON DELETE CASCADE,
    author_id UUID NOT NULL REFERENCES app.users(user_id),
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 7. Tags and Investigation_Tags
CREATE TABLE app.tags (
    tag_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id UUID NOT NULL REFERENCES app.organizations(org_id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    UNIQUE (org_id, name)
);

CREATE TABLE app.investigation_tags (
    inv_id UUID NOT NULL REFERENCES app.investigations(inv_id) ON DELETE CASCADE,
    tag_id UUID NOT NULL REFERENCES app.tags(tag_id) ON DELETE CASCADE,
    PRIMARY KEY (inv_id, tag_id)
);

-- 8. Audit Log
CREATE TABLE app.audit_log (
    audit_id BIGSERIAL PRIMARY KEY,
    org_id UUID NOT NULL REFERENCES app.organizations(org_id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES app.users(user_id),
    action VARCHAR(255) NOT NULL,
    entity VARCHAR(255) NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ====================================================================
-- B. RESEARCH/EXPERIMENT SCHEMA (Global/No-RLS)
-- ====================================================================

-- 1. Workload Runs
CREATE TABLE research.workload_runs (
    run_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    phase VARCHAR(255) NOT NULL,
    configuration JSONB,
    start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    end_time TIMESTAMP
);

-- 2. Query Observations
CREATE TABLE research.query_observations (
    obs_id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES research.workload_runs(run_id) ON DELETE CASCADE,
    query_type VARCHAR(255) NOT NULL,
    latency_ms DOUBLE PRECISION,
    queue_time_ms DOUBLE PRECISION,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 3. Runtime Measurements
CREATE TABLE research.runtime_measurements (
    measurement_id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES research.workload_runs(run_id) ON DELETE CASCADE,
    active_connections INTEGER,
    io_wait_ms DOUBLE PRECISION,
    cpu_usage_pct DOUBLE PRECISION,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 4. Controller Decisions
CREATE TABLE research.controller_decisions (
    decision_id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES research.workload_runs(run_id) ON DELETE CASCADE,
    action_taken VARCHAR(255),
    trigger_metric VARCHAR(255),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ====================================================================
-- C. HIGY-CONTENTION INDEXING STRATEGY
-- ====================================================================

-- 1. Interactive Index (Fast point-lookups & timeline bounding)
-- Ensures the latency-sensitive /api/investigate workloads query at <10ms
CREATE INDEX idx_sec_events_interactive 
ON app.security_events USING btree (org_id, id_orig_h, ts DESC);

-- 2. Analytical Index (Heavy aggregation sweeping)
-- Ensures /api/aggregate spans massive disk regions causing realistic shared_buffer IO contention
CREATE INDEX idx_sec_events_analytical 
ON app.security_events USING btree (org_id, source, proto);
