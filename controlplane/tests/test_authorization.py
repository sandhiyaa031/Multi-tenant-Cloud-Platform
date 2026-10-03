"""Authorization is tested twice: through the API, and directly against the
database as the API's own role. The second set proves the rules hold even if an
endpoint forgets to check."""
import threading

import psycopg
import pytest

from tests.conftest import Org


def set_context(conn, user_id, org_id):
    conn.execute("SELECT set_config('app.user_id', %s, true), set_config('app.org_id', %s, true)", (user_id, org_id))


def user_id_of(client, headers) -> str:
    return client.get("/api/v1/auth/me", headers=headers).json()["user_id"]


# ── RBAC through the API ─────────────────────────────────────────────────────


def test_role_matrix(client, org, outbox):
    viewer = org.member("VIEWER", outbox)
    operator = org.member("OPERATOR", outbox)
    cluster_id = org.cluster()

    # Everyone reads.
    for who in (viewer, operator, org.admin):
        assert client.get("/api/v1/tenants", headers=who).status_code == 200
        assert client.get("/api/v1/audit", headers=who).status_code == 200

    # OPERATOR and above write tenants and SLOs.
    assert client.post("/api/v1/tenants", headers=viewer, json=org.tenant_body(cluster_id, 1, 10)).status_code == 403
    r = client.post("/api/v1/tenants", headers=operator, json=org.tenant_body(cluster_id, 1, 10))
    assert r.status_code == 201
    tenant_id = r.json()["id"]
    slo = {"query_class": "OLTP", "percentile": 99, "threshold_ms": 50}
    assert client.put(f"/api/v1/tenants/{tenant_id}/slos", headers=viewer, json=slo).status_code == 403
    assert client.put(f"/api/v1/tenants/{tenant_id}/slos", headers=operator, json=slo).status_code == 200

    # Only ADMIN manages clusters and members.
    cluster = {"name": "another", "pooler_host": "h", "pooler_port": 6432, "database_name": "d"}
    invite = {"email": "x@example.com", "full_name": "X", "role": "VIEWER"}
    for who in (viewer, operator):
        assert client.post("/api/v1/clusters", headers=who, json=cluster).status_code == 403
        assert client.post("/api/v1/members", headers=who, json=invite).status_code == 403


def test_demotion_takes_effect_on_the_existing_token(client, org, outbox):
    operator = org.member("OPERATOR", outbox)
    cluster_id = org.cluster()
    assert client.post("/api/v1/tenants", headers=operator, json=org.tenant_body(cluster_id, 1, 5)).status_code == 201

    operator_id = user_id_of(client, operator)
    assert client.patch(f"/api/v1/members/{operator_id}", headers=org.admin, json={"role": "VIEWER"}).status_code == 200
    assert client.post("/api/v1/tenants", headers=operator, json=org.tenant_body(cluster_id, 6, 9)).status_code == 403

    assert client.delete(f"/api/v1/members/{operator_id}", headers=org.admin).status_code == 204
    assert client.get("/api/v1/tenants", headers=operator).status_code == 403


def test_last_admin_cannot_be_demoted_or_removed(client, org):
    admin_id = user_id_of(client, org.admin)
    assert client.patch(f"/api/v1/members/{admin_id}", headers=org.admin, json={"role": "VIEWER"}).status_code == 409
    assert client.delete(f"/api/v1/members/{admin_id}", headers=org.admin).status_code == 409


def test_two_admins_demoting_each_other_concurrently_leaves_one(client, org, outbox, owner_db):
    """The race the organization-row lock exists for: each transaction sees the
    other admin still in place, so without the lock both demotions would commit."""
    second = org.member("ADMIN", outbox)
    ids = [user_id_of(client, org.admin), user_id_of(client, second)]
    headers = [second, org.admin]  # each admin demotes the other
    barrier = threading.Barrier(2)
    codes = []

    def demote(target, who):
        barrier.wait()
        codes.append(client.patch(f"/api/v1/members/{target}", headers=who, json={"role": "VIEWER"}).status_code)

    threads = [threading.Thread(target=demote, args=(ids[i], headers[i])) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    admins = owner_db.execute(
        "SELECT count(*) FROM cp.memberships WHERE org_id = %s AND role = 'ADMIN'", (org.id,)
    ).fetchone()[0]
    assert admins == 1
    # The loser is refused either by the guard (409) or because it was already demoted (403).
    assert sorted(codes)[0] == 200 and sorted(codes)[1] in (403, 409)


# ── Organization isolation through the API ───────────────────────────────────


def test_organizations_cannot_see_or_touch_each_other(client, org, other_org):
    cluster_id = org.cluster()
    tenant_id = client.post("/api/v1/tenants", headers=org.admin, json=org.tenant_body(cluster_id, 1, 10)).json()["id"]

    assert client.get("/api/v1/tenants", headers=other_org.admin).json() == []
    assert client.get("/api/v1/clusters", headers=other_org.admin).json() == []
    assert client.delete(f"/api/v1/tenants/{tenant_id}", headers=other_org.admin).status_code == 404
    slo = {"query_class": "OLTP", "percentile": 99, "threshold_ms": 50}
    assert client.put(f"/api/v1/tenants/{tenant_id}/slos", headers=other_org.admin, json=slo).status_code == 404
    # Creating a tenant in someone else's cluster fails on the composite foreign key.
    r = client.post("/api/v1/tenants", headers=other_org.admin, json=other_org.tenant_body(cluster_id, 50, 60))
    assert r.status_code == 409

    theirs = {e["entity_id"] for e in client.get("/api/v1/audit", headers=other_org.admin).json()}
    assert tenant_id not in theirs
    members = client.get("/api/v1/members", headers=other_org.admin).json()
    assert [m["email"] for m in members] == [other_org.admin_email]


# ── The same rules, enforced by PostgreSQL itself ────────────────────────────


def test_db_without_context_sees_nothing(client, org, api_db):
    org.cluster()
    for table in ("organizations", "clusters", "tenants", "slos", "memberships", "audit_log"):
        assert api_db.execute(f"SELECT count(*) FROM cp.{table}").fetchone()[0] == 0


def test_db_rls_scopes_rows_to_the_context_org(client, org, other_org, api_db):
    org.cluster()
    other_org.cluster()
    set_context(api_db, user_id_of(client, org.admin), org.id)
    assert {str(r[0]) for r in api_db.execute("SELECT org_id FROM cp.clusters")} == {org.id}


def test_db_forged_org_context_without_membership_sees_nothing(client, org, other_org, api_db):
    other_org.cluster()
    set_context(api_db, user_id_of(client, org.admin), other_org.id)
    assert api_db.execute("SELECT count(*) FROM cp.clusters").fetchone()[0] == 0


def test_db_viewer_cannot_write_even_with_direct_sql(client, org, outbox, api_db):
    viewer = org.member("VIEWER", outbox)
    cluster_id = org.cluster()
    set_context(api_db, user_id_of(client, viewer), org.id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        api_db.execute(
            "INSERT INTO cp.tenants (org_id, cluster_id, name, db_role, warehouse_lo, warehouse_hi, profile)"
            " VALUES (%s, %s, 'sneaky', 'sneaky_role', 1, 2, 'MIXED')",
            (org.id, cluster_id),
        )


def test_db_cannot_insert_a_row_for_another_org(client, org, other_org, api_db):
    set_context(api_db, user_id_of(client, org.admin), org.id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        api_db.execute(
            "INSERT INTO cp.clusters (org_id, name, pooler_host, pooler_port, database_name)"
            " VALUES (%s, 'planted', 'h', 1, 'd')",
            (other_org.id,),
        )


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT password_hash FROM cp.users",
        "SELECT * FROM cp.password_reset_tokens",
        "INSERT INTO cp.audit_log (org_id, action, entity_type) VALUES (gen_random_uuid(), 'forged', 'x')",
        "INSERT INTO cp.users (email, full_name) VALUES ('planted@example.com', 'P')",
        "UPDATE cp.memberships SET org_id = gen_random_uuid()",
    ],
)
def test_db_api_role_lacks_dangerous_privileges(api_db, statement):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        api_db.execute(statement)


def test_api_role_is_not_privileged(api_db):
    row = api_db.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
    assert row == (False, False)
