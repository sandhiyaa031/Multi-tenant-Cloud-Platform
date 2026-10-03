from datetime import datetime, timedelta, timezone

import jwt

from app.config import get_settings
from tests.conftest import PASSWORD, Org, auth, token_from, unique


def test_signup_creates_org_and_admin(client, org):
    r = client.get("/api/v1/auth/me", headers=org.admin)
    assert r.status_code == 200
    assert r.json()["email"] == org.admin_email
    assert r.json()["org"]["role"] == "ADMIN"


def test_signup_duplicate_email_is_conflict(client, org):
    r = client.post(
        "/api/v1/auth/signup",
        json={"org_name": unique("other"), "email": org.admin_email, "password": PASSWORD, "full_name": "X"},
    )
    assert r.status_code == 409
    assert "email" in r.json()["detail"]


def test_signup_is_atomic(client, org, owner_db):
    """A failed signup (duplicate email) must not leave a half-created organization behind."""
    name = unique("ghost")
    r = client.post(
        "/api/v1/auth/signup",
        json={"org_name": name, "email": org.admin_email, "password": PASSWORD, "full_name": "X"},
    )
    assert r.status_code == 409
    assert owner_db.execute("SELECT count(*) FROM cp.organizations WHERE slug = %s", (name,)).fetchone()[0] == 0


def test_weak_password_rejected(client):
    r = client.post(
        "/api/v1/auth/signup",
        json={"org_name": unique("weak"), "email": f"{unique('u')}@example.com", "password": "short", "full_name": "X"},
    )
    assert r.status_code == 422


def test_login_wrong_password_and_unknown_user_look_identical(client, org):
    wrong = client.post("/api/v1/auth/login", json={"email": org.admin_email, "password": "not-the-password"})
    unknown = client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_login_succeeds_and_is_audited(client, org):
    r = client.post("/api/v1/auth/login", json={"email": org.admin_email, "password": PASSWORD})
    assert r.status_code == 200
    actions = [e["action"] for e in client.get("/api/v1/audit", headers=org.admin).json()]
    assert "auth.login" in actions


def test_requests_without_or_with_bad_token_are_401(client):
    assert client.get("/api/v1/tenants").status_code == 401
    assert client.get("/api/v1/tenants", headers=auth("not.a.token")).status_code == 401


def test_expired_and_forged_tokens_are_401(client, org):
    claims = jwt.decode(org.admin["Authorization"].split()[1], options={"verify_signature": False})
    expired = dict(claims, exp=datetime.now(timezone.utc) - timedelta(minutes=1))
    token = jwt.encode(expired, get_settings().jwt_secret, algorithm="HS256")
    assert client.get("/api/v1/tenants", headers=auth(token)).status_code == 401

    forged = jwt.encode(claims, "x" * 40, algorithm="HS256")
    assert client.get("/api/v1/tenants", headers=auth(forged)).status_code == 401


def test_token_for_org_without_membership_is_403(client, org, other_org):
    """A validly signed token naming an organization the user does not belong to gets nothing."""
    claims = jwt.decode(org.admin["Authorization"].split()[1], options={"verify_signature": False})
    crossed = jwt.encode(dict(claims, org=other_org.id), get_settings().jwt_secret, algorithm="HS256")
    assert client.get("/api/v1/tenants", headers=auth(crossed)).status_code == 403


def test_password_reset_flow_and_single_use(client, org, outbox):
    r = client.post("/api/v1/auth/forgot-password", json={"email": org.admin_email})
    assert r.status_code == 202
    token = token_from(outbox[-1][2])

    new_password = "a-brand-new-password"
    assert client.post("/api/v1/auth/reset-password", json={"token": token, "new_password": new_password}).status_code == 200
    assert client.post("/api/v1/auth/login", json={"email": org.admin_email, "password": PASSWORD}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": org.admin_email, "password": new_password}).status_code == 200

    again = client.post("/api/v1/auth/reset-password", json={"token": token, "new_password": "yet-another-password"})
    assert again.status_code == 400


def test_forgot_password_does_not_reveal_whether_account_exists(client, org, outbox):
    known = client.post("/api/v1/auth/forgot-password", json={"email": org.admin_email})
    unknown = client.post("/api/v1/auth/forgot-password", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert len(outbox) == 1


def test_expired_reset_token_rejected(client, org, outbox, owner_db):
    client.post("/api/v1/auth/forgot-password", json={"email": org.admin_email})
    token = token_from(outbox[-1][2])
    owner_db.execute("UPDATE cp.password_reset_tokens SET expires_at = now() - interval '1 second' WHERE used_at IS NULL")
    owner_db.commit()
    r = client.post("/api/v1/auth/reset-password", json={"token": token, "new_password": "a-brand-new-password"})
    assert r.status_code == 400


def test_invited_user_cannot_log_in_before_setting_password(client, org, outbox):
    email = f"{unique('invitee')}@example.com"
    client.post("/api/v1/members", headers=org.admin, json={"email": email, "full_name": "I", "role": "VIEWER"})
    assert client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).status_code == 401


def test_user_in_two_orgs_can_switch(client, org, other_org, outbox):
    # other_org's admin invites org's admin (an existing account: no invite mail, no token).
    r = client.post(
        "/api/v1/members",
        headers=other_org.admin,
        json={"email": org.admin_email, "full_name": "Ada Admin", "role": "VIEWER"},
    )
    assert r.status_code == 201
    assert outbox == []

    r = client.post("/api/v1/auth/switch-org", headers=org.admin, json={"org_slug": other_org.slug})
    assert r.status_code == 200
    switched = auth(r.json()["access_token"])
    assert client.get("/api/v1/auth/me", headers=switched).json()["org"]["role"] == "VIEWER"

    stranger = Org(client)
    assert client.post("/api/v1/auth/switch-org", headers=org.admin, json={"org_slug": stranger.slug}).status_code == 403
