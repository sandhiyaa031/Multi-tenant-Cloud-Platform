"""Organizations as a managed resource: create, list, rename, archive, and what each does to the
rows around it. Every claim is checked against the control-plane database itself, not only
against what the API says."""
import pytest

from tests.conftest import PASSWORD, auth, unique

API = "/api/v1"


def make_org(client, headers: dict, name: str | None = None):
    name = name or unique("second")
    r = client.post(f"{API}/orgs", headers=headers, json={"name": name})
    return name, r


def session_in(client, headers: dict, slug: str) -> dict:
    r = client.post(f"{API}/auth/switch-org", headers=headers, json={"org_slug": slug})
    assert r.status_code == 200, r.text
    return auth(r.json()["access_token"])


# ── create and list ──────────────────────────────────────────────────────────

def test_a_signed_in_user_creates_another_organization_and_it_is_persisted(client, org, owner_db):
    name, r = make_org(client, org.admin)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["name"] == name and created["role"] == "ADMIN" and created["current"] is False
    assert created["members"] == 1 and created["clusters"] == 0 and created["tenants"] == 0

    # In the database, as the owner: the organization, its first ADMIN membership, and an audit row.
    row = owner_db.execute("SELECT name, slug, archived_at FROM cp.organizations WHERE id = %s", (created["id"],)).fetchone()
    assert row == (name, created["slug"], None)
    roles = owner_db.execute("SELECT role::text FROM cp.memberships WHERE org_id = %s", (created["id"],)).fetchall()
    assert roles == [("ADMIN",)]
    audit = dict(owner_db.execute("SELECT action, actor_user_id::text FROM cp.audit_log WHERE org_id = %s", (created["id"],)).fetchall())
    assert {"organizations.create", "memberships.insert"} <= set(audit)           # the explicit event and the trigger's row
    assert audit["organizations.create"] is not None                              # and the actor is named


def test_listing_survives_a_new_session_and_shows_every_organization_of_the_user(client, org):
    _, r = make_org(client, org.admin)
    second = r.json()
    # A brand-new login is a new session: what it sees comes from the database, not from the browser.
    login = client.post(f"{API}/auth/login", json={"email": org.admin_email, "password": PASSWORD})
    assert login.status_code == 200
    listed = client.get(f"{API}/orgs", headers=auth(login.json()["access_token"])).json()
    assert {o["slug"] for o in listed} == {org.slug, second["slug"]}
    assert [o["slug"] for o in listed if o["current"]] == [org.slug]
    assert {o["slug"] for o in login.json()["orgs"]} == {org.slug, second["slug"]}      # the switcher's list agrees


def test_creating_an_organization_creates_no_cluster_and_no_tenant(client, org, owner_db):
    """The boundary between DBPilot organizations and data-plane tenants."""
    org.cluster()
    _, r = make_org(client, org.admin)
    new_id = r.json()["id"]
    assert owner_db.execute("SELECT count(*) FROM cp.clusters WHERE org_id = %s", (new_id,)).fetchone()[0] == 0
    assert owner_db.execute("SELECT count(*) FROM cp.tenants WHERE org_id = %s", (new_id,)).fetchone()[0] == 0
    first = next(o for o in client.get(f"{API}/orgs", headers=org.admin).json() if o["id"] == org.id)
    assert first["clusters"] == 1 and first["tenants"] == 0              # counts belong to the organization that owns them


def test_a_user_sees_only_their_own_organizations(client, org, other_org):
    _, r = make_org(client, org.admin)
    mine = {o["slug"] for o in client.get(f"{API}/orgs", headers=org.admin).json()}
    theirs = {o["slug"] for o in client.get(f"{API}/orgs", headers=other_org.admin).json()}
    assert r.json()["slug"] in mine and not (mine & theirs)
    # Nor can they step into it.
    r = client.post(f"{API}/auth/switch-org", headers=other_org.admin, json={"org_slug": org.slug})
    assert r.status_code == 403


def test_invalid_names_and_missing_authentication_are_refused(client, org):
    assert client.post(f"{API}/orgs", headers=org.admin, json={"name": "x"}).status_code == 422          # too short
    assert client.post(f"{API}/orgs", headers=org.admin, json={"name": "!!!???"}).status_code == 422     # no letters or digits
    assert client.post(f"{API}/orgs", headers=org.admin, json={"name": "a" * 81}).status_code == 422
    assert client.post(f"{API}/orgs", headers=org.admin, json={}).status_code == 422
    assert client.post(f"{API}/orgs", json={"name": "No Session"}).status_code == 401
    assert client.get(f"{API}/orgs").status_code == 401
    assert client.get(f"{API}/orgs", headers=auth("not-a-token")).status_code == 401


def test_a_name_that_is_taken_is_a_conflict_that_says_so(client, org):
    name, r = make_org(client, org.admin)
    assert r.status_code == 201
    again = client.post(f"{API}/orgs", headers=org.admin, json={"name": name})
    assert again.status_code == 409 and "already exists" in again.json()["detail"]


# ── rename ───────────────────────────────────────────────────────────────────

def test_an_admin_renames_the_organization_and_the_identifier_stays(client, org, owner_db):
    new_name = unique("renamed")
    r = client.patch(f"{API}/orgs/current", headers=org.admin, json={"name": new_name})
    assert r.status_code == 200 and r.json()["name"] == new_name and r.json()["slug"] == org.slug
    assert owner_db.execute("SELECT name, slug FROM cp.organizations WHERE id = %s", (org.id,)).fetchone() == (new_name, org.slug)
    assert owner_db.execute("SELECT count(*) FROM cp.audit_log WHERE org_id = %s AND action = 'organizations.rename'", (org.id,)).fetchone()[0] == 1
    assert client.get(f"{API}/auth/me", headers=org.admin).json()["org"]["name"] == new_name


def test_only_an_admin_may_rename(client, org, outbox):
    for role in ("VIEWER", "OPERATOR"):
        headers = org.member(role, outbox)
        assert client.patch(f"{API}/orgs/current", headers=headers, json={"name": "Hijacked"}).status_code == 403
    assert client.get(f"{API}/auth/me", headers=org.admin).json()["org"]["name"] == org.name


# ── archive ──────────────────────────────────────────────────────────────────

def test_archiving_keeps_the_rows_and_closes_the_door(client, org, owner_db):
    name, r = make_org(client, org.admin)
    second = r.json()
    inside = session_in(client, org.admin, second["slug"])
    assert client.get(f"{API}/clusters", headers=inside).status_code == 200

    r = client.delete(f"{API}/orgs/current", headers=inside)
    assert r.status_code == 200, r.text
    assert r.json()["org"]["slug"] == org.slug                               # the session moves to the other organization
    assert {o["slug"] for o in r.json()["orgs"]} == {org.slug}

    # Kept, with its history.
    assert owner_db.execute("SELECT archived_at IS NOT NULL FROM cp.organizations WHERE id = %s", (second["id"],)).fetchone()[0] is True
    actions = [a for (a,) in owner_db.execute("SELECT action FROM cp.audit_log WHERE org_id = %s ORDER BY id", (second["id"],))]
    assert "organizations.create" in actions and actions[-1] == "organizations.archive"
    # Shut: the old session, the listing, sign-in, and the name.
    assert client.get(f"{API}/clusters", headers=inside).status_code == 403
    assert client.get(f"{API}/auth/me", headers=inside).status_code == 403
    assert second["slug"] not in {o["slug"] for o in client.get(f"{API}/orgs", headers=org.admin).json()}
    login = client.post(f"{API}/auth/login", json={"email": org.admin_email, "password": PASSWORD, "org_slug": second["slug"]})
    assert login.status_code == 403
    assert client.post(f"{API}/orgs", headers=org.admin, json={"name": name}).status_code == 409


def test_the_only_organization_of_a_user_cannot_be_archived(client, org, owner_db):
    r = client.delete(f"{API}/orgs/current", headers=org.admin)
    assert r.status_code == 409 and "only organization" in r.json()["detail"]
    assert owner_db.execute("SELECT archived_at FROM cp.organizations WHERE id = %s", (org.id,)).fetchone()[0] is None


def test_an_organization_with_a_live_cluster_must_retire_it_first(client, org, owner_db):
    _, r = make_org(client, org.admin)
    inside = session_in(client, org.admin, r.json()["slug"])
    cluster = client.post(f"{API}/clusters", headers=inside, json={
        "name": unique("prod"), "pooler_host": "pgbouncer", "pooler_port": 6432, "database_name": "app"}).json()["id"]
    refused = client.delete(f"{API}/orgs/current", headers=inside)
    assert refused.status_code == 409 and "retire" in refused.json()["detail"]

    retired = client.post(f"{API}/clusters/{cluster}/retire", headers=inside)
    assert retired.status_code == 200 and retired.json()["status"] == "RETIRED" and retired.json()["primary_host"] is None
    assert owner_db.execute("SELECT status FROM cp.clusters WHERE id = %s", (cluster,)).fetchone()[0] == "RETIRED"
    assert client.delete(f"{API}/orgs/current", headers=inside).status_code == 200
    # The retired cluster is history, not deleted.
    assert owner_db.execute("SELECT count(*) FROM cp.clusters WHERE id = %s", (cluster,)).fetchone()[0] == 1


def test_only_an_admin_may_archive(client, org, outbox):
    for role in ("VIEWER", "OPERATOR"):
        assert client.delete(f"{API}/orgs/current", headers=org.member(role, outbox)).status_code == 403


# ── retiring a cluster ───────────────────────────────────────────────────────

def test_a_cluster_with_a_change_in_flight_cannot_be_retired(client, org):
    cluster = org.cluster()
    r = client.post(f"{API}/clusters/{cluster}/proposals", headers=org.admin,
                    json={"action": {"type": "no_action", "reason": "waiting"}, "verification": "none"})
    assert r.status_code == 201, r.text          # no primary host: the engine never picks it up, it stays PROPOSED
    refused = client.post(f"{API}/clusters/{cluster}/retire", headers=org.admin)
    assert refused.status_code == 409 and "still being verified" in refused.json()["detail"]


def test_retiring_needs_an_admin_and_another_organizations_cluster_is_not_found(client, org, other_org, outbox):
    cluster = org.cluster()
    assert client.post(f"{API}/clusters/{cluster}/retire", headers=org.member("OPERATOR", outbox)).status_code == 403
    assert client.post(f"{API}/clusters/{cluster}/retire", headers=other_org.admin).status_code == 404
    assert client.get(f"{API}/clusters", headers=org.admin).json()[0]["status"] != "RETIRED"
