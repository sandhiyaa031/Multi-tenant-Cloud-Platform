-- 006_latency.sql
-- Per-tenant transaction latency, computed from the data plane's statement log.
-- pg_stat_statements gives totals and means; percentiles need individual timings.
-- Latency here is server-side: from the start of a transaction's first statement
-- to the end of its COMMIT. It excludes network time and time queued in the pooler.

CREATE TABLE cp.latency_stats (
    org_id        uuid NOT NULL,
    cluster_id    uuid NOT NULL,
    tenant_id     uuid NOT NULL,
    query_class   text NOT NULL CHECK (query_class IN ('OLTP', 'OLAP')),
    window_start  timestamptz NOT NULL,
    window_end    timestamptz NOT NULL,
    txn_count     integer NOT NULL CHECK (txn_count > 0),
    failed_count  integer NOT NULL,
    mean_ms       double precision NOT NULL,
    p50_ms        double precision NOT NULL,
    p95_ms        double precision NOT NULL,
    p99_ms        double precision NOT NULL,
    PRIMARY KEY (tenant_id, query_class, window_end),
    FOREIGN KEY (tenant_id, org_id) REFERENCES cp.tenants (id, org_id) ON DELETE CASCADE
);
CREATE INDEX latency_stats_cluster_time_idx ON cp.latency_stats (cluster_id, window_end);

ALTER TABLE cp.latency_stats ENABLE ROW LEVEL SECURITY;
CREATE POLICY latency_stats_read ON cp.latency_stats FOR SELECT TO dbpilot_api
    USING (org_id = cp.current_org() AND cp.has_role('VIEWER'));
CREATE POLICY latency_stats_collect ON cp.latency_stats FOR ALL TO dbpilot_collector
    USING (true) WITH CHECK (true);
GRANT SELECT ON cp.latency_stats TO dbpilot_api;
GRANT SELECT, INSERT ON cp.latency_stats TO dbpilot_collector;
