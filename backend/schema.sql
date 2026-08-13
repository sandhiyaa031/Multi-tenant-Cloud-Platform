-- schema.sql — Run once, manually, by a superuser/admin role, before
-- workload_runner.py is run for the first time.
--
-- Sets up two roles matching the two-connection design in config.py/db.py:
--   dbpilot_workload_ro — SELECT-only on public (CartNova's business tables)
--   dbpilot_meta_rw     — full rights on dbpilot_meta schema only
--
-- workload_runner.py's ensure_schema() will also CREATE TABLE IF NOT
-- EXISTS the same table on startup as a convenience for local dev, but
-- the role/grant setup below should be applied once by hand.

-- ── Read-only workload role ────────────────────────────────────────────
CREATE ROLE dbpilot_workload_ro LOGIN PASSWORD 'changeme';
GRANT CONNECT ON DATABASE cartnova TO dbpilot_workload_ro;
GRANT USAGE ON SCHEMA public TO dbpilot_workload_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dbpilot_workload_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO dbpilot_workload_ro;
-- Explicitly no INSERT/UPDATE/DELETE/DDL grants of any kind.

-- ── Metadata storage role ───────────────────────────────────────────────
CREATE ROLE dbpilot_meta_rw LOGIN PASSWORD 'changeme';
CREATE SCHEMA IF NOT EXISTS dbpilot_meta AUTHORIZATION dbpilot_meta_rw;
GRANT CONNECT ON DATABASE cartnova TO dbpilot_meta_rw;
GRANT ALL ON SCHEMA dbpilot_meta TO dbpilot_meta_rw;
-- This role has NO privileges on the public schema — it cannot see or
-- touch CartNova's business tables, only dbpilot_meta.

-- ── Results table (kept in sync with storage.py::SCHEMA_SQL) ──────────
CREATE TABLE IF NOT EXISTS dbpilot_meta.workload_runs (
    run_id                     BIGSERIAL PRIMARY KEY,
    collection_session_id      UUID NOT NULL,
    template_id                TEXT NOT NULL,
    template_name               TEXT NOT NULL,
    params                       JSONB NOT NULL,
    sql_text                     TEXT NOT NULL,
    repetition_index              INT NOT NULL,
    executed_at                   TIMESTAMPTZ NOT NULL,
    pg_version                     TEXT NOT NULL,

    planner_startup_cost           DOUBLE PRECISION NOT NULL,
    planner_total_cost              DOUBLE PRECISION NOT NULL,
    planner_estimated_rows           BIGINT NOT NULL,
    actual_rows                       BIGINT NOT NULL,
    actual_loops                       INT NOT NULL,
    planning_time_ms                    DOUBLE PRECISION NOT NULL,
    execution_time_ms                    DOUBLE PRECISION NOT NULL,
    shared_blks_hit                       BIGINT NOT NULL,
    shared_blks_read                       BIGINT NOT NULL,
    jit_enabled                             BOOLEAN NOT NULL,
    parallel_workers_planned                 INT NOT NULL,
    parallel_workers_launched                 INT NOT NULL,
    num_joins                                  INT NOT NULL,
    num_scans                                   INT NOT NULL,
    plan_node_count                              INT NOT NULL,
    plan_depth                                    INT NOT NULL,
    scan_types                                     TEXT[] NOT NULL,
    join_types                                      TEXT[] NOT NULL,
    has_seqscan                                      BOOLEAN NOT NULL,
    has_index_scan                                    BOOLEAN NOT NULL,
    has_bitmap_scan                                    BOOLEAN NOT NULL,
    has_sort                                            BOOLEAN NOT NULL,
    has_hash_agg                                         BOOLEAN NOT NULL,
    has_group_agg                                         BOOLEAN NOT NULL,
    has_nested_loop                                        BOOLEAN NOT NULL,
    has_hash_join                                           BOOLEAN NOT NULL,
    has_merge_join                                           BOOLEAN NOT NULL,

    raw_plan_json                                             JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workload_runs_template_id
    ON dbpilot_meta.workload_runs (template_id);
CREATE INDEX IF NOT EXISTS idx_workload_runs_session
    ON dbpilot_meta.workload_runs (collection_session_id);
