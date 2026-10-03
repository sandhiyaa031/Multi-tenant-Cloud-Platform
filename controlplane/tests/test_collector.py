import os

import psycopg
import pytest

from app.collector import STATEMENT_COUNTERS, ZERO_STATEMENT, counter_delta, from_env

DP_HOST = os.environ.get("DP_PRIMARY_HOST", "dp-primary")


def counters(calls, ms=0.0):
    row = {f: 0 for f in STATEMENT_COUNTERS}
    row.update(calls=calls, total_exec_ms=ms)
    return row


def test_delta_is_none_without_a_previous_snapshot():
    assert counter_delta(counters(10), None, STATEMENT_COUNTERS) is None


def test_delta_subtracts_cumulative_counters():
    delta = counter_delta(counters(15, 90.0), counters(10, 60.0), STATEMENT_COUNTERS)
    assert delta["calls"] == 5 and delta["total_exec_ms"] == 30.0


def test_delta_after_a_statistics_reset_uses_the_current_value():
    """Counters that went down mean a reset; the current value is the activity since then."""
    delta = counter_delta(counters(3, 12.0), counters(500, 9000.0), STATEMENT_COUNTERS)
    assert delta["calls"] == 3 and delta["total_exec_ms"] == 12.0


def test_fingerprint_first_seen_in_a_window_counts_from_zero():
    delta = counter_delta(counters(4, 8.0), ZERO_STATEMENT, STATEMENT_COUNTERS)
    assert delta["calls"] == 4 and delta["total_exec_ms"] == 8.0


def test_no_row_for_an_idle_window():
    assert counter_delta(counters(10, 60.0), counters(10, 60.0), STATEMENT_COUNTERS) is None


@pytest.mark.parametrize(
    "statement",
    ["SELECT * FROM cp.users", "SELECT * FROM cp.audit_log", "SELECT * FROM cp.memberships",
     "UPDATE cp.tenants SET name = 'x'", "DELETE FROM cp.query_stats"],
)
def test_collector_role_is_limited_to_reading_resources_and_appending_telemetry(statement):
    with psycopg.connect(os.environ["CONTROL_DB_COLLECTOR_URL"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)


def data_plane_available() -> bool:
    try:
        psycopg.connect(
            host=DP_HOST, dbname="app", user="dbpilot_monitor", password=os.environ.get("DP_MONITOR_PASSWORD", ""),
            connect_timeout=3,
        ).close()
        return True
    except psycopg.Error:
        return False


needs_data_plane = pytest.mark.skipif(not data_plane_available(), reason="data plane is not running")


def tenant_conn(role: str) -> psycopg.Connection:
    return psycopg.connect(
        host=DP_HOST, dbname="app", user=role, password=os.environ["DP_TENANT_PASSWORD"], autocommit=True
    )


@needs_data_plane
def test_monitor_role_reads_statistics_but_not_tenant_data():
    with psycopg.connect(
        host=DP_HOST, dbname="app", user="dbpilot_monitor", password=os.environ["DP_MONITOR_PASSWORD"]
    ) as conn:
        assert conn.execute("SELECT count(*) FROM pg_stat_statements").fetchone()[0] >= 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM ch.customer LIMIT 1")


@needs_data_plane
def test_collector_attributes_activity_to_the_right_tenant(client, org, other_org):
    r = client.post(
        "/api/v1/clusters",
        headers=org.admin,
        json={"name": "observed", "pooler_host": "pgbouncer", "pooler_port": 6432, "database_name": "app",
              "primary_host": DP_HOST, "primary_port": 5432},
    )
    assert r.status_code == 201, r.text
    cluster_id = r.json()["id"]
    for name, role, lo, hi in [("steady", "t_steady", 1, 2), ("bursty", "t_bursty", 3, 4)]:
        body = {"cluster_id": cluster_id, "name": name, "db_role": role, "warehouse_lo": lo, "warehouse_hi": hi,
                "profile": "STEADY_OLTP"}
        assert client.post("/api/v1/tenants", headers=org.admin, json=body).status_code == 201

    # pg_stat_statements identifies a query by the shape of its parse tree, so literals
    # (and even column aliases) do not make it distinct. Nothing else issues this shape,
    # and the collector stores deltas, so the counts below are exactly this test's calls.
    marker = "SELECT count(*) FROM ch.district WHERE d_id = %s AND d_w_id > %s AND d_tax >= %s"
    collector = from_env()
    collector.collect_once()  # baseline
    with tenant_conn("t_steady") as steady, tenant_conn("t_bursty") as bursty:
        for d in range(1, 6):
            steady.execute(marker, (d, 0, 0))
        bursty.execute(marker, (1, 0, 0))
        bursty.execute(marker, (2, 0, 0))
    assert collector.collect_once() >= 2

    top = client.get(f"/api/v1/clusters/{cluster_id}/top-queries?limit=100", headers=org.admin).json()
    marked = {q["tenant"]: q for q in top if "d_tax >= $3" in q["query"]}
    assert {t: q["calls"] for t, q in marked.items()} == {"steady": 5, "bursty": 2}
    assert all(q["total_exec_ms"] > 0 and 0 < q["time_share"] <= 1 for q in marked.values())

    load = client.get(f"/api/v1/clusters/{cluster_id}/tenant-load", headers=org.admin).json()
    assert {p["tenant"] for p in load} == {"steady", "bursty"}
    points = client.get(f"/api/v1/clusters/{cluster_id}/instance", headers=org.admin).json()
    assert len(points) == 1 and points[0]["database_bytes"] > 0

    # Another organization cannot read this cluster's telemetry.
    for path in ("top-queries", "tenant-load", "instance"):
        assert client.get(f"/api/v1/clusters/{cluster_id}/{path}", headers=other_org.admin).json() == []


def test_latency_rows_summarise_per_role_and_class():
    from datetime import datetime, timedelta, timezone

    from app.collector import latency_rows
    from dbpilot_core.pglog import Transaction

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def txn(user, app, ms, failed=False):
        return Transaction(user=user, app=app, pid=1, start=t0, end=t0 + timedelta(milliseconds=ms), failed=failed)

    rows = latency_rows(
        [txn("t_a", "oltp", ms) for ms in range(1, 101)]
        + [txn("t_a", "oltp", 5000, failed=True), txn("t_a", "olap", 300), txn("t_b", "oltp", 7)]
    )
    count, failed, mean, p50, p95, p99 = rows[("t_a", "OLTP")]
    assert (count, failed) == (100, 1)          # the failed one is counted but not timed
    assert round(mean, 1) == 50.5 and round(p50, 1) == 50.5 and round(p99) == 99
    assert rows[("t_a", "OLAP")][0] == 1 and rows[("t_b", "OLTP")][3] == 7
