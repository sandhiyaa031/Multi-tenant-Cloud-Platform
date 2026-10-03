import psycopg
import pytest

from app.engine import rollback_reason, t0_static, window_breaches
from dbpilot_core import actions

INDEX = {"type": "create_index", "table": "order_line", "columns": ["ol_i_id"], "tenant_role": "t_analytic"}
MEMORY = 6 * 1024**3
FACTS = {"tenant_roles": {"t_steady", "t_analytic"}, "max_connections": 200, "pool_size": 20, "applied_actions": []}


# ── T0 static rules ──────────────────────────────────────────────────────────

def test_t0_passes_a_bounded_action():
    assert t0_static(actions.parse_action(INDEX), FACTS, MEMORY)[0] == "APPROVE"


def test_t0_rejects_unknown_tenant_and_duplicates():
    other = actions.parse_action({**INDEX, "tenant_role": "t_ghost"})
    assert t0_static(other, FACTS, MEMORY)[0] == "REJECT"
    applied = {**FACTS, "applied_actions": [actions.parse_action(INDEX).model_dump()]}
    assert t0_static(actions.parse_action(INDEX), applied, MEMORY)[0] == "REJECT"


def test_t0_memory_arithmetic_separates_role_scope_from_instance_scope():
    """64 MB of sort memory is fine for one tenant's 20 connections and refused for all 200."""
    role = actions.parse_action({"type": "role_setting", "tenant_role": "t_analytic", "name": "work_mem", "value": "65536"})
    instance = actions.parse_action({"type": "instance_setting", "name": "work_mem", "value": "65536"})
    assert t0_static(role, FACTS, MEMORY)[0] == "APPROVE"
    decision, summary = t0_static(instance, FACTS, MEMORY)
    assert decision == "REJECT" and "memory" in summary


def test_t0_routes_advisory_actions_to_a_human():
    assert t0_static(actions.parse_action({"type": "no_action", "reason": "transient"}), FACTS, MEMORY)[0] == "SKIPPED"
    routing = actions.parse_action({"type": "replica_routing", "tenant_role": "t_analytic", "enabled": True})
    assert t0_static(routing, FACTS, MEMORY)[0] == "SKIPPED"


# ── T3 canary decisions ──────────────────────────────────────────────────────

BASELINE = {"t_steady/OLTP": 30.0, "t_analytic/OLAP": 400.0}
CONTRACT = {"t_steady/OLTP": 1.15, "t_analytic/OLAP": 1.10}


def test_window_breach_detection():
    ok = {"t_steady/OLTP": (33.0, 200), "t_analytic/OLAP": (300.0, 20)}
    assert window_breaches(BASELINE, ok, CONTRACT, 5) == []
    bad = {"t_steady/OLTP": (45.0, 200), "t_analytic/OLAP": (300.0, 20)}
    assert window_breaches(BASELINE, bad, CONTRACT, 5)[0].startswith("t_steady/OLTP: p95 1.50x")
    # Too few transactions to judge a percentile: not counted as a breach.
    sparse = {"t_steady/OLTP": (90.0, 2)}
    assert window_breaches(BASELINE, sparse, CONTRACT, 5) == []


def test_rollback_needs_two_breaches_in_three_windows():
    ok, bad = {"breaches": [], "telemetry": True}, {"breaches": ["t_steady/OLTP: ..."], "telemetry": True}
    assert rollback_reason([bad]) is None                 # one bad window may be noise
    assert rollback_reason([bad, ok, ok]) is None
    assert "2 of the last 3" in rollback_reason([bad, ok, bad])
    assert "2 of the last 3" in rollback_reason([ok, bad, bad])


def test_lost_telemetry_triggers_rollback():
    blind = {"breaches": [], "telemetry": False}
    assert rollback_reason([blind]) is None
    assert "telemetry lost" in rollback_reason([blind, blind])


# ── API and database rules ───────────────────────────────────────────────────

@pytest.fixture
def cluster_with_tenant(client, org):
    cluster_id = org.cluster()
    body = {"cluster_id": cluster_id, "name": "analytic", "db_role": "t_analytic", "warehouse_lo": 5,
            "warehouse_hi": 8, "profile": "ANALYTICAL"}
    assert client.post("/api/v1/tenants", headers=org.admin, json=body).status_code == 201
    return cluster_id


def propose(client, org, cluster_id, action=INDEX, **extra):
    return client.post(f"/api/v1/clusters/{cluster_id}/proposals", headers=org.admin, json={"action": action, **extra})


def test_proposal_is_validated_normalised_and_linked_to_its_tenant(client, org, cluster_with_tenant):
    r = propose(client, org, cluster_with_tenant, rationale="item lookups scan the whole partition")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["state"] == "PROPOSED" and body["source"] == "manual"
    assert body["action"]["include"] == []          # defaults filled in
    assert body["target_tenant_id"] is not None


@pytest.mark.parametrize("action", [
    {"type": "run_sql", "sql": "DROP TABLE ch.customer"},
    {"type": "create_index", "table": "orders", "columns": ["o_id); DROP TABLE x; --"]},
    {"type": "instance_setting", "name": "fsync", "value": "off"},
    {**INDEX, "tenant_role": "t_not_registered"},
])
def test_api_refuses_anything_outside_the_action_space(client, org, cluster_with_tenant, action):
    assert propose(client, org, cluster_with_tenant, action).status_code == 422


def test_viewer_cannot_propose_and_other_orgs_cannot_see(client, org, other_org, cluster_with_tenant, outbox):
    viewer = org.member("VIEWER", outbox)
    r = client.post(f"/api/v1/clusters/{cluster_with_tenant}/proposals", headers=viewer, json={"action": INDEX})
    assert r.status_code == 403
    pid = propose(client, org, cluster_with_tenant).json()["id"]
    assert client.get(f"/api/v1/proposals/{pid}", headers=other_org.admin).status_code == 404
    assert client.get(f"/api/v1/clusters/{cluster_with_tenant}/ledger", headers=other_org.admin).json() == []
    assert client.post(f"/api/v1/clusters/{cluster_with_tenant}/proposals", headers=other_org.admin,
                       json={"action": INDEX}).status_code == 404


def move(owner_db, pid, *states):
    for s in states:
        owner_db.execute("UPDATE cp.proposals SET state = %s WHERE id = %s", (s, pid))
    owner_db.commit()


def test_state_machine_forbids_skipping_verification(client, org, cluster_with_tenant, owner_db):
    """Even the database owner cannot take a proposal straight to production."""
    pid = propose(client, org, cluster_with_tenant).json()["id"]
    for illegal in ("APPROVED", "CANARY", "APPLIED"):
        with pytest.raises(psycopg.DatabaseError) as exc:
            owner_db.execute("UPDATE cp.proposals SET state = %s WHERE id = %s", (illegal, pid))
        assert exc.value.sqlstate == "DP003"
        owner_db.rollback()


def test_rejected_proposal_cannot_be_revived_and_action_cannot_be_edited(client, org, cluster_with_tenant, owner_db):
    pid = propose(client, org, cluster_with_tenant).json()["id"]
    with pytest.raises(psycopg.DatabaseError) as exc:
        owner_db.execute("""UPDATE cp.proposals SET action = '{"type": "no_action", "reason": "x"}' WHERE id = %s""", (pid,))
    assert exc.value.sqlstate == "DP003"
    owner_db.rollback()
    move(owner_db, pid, "VERIFYING", "REJECTED")
    with pytest.raises(psycopg.DatabaseError):
        owner_db.execute("UPDATE cp.proposals SET state = 'APPROVED' WHERE id = %s", (pid,))
    owner_db.rollback()


def test_approval_rules(client, org, cluster_with_tenant, owner_db, outbox):
    operator = org.member("OPERATOR", outbox)
    passed = propose(client, org, cluster_with_tenant).json()["id"]
    unsure = propose(client, org, cluster_with_tenant).json()["id"]
    fresh = propose(client, org, cluster_with_tenant).json()["id"]
    move(owner_db, passed, "VERIFYING", "AWAITING_APPROVAL")
    move(owner_db, unsure, "VERIFYING", "INCONCLUSIVE")

    # Not yet verified: nobody can approve it.
    assert client.post(f"/api/v1/proposals/{fresh}/approve", headers=org.admin, json={}).status_code == 409
    # Verified: an operator may approve.
    r = client.post(f"/api/v1/proposals/{passed}/approve", headers=operator, json={"reason": "looks right"})
    assert r.status_code == 200 and r.json()["state"] == "APPROVED"
    # Inconclusive: overriding takes an admin.
    assert client.post(f"/api/v1/proposals/{unsure}/approve", headers=operator, json={}).status_code == 403
    assert client.post(f"/api/v1/proposals/{unsure}/approve", headers=org.admin, json={}).json()["state"] == "APPROVED"

    actions_logged = [e["action"] for e in client.get("/api/v1/audit?limit=200", headers=org.admin).json()]
    assert actions_logged.count("proposals.update") >= 2


def test_only_one_canary_per_cluster(client, org, cluster_with_tenant, owner_db):
    a = propose(client, org, cluster_with_tenant).json()["id"]
    b = propose(client, org, cluster_with_tenant).json()["id"]
    move(owner_db, a, "VERIFYING", "APPROVED", "CANARY")
    move(owner_db, b, "VERIFYING", "APPROVED")
    with pytest.raises(psycopg.errors.UniqueViolation):
        owner_db.execute("UPDATE cp.proposals SET state = 'CANARY' WHERE id = %s", (b,))
    owner_db.rollback()


def test_detail_and_ledger(client, org, cluster_with_tenant):
    pid = propose(client, org, cluster_with_tenant).json()["id"]
    detail = client.get(f"/api/v1/proposals/{pid}", headers=org.admin).json()
    assert detail["steps"] == [] and detail["twin_runs"] == [] and detail["canary"] is None
    rows = client.get(f"/api/v1/clusters/{cluster_with_tenant}/ledger", headers=org.admin).json()
    assert rows[0]["proposal_id"] == pid and rows[0]["action_type"] == "create_index" and rows[0]["twin_decision"] is None


def test_action_schema_is_published(client, org):
    body = client.get("/api/v1/actions/schema", headers=org.admin).json()
    assert "work_mem" in body["role_settings"] and "order_line" in body["tables"]
