import psycopg
import pytest


def test_overlapping_warehouse_ranges_rejected(client, org):
    cluster_id = org.cluster()
    assert client.post("/api/v1/tenants", headers=org.admin, json=org.tenant_body(cluster_id, 1, 10)).status_code == 201
    r = client.post("/api/v1/tenants", headers=org.admin, json=org.tenant_body(cluster_id, 10, 20))
    assert r.status_code == 409
    assert "overlaps" in r.json()["detail"]
    assert client.post("/api/v1/tenants", headers=org.admin, json=org.tenant_body(cluster_id, 11, 20)).status_code == 201


def test_same_range_is_fine_in_a_different_cluster(client, org):
    a, b = org.cluster(), org.cluster()
    assert client.post("/api/v1/tenants", headers=org.admin, json=org.tenant_body(a, 1, 10)).status_code == 201
    assert client.post("/api/v1/tenants", headers=org.admin, json=org.tenant_body(b, 1, 10)).status_code == 201


def test_inverted_range_rejected(client, org):
    r = client.post("/api/v1/tenants", headers=org.admin, json=org.tenant_body(org.cluster(), 10, 1))
    assert r.status_code == 422


def test_slo_upsert_replaces_rather_than_duplicates(client, org):
    tenant = client.post("/api/v1/tenants", headers=org.admin, json=org.tenant_body(org.cluster(), 1, 4)).json()
    url = f"/api/v1/tenants/{tenant['id']}/slos"
    client.put(url, headers=org.admin, json={"query_class": "OLTP", "percentile": 99, "threshold_ms": 50})
    client.put(url, headers=org.admin, json={"query_class": "OLTP", "percentile": 99, "threshold_ms": 30})
    slos = client.get(url, headers=org.admin).json()
    assert len(slos) == 1
    assert float(slos[0]["threshold_ms"]) == 30


def test_changes_are_audited_with_actor_and_before_after(client, org):
    tenant = client.post("/api/v1/tenants", headers=org.admin, json=org.tenant_body(org.cluster(), 1, 4)).json()
    client.delete(f"/api/v1/tenants/{tenant['id']}", headers=org.admin)

    entries = [e for e in client.get("/api/v1/audit", headers=org.admin).json() if e["entity_id"] == tenant["id"]]
    assert [e["action"] for e in entries] == ["tenants.delete", "tenants.insert"]  # newest first
    assert entries[0]["actor_email"] == org.admin_email
    assert entries[0]["detail"]["old"]["name"] == tenant["name"]
    assert entries[1]["detail"]["new"]["name"] == tenant["name"]


def test_audit_pagination(client, org):
    cluster_id = org.cluster()
    for i in range(3):
        client.post("/api/v1/tenants", headers=org.admin, json=org.tenant_body(cluster_id, i * 10 + 1, i * 10 + 5))
    first = client.get("/api/v1/audit?limit=2", headers=org.admin).json()
    second = client.get(f"/api/v1/audit?limit=2&before_id={first[-1]['id']}", headers=org.admin).json()
    assert len(first) == 2 and second
    assert first[-1]["id"] > second[0]["id"]


@pytest.mark.parametrize("statement", ["UPDATE cp.audit_log SET action = 'rewritten'", "DELETE FROM cp.audit_log"])
def test_audit_log_is_immutable_even_for_the_owner(client, org, owner_db, statement):
    org.cluster()  # guarantees at least one audit row exists
    with pytest.raises(psycopg.DatabaseError) as exc:
        owner_db.execute(statement)
    assert exc.value.sqlstate == "DP002"


def test_audit_log_cannot_be_truncated(owner_db):
    with pytest.raises(psycopg.DatabaseError) as exc:
        owner_db.execute("TRUNCATE cp.audit_log")
    assert exc.value.sqlstate == "DP002"
