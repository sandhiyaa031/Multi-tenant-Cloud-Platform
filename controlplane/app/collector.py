"""Telemetry collector: the OBSERVE step.

Every interval, for every registered cluster, it reads PostgreSQL's cumulative
statistics on the data plane and stores per-tenant deltas in the control-plane
database. It runs as its own process with its own least-privilege roles on both
sides: `dbpilot_monitor` (statistics only) on the data plane and
`dbpilot_collector` (append telemetry only) on the control plane.

    python -m app.collector            # loop forever
    python -m app.collector --once     # one snapshot, for tests and debugging
"""
import argparse
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger("dbpilot.collector")

# Cumulative per-statement counters we difference between snapshots.
STATEMENT_COUNTERS = (
    "calls", "total_exec_ms", "rows", "shared_blks_hit", "shared_blks_read",
    "shared_blks_dirtied", "temp_blks_written", "wal_bytes",
)
ZERO_STATEMENT = dict.fromkeys(STATEMENT_COUNTERS, 0)
INSTANCE_COUNTERS = ("xact_commit", "xact_rollback", "blks_read", "blks_hit", "temp_bytes", "deadlocks", "wal_bytes")

STATEMENTS_SQL = """
    SELECT r.rolname AS role, s.queryid, s.query, s.calls, s.total_exec_time AS total_exec_ms, s.rows,
           s.shared_blks_hit, s.shared_blks_read, s.shared_blks_dirtied, s.temp_blks_written, s.wal_bytes
    FROM pg_stat_statements s
    JOIN pg_roles r ON r.oid = s.userid
    WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND s.toplevel AND s.queryid IS NOT NULL
"""
INSTANCE_SQL = """
    SELECT d.xact_commit, d.xact_rollback, d.blks_read, d.blks_hit, d.temp_bytes, d.deadlocks,
           (SELECT wal_bytes FROM pg_stat_wal) AS wal_bytes,
           (SELECT count(*) FROM pg_stat_activity WHERE state = 'active' AND pid <> pg_backend_pid())
               AS active_connections,
           (SELECT max(pg_wal_lsn_diff(sent_lsn, replay_lsn))::bigint FROM pg_stat_replication)
               AS replica_lag_bytes,
           pg_database_size(current_database()) AS database_bytes
    FROM pg_stat_database d
    WHERE d.datname = current_database()
"""


def counter_delta(current: dict, previous: dict | None, fields: tuple[str, ...]) -> dict | None:
    """Difference of cumulative counters between two snapshots.

    Returns None when there is no previous snapshot (nothing to subtract from).
    If any counter went down, the statistics were reset or the entry was evicted
    and re-created, so the current value is everything since that restart and is
    used as the delta. A window with no activity yields None rather than a zero row.
    """
    if previous is None:
        return None
    if any(current[f] < previous[f] for f in fields):
        delta = {f: current[f] for f in fields}
    else:
        delta = {f: current[f] - previous[f] for f in fields}
    return delta if delta[fields[0]] > 0 else None


@dataclass
class ClusterState:
    """The previous snapshot of one cluster, kept in memory between cycles."""

    taken_at: datetime
    statements: dict[tuple[str, int], dict]
    instance: dict


class Collector:
    def __init__(self, control_url: str, monitor_user: str, monitor_password: str):
        self.control_url = control_url
        self.monitor_user = monitor_user
        self.monitor_password = monitor_password
        self.state: dict[UUID, ClusterState] = {}

    def collect_once(self) -> int:
        """Snapshots every reachable cluster. Returns the number of query_stats rows written."""
        written = 0
        with psycopg.connect(self.control_url, row_factory=dict_row) as control:
            clusters = control.execute(
                "SELECT id, org_id, primary_host, primary_port, database_name FROM cp.clusters"
                " WHERE primary_host IS NOT NULL AND status <> 'RETIRED'"
            ).fetchall()
            for cluster in clusters:
                try:
                    written += self._collect_cluster(control, cluster)
                    control.commit()
                except psycopg.Error:
                    # One unreachable cluster must not stop collection for the others.
                    control.rollback()
                    log.exception("collection failed for cluster %s", cluster["id"])
        return written

    def _snapshot(self, cluster: dict) -> ClusterState:
        with psycopg.connect(
            host=cluster["primary_host"], port=cluster["primary_port"] or 5432, dbname=cluster["database_name"],
            user=self.monitor_user, password=self.monitor_password, connect_timeout=5, row_factory=dict_row,
        ) as dp:
            statements = {(r["role"], r["queryid"]): r for r in dp.execute(STATEMENTS_SQL)}
            instance = dp.execute(INSTANCE_SQL).fetchone()
        return ClusterState(taken_at=datetime.now(timezone.utc), statements=statements, instance=instance)

    def _collect_cluster(self, control: psycopg.Connection, cluster: dict) -> int:
        current = self._snapshot(cluster)
        previous = self.state.get(cluster["id"])
        self.state[cluster["id"]] = current
        if previous is None:
            log.info("cluster %s: baseline snapshot taken (%d statements)", cluster["id"], len(current.statements))
            return 0

        tenants = {
            r["db_role"]: r["id"]
            for r in control.execute("SELECT id, db_role FROM cp.tenants WHERE cluster_id = %s", (cluster["id"],))
        }
        window = (previous.taken_at, current.taken_at)
        stat_rows, fingerprints = [], {}
        for (role, queryid), row in current.statements.items():
            tenant_id = tenants.get(role)
            if tenant_id is None:
                continue  # owner, monitor and other non-tenant roles are not tenant workload
            # A fingerprint absent from the previous snapshot first ran inside this window,
            # so all of its counters belong to the window.
            delta = counter_delta(row, previous.statements.get((role, queryid), ZERO_STATEMENT), STATEMENT_COUNTERS)
            if delta is None:
                continue
            fingerprints[queryid] = row["query"]
            stat_rows.append(
                (cluster["org_id"], cluster["id"], tenant_id, queryid, *window, *(delta[f] for f in STATEMENT_COUNTERS))
            )

        control.execute("SELECT cp.ensure_query_stats_partition(%s)", (current.taken_at.date(),))
        with control.cursor() as cur:
            cur.executemany(
                "INSERT INTO cp.query_fingerprints (cluster_id, org_id, queryid, query) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (cluster_id, queryid) DO NOTHING",
                [(cluster["id"], cluster["org_id"], qid, text) for qid, text in fingerprints.items()],
            )
            cur.executemany(
                "INSERT INTO cp.query_stats (org_id, cluster_id, tenant_id, queryid, window_start, window_end,"
                " calls, total_exec_ms, rows, shared_blks_hit, shared_blks_read, shared_blks_dirtied,"
                " temp_blks_written, wal_bytes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                stat_rows,
            )

        inst = current.instance
        delta = counter_delta(inst, previous.instance, INSTANCE_COUNTERS) or {f: 0 for f in INSTANCE_COUNTERS}
        control.execute(
            "INSERT INTO cp.instance_stats (org_id, cluster_id, window_start, window_end, xact_commit,"
            " xact_rollback, blks_read, blks_hit, temp_bytes, deadlocks, wal_bytes, active_connections,"
            " replica_lag_bytes, database_bytes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (cluster["org_id"], cluster["id"], *window, *(delta[f] for f in INSTANCE_COUNTERS),
             inst["active_connections"], inst["replica_lag_bytes"], inst["database_bytes"]),
        )
        log.info("cluster %s: %d query_stats rows for window %s..%s", cluster["id"], len(stat_rows), *window)
        return len(stat_rows)


def from_env() -> Collector:
    return Collector(
        control_url=os.environ["CONTROL_DB_COLLECTOR_URL"],
        monitor_user=os.environ.get("DP_MONITOR_USER", "dbpilot_monitor"),
        monitor_password=os.environ["DP_MONITOR_PASSWORD"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="collector")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=float(os.environ.get("COLLECT_INTERVAL_S", "60")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    collector = from_env()
    if args.once:
        collector.collect_once()
        return
    while True:
        started = time.monotonic()
        try:
            collector.collect_once()
        except psycopg.Error:
            log.exception("control-plane database unavailable; will retry")
        time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
