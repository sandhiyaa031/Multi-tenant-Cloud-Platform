-- 005_telemetry.sql
-- What DBPilot observes about a managed cluster, stored per tenant.
--
-- pg_stat_statements on the data plane keeps cumulative counters per
-- (role, query fingerprint). The collector snapshots them periodically and
-- stores the difference between consecutive snapshots: one row here means
-- "tenant T ran fingerprint Q this many times, for this long, in this window".

-- Where the collector reaches the cluster's primary directly (statistics views
-- are not available through the pooler's tenant-only login).
ALTER TABLE cp.clusters
    ADD COLUMN primary_host text,
    ADD COLUMN primary_port integer CHECK (primary_port BETWEEN 1 AND 65535);

-- Normalised query text, stored once per cluster rather than on every stats row.
CREATE TABLE cp.query_fingerprints (
    cluster_id  uuid NOT NULL,
    org_id      uuid NOT NULL,
    queryid     bigint NOT NULL,
    query       text NOT NULL,
    first_seen  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (cluster_id, queryid),
    FOREIGN KEY (cluster_id, org_id) REFERENCES cp.clusters (id, org_id) ON DELETE CASCADE
);

-- Append-only time series. Partitioned by day so that old data is removed by
-- dropping a partition (instant) instead of DELETE (slow, leaves dead rows),
-- and so that queries over a recent window read only recent partitions.
CREATE TABLE cp.query_stats (
    org_id              uuid NOT NULL,
    cluster_id          uuid NOT NULL,
    tenant_id           uuid NOT NULL,
    queryid             bigint NOT NULL,
    window_start        timestamptz NOT NULL,
    window_end          timestamptz NOT NULL,
    calls               bigint NOT NULL CHECK (calls >= 0),
    total_exec_ms       double precision NOT NULL CHECK (total_exec_ms >= 0),
    rows                bigint NOT NULL,
    shared_blks_hit     bigint NOT NULL,
    shared_blks_read    bigint NOT NULL,
    shared_blks_dirtied bigint NOT NULL,
    temp_blks_written   bigint NOT NULL,
    wal_bytes           numeric NOT NULL,
    PRIMARY KEY (tenant_id, queryid, window_end),
    FOREIGN KEY (tenant_id, org_id) REFERENCES cp.tenants (id, org_id) ON DELETE CASCADE,
    CHECK (window_end > window_start)
) PARTITION BY RANGE (window_end);
CREATE INDEX query_stats_cluster_time_idx ON cp.query_stats (cluster_id, window_end);

-- Instance-wide counters for the same windows.
CREATE TABLE cp.instance_stats (
    org_id             uuid NOT NULL,
    cluster_id         uuid NOT NULL,
    window_start       timestamptz NOT NULL,
    window_end         timestamptz NOT NULL,
    xact_commit        bigint NOT NULL,
    xact_rollback      bigint NOT NULL,
    blks_read          bigint NOT NULL,
    blks_hit           bigint NOT NULL,
    temp_bytes         bigint NOT NULL,
    deadlocks          bigint NOT NULL,
    wal_bytes          numeric NOT NULL,
    active_connections integer NOT NULL,       -- gauge at window_end
    replica_lag_bytes  bigint,                 -- gauge; NULL when no replica is attached
    database_bytes     bigint NOT NULL,        -- gauge
    PRIMARY KEY (cluster_id, window_end),
    FOREIGN KEY (cluster_id, org_id) REFERENCES cp.clusters (id, org_id) ON DELETE CASCADE
);

-- Creates the partition for one UTC day if it does not exist yet.
CREATE FUNCTION cp.ensure_query_stats_partition(p_day date) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cp, public, pg_temp AS $$
BEGIN
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS cp.%I PARTITION OF cp.query_stats FOR VALUES FROM (%L) TO (%L)',
        'query_stats_' || to_char(p_day, 'YYYYMMDD'),
        p_day::timestamp AT TIME ZONE 'UTC',
        (p_day + 1)::timestamp AT TIME ZONE 'UTC');
END $$;

-- ── Access ───────────────────────────────────────────────────────────────────

ALTER TABLE cp.query_fingerprints ENABLE ROW LEVEL SECURITY;
ALTER TABLE cp.query_stats        ENABLE ROW LEVEL SECURITY;
ALTER TABLE cp.instance_stats     ENABLE ROW LEVEL SECURITY;

-- Members read their own organization's telemetry.
CREATE POLICY query_fingerprints_read ON cp.query_fingerprints FOR SELECT TO dbpilot_api
    USING (org_id = cp.current_org() AND cp.has_role('VIEWER'));
CREATE POLICY query_stats_read ON cp.query_stats FOR SELECT TO dbpilot_api
    USING (org_id = cp.current_org() AND cp.has_role('VIEWER'));
CREATE POLICY instance_stats_read ON cp.instance_stats FOR SELECT TO dbpilot_api
    USING (org_id = cp.current_org() AND cp.has_role('VIEWER'));
GRANT SELECT ON cp.query_fingerprints, cp.query_stats, cp.instance_stats TO dbpilot_api;
GRANT UPDATE (primary_host, primary_port) ON cp.clusters TO dbpilot_api;

-- The collector is a system service spanning all organizations. It gets its own
-- role with exactly this: read which clusters and tenants exist, append telemetry.
-- It cannot read users, memberships or the audit log, and cannot change resources.
GRANT USAGE ON SCHEMA cp TO dbpilot_collector;
GRANT SELECT ON cp.clusters, cp.tenants TO dbpilot_collector;
GRANT SELECT, INSERT ON cp.query_fingerprints, cp.query_stats, cp.instance_stats TO dbpilot_collector;
REVOKE ALL ON FUNCTION cp.ensure_query_stats_partition(date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cp.ensure_query_stats_partition(date) TO dbpilot_collector;

CREATE POLICY clusters_collector_read ON cp.clusters FOR SELECT TO dbpilot_collector USING (true);
CREATE POLICY tenants_collector_read  ON cp.tenants  FOR SELECT TO dbpilot_collector USING (true);
CREATE POLICY query_fingerprints_collect ON cp.query_fingerprints FOR ALL TO dbpilot_collector
    USING (true) WITH CHECK (true);
CREATE POLICY query_stats_collect ON cp.query_stats FOR ALL TO dbpilot_collector
    USING (true) WITH CHECK (true);
CREATE POLICY instance_stats_collect ON cp.instance_stats FOR ALL TO dbpilot_collector
    USING (true) WITH CHECK (true);
