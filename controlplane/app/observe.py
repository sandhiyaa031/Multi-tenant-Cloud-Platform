"""Read-only observations of a managed cluster.

These functions are the whole of what a proposer can see. The rule-based
proposer calls them directly; the LLM agent calls them as tools. None of them
changes anything, and none returns row data: query text is fingerprints
(literals already replaced by $n) and everything else is statistics.
"""
import os
from dataclasses import dataclass
from uuid import UUID

import httpx
import psycopg
from psycopg.rows import dict_row

from dbpilot_core import actions


@dataclass
class Observer:
    """Bound to one organization and one cluster. `control` is a connection whose
    transaction carries that organization's context, so row-level security applies."""

    control: psycopg.Connection
    cluster: dict

    # -- control-plane telemetry --------------------------------------------

    def tenants(self) -> list[dict]:
        return self.control.execute(
            "SELECT name, db_role, profile, warehouse_lo, warehouse_hi FROM cp.tenants WHERE cluster_id = %s"
            " ORDER BY warehouse_lo", (self.cluster["id"],)).fetchall()

    def slo_status(self, minutes: int = 15) -> list[dict]:
        """Each SLO with the latency observed against it over the last `minutes`."""
        return self.control.execute(
            """
            SELECT t.db_role AS tenant_role, s.query_class, s.percentile, s.threshold_ms::float AS threshold_ms,
                   count(l.*) AS windows,
                   round(avg(CASE s.percentile WHEN 50 THEN l.p50_ms WHEN 95 THEN l.p95_ms ELSE l.p99_ms END)::numeric, 2)::float
                       AS observed_ms,
                   count(*) FILTER (WHERE CASE s.percentile WHEN 50 THEN l.p50_ms WHEN 95 THEN l.p95_ms ELSE l.p99_ms END
                                          > s.threshold_ms) AS windows_violating
            FROM cp.slos s
            JOIN cp.tenants t ON t.id = s.tenant_id
            LEFT JOIN cp.latency_stats l ON l.tenant_id = s.tenant_id AND l.query_class = s.query_class
                 AND l.window_end > now() - make_interval(mins => %s)
            WHERE t.cluster_id = %s
            GROUP BY t.db_role, s.query_class, s.percentile, s.threshold_ms
            ORDER BY t.db_role, s.query_class
            """, (minutes, self.cluster["id"])).fetchall()

    def latency(self, minutes: int = 15) -> list[dict]:
        return self.control.execute(
            """
            SELECT t.db_role AS tenant_role, l.query_class, sum(l.txn_count) AS transactions,
                   round(avg(l.p50_ms)::numeric, 2)::float AS p50_ms, round(avg(l.p95_ms)::numeric, 2)::float AS p95_ms,
                   round(max(l.p99_ms)::numeric, 2)::float AS worst_p99_ms
            FROM cp.latency_stats l JOIN cp.tenants t ON t.id = l.tenant_id
            WHERE l.cluster_id = %s AND l.window_end > now() - make_interval(mins => %s)
            GROUP BY t.db_role, l.query_class ORDER BY t.db_role, l.query_class
            """, (self.cluster["id"], minutes)).fetchall()

    def top_queries(self, minutes: int = 15, tenant_role: str | None = None, limit: int = 15) -> list[dict]:
        return self.control.execute(
            """
            SELECT t.db_role AS tenant_role, s.queryid::text AS queryid, f.query,
                   sum(s.calls)::bigint AS calls, round(sum(s.total_exec_ms)::numeric, 1)::float AS total_exec_ms,
                   round((sum(s.total_exec_ms) / sum(s.calls))::numeric, 3)::float AS mean_exec_ms,
                   sum(s.rows)::bigint AS rows, sum(s.shared_blks_read)::bigint AS blocks_read_from_disk,
                   sum(s.temp_blks_written)::bigint AS temp_blocks_written, sum(s.wal_bytes)::bigint AS wal_bytes
            FROM cp.query_stats s
            JOIN cp.tenants t ON t.id = s.tenant_id
            JOIN cp.query_fingerprints f ON f.cluster_id = s.cluster_id AND f.queryid = s.queryid
            WHERE s.cluster_id = %s AND s.window_end > now() - make_interval(mins => %s)
              AND (%s::text IS NULL OR t.db_role = %s)
              AND f.query !~* '^(BEGIN|COMMIT|ROLLBACK|SET|SHOW)'
            GROUP BY t.db_role, s.queryid, f.query
            ORDER BY sum(s.total_exec_ms) DESC LIMIT %s
            """, (self.cluster["id"], minutes, tenant_role, tenant_role, limit)).fetchall()

    def tenant_load(self, minutes: int = 30) -> list[dict]:
        """Per tenant and collector window: calls and execution time. Shows bursts and shifts."""
        return self.control.execute(
            """
            SELECT t.db_role AS tenant_role, s.window_end, sum(s.calls)::bigint AS calls,
                   round(sum(s.total_exec_ms)::numeric, 1)::float AS total_exec_ms
            FROM cp.query_stats s JOIN cp.tenants t ON t.id = s.tenant_id
            WHERE s.cluster_id = %s AND s.window_end > now() - make_interval(mins => %s)
            GROUP BY t.db_role, s.window_end ORDER BY s.window_end, t.db_role
            """, (self.cluster["id"], minutes)).fetchall()

    def history(self, limit: int = 20) -> list[dict]:
        """Past proposals on this cluster with what the twin predicted and what production did."""
        return self.control.execute(
            "SELECT action, source, state, state_reason, twin_decision, canary_outcome, production_ratios, created_at"
            " FROM cp.outcome_ledger WHERE cluster_id = %s ORDER BY created_at DESC LIMIT %s",
            (self.cluster["id"], limit)).fetchall()

    # -- data plane, through the statistics-only monitoring role --------------

    def _monitor(self) -> psycopg.Connection:
        return psycopg.connect(
            host=self.cluster["primary_host"], port=self.cluster["primary_port"] or 5432,
            dbname=self.cluster["database_name"], user=os.environ.get("DP_MONITOR_USER", "dbpilot_monitor"),
            password=os.environ["DP_MONITOR_PASSWORD"], connect_timeout=5, row_factory=dict_row, autocommit=True)

    def table_profile(self, table: str) -> dict:
        """Size, scan and write counters, statistics freshness and indexes of one table's partitions."""
        if table not in actions.TABLES:
            raise ValueError(f"unknown table {table!r}; known tables: {sorted(actions.TABLES)}")
        with self._monitor() as dp:
            relations = dp.execute(
                """
                SELECT s.relname AS relation, pg_total_relation_size(s.relid) AS total_bytes, s.n_live_tup AS live_rows,
                       s.seq_scan, s.seq_tup_read, s.idx_scan, s.n_tup_ins AS inserts, s.n_tup_upd AS updates,
                       s.n_tup_del AS deletes, s.n_mod_since_analyze AS rows_changed_since_analyze,
                       greatest(s.last_analyze, s.last_autoanalyze) AS last_analyzed
                FROM pg_stat_user_tables s
                WHERE s.schemaname = 'ch' AND (s.relname = %s OR s.relname LIKE %s) ORDER BY s.relname
                """, (table, table + r"\_t\_%")).fetchall()
            indexes = dp.execute(
                """
                SELECT i.relname AS relation, i.indexrelname AS index, pg_relation_size(i.indexrelid) AS bytes,
                       i.idx_scan AS scans, pg_get_indexdef(i.indexrelid) AS definition
                FROM pg_stat_user_indexes i
                WHERE i.schemaname = 'ch' AND (i.relname = %s OR i.relname LIKE %s) ORDER BY i.relname, i.indexrelname
                """, (table, table + r"\_t\_%")).fetchall()
        return {"table": table, "columns": [actions.TABLES[table][0], *actions.TABLES[table][1]],
                "relations": relations, "indexes": indexes}

    def settings(self) -> dict:
        """Current values of every setting an action may change, instance-wide and per tenant."""
        names = sorted(set(actions.ROLE_SETTINGS) | set(actions.INSTANCE_SETTINGS))
        with self._monitor() as dp:
            instance = {r["name"]: f"{r['setting']}{r['unit'] or ''}" for r in dp.execute(
                "SELECT name, setting, unit FROM pg_settings WHERE name = ANY(%s)", (names,))}
            roles = {r["rolname"]: {"overrides": r["rolconfig"] or [], "connection_limit": r["rolconnlimit"]}
                     for r in dp.execute("SELECT rolname, rolconfig, rolconnlimit FROM pg_roles WHERE rolname LIKE 't\\_%'")}
        return {"instance": instance, "tenants": roles}

    # -- twin source: planner questions, never production ---------------------

    def _twin(self) -> httpx.Client:
        return httpx.Client(base_url=os.environ.get("TWIN_URL", "http://twin:8080"), timeout=60,
                            headers={"Authorization": f"Bearer {os.environ['TWIN_TOKEN']}"})

    def explain(self, queryid: str) -> dict:
        row = self.control.execute(
            "SELECT query FROM cp.query_fingerprints WHERE cluster_id = %s AND queryid = %s",
            (self.cluster["id"], int(queryid))).fetchone()
        if row is None:
            raise ValueError(f"no fingerprint with queryid {queryid}")
        with self._twin() as twin:
            return twin.post("/explain", json={"query": row["query"]}).raise_for_status().json()

    def whatif_index(self, table: str, columns: list[str], tenant_role: str | None = None, minutes: int = 60) -> dict:
        """Planner cost of the observed queries on `table`, with and without a hypothetical index."""
        action = actions.CreateIndex(table=table, columns=columns, tenant_role=tenant_role)
        queries = [q["query"] for q in self.top_queries(minutes, None, 100) if f"ch.{table}" in q["query"]]
        with self._twin() as twin:
            return twin.post("/whatif", json={"action": action.model_dump(), "queries": queries}).raise_for_status().json()


def open_observer(database_url: str, user_id: UUID, org_id: UUID, cluster_id: UUID) -> tuple[psycopg.Connection, Observer]:
    """A synchronous connection in the caller's organization context, for use off the event loop."""
    conn = psycopg.connect(database_url, row_factory=dict_row)
    conn.execute("SELECT set_config('app.user_id', %s, false), set_config('app.org_id', %s, false)",
                 (str(user_id), str(org_id)))
    cluster = conn.execute("SELECT * FROM cp.clusters WHERE id = %s", (cluster_id,)).fetchone()
    if cluster is None:
        conn.close()
        raise LookupError("cluster not found")
    return conn, Observer(conn, cluster)
