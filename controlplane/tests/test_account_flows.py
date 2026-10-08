"""Account flows the console depends on: what the sign-in pages are told, slowing down password
guessing, switching sign-up off, and getting a password link to someone when no mail can be sent."""
import dataclasses

from app import mailer
from app.config import get_settings
from app.routers import auth as auth_router
from tests.conftest import PASSWORD, auth, token_from, unique


def invite(client, org, role="VIEWER"):
    email = f"{unique('member')}@example.com"
    r = client.post("/api/v1/members", headers=org.admin, json={"email": email, "full_name": "New Member", "role": role})
    assert r.status_code == 201, r.text
    return email, r.json()


def test_public_config_says_what_the_sign_in_pages_can_offer(client, monkeypatch):
    assert client.get("/api/v1/auth/config").json() == {"signup_enabled": True, "mail_delivery": False}
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    assert client.get("/api/v1/auth/config").json()["mail_delivery"] is True


def test_signup_can_be_switched_off(client, monkeypatch):
    closed = dataclasses.replace(get_settings(), signup_enabled=False)
    monkeypatch.setattr(auth_router, "get_settings", lambda: closed)
    r = client.post("/api/v1/auth/signup", json={"org_name": unique("closed"), "email": f"{unique('u')}@example.com",
                                                 "password": PASSWORD, "full_name": "X"})
    assert r.status_code == 403
    assert client.get("/api/v1/auth/config").json()["signup_enabled"] is False


def test_repeated_failed_sign_ins_are_refused_for_a_while_even_with_the_right_password(client, org):
    for _ in range(auth_router.LOGIN_MAX_FAILURES):
        assert client.post("/api/v1/auth/login", json={"email": org.admin_email, "password": "wrong-password"}).status_code == 401
    r = client.post("/api/v1/auth/login", json={"email": org.admin_email, "password": PASSWORD})
    assert r.status_code == 429
    # Only that address is held back, and only for the window.
    other = f"{unique('someone')}@example.com"
    assert client.post("/api/v1/auth/login", json={"email": other, "password": "wrong-password"}).status_code == 401
    auth_router._failures[org.admin_email.lower()].clear()
    assert client.post("/api/v1/auth/login", json={"email": org.admin_email, "password": PASSWORD}).status_code == 200


def test_a_successful_sign_in_forgets_earlier_failures(client, org):
    for _ in range(auth_router.LOGIN_MAX_FAILURES - 1):
        client.post("/api/v1/auth/login", json={"email": org.admin_email, "password": "wrong-password"})
    assert client.post("/api/v1/auth/login", json={"email": org.admin_email, "password": PASSWORD}).status_code == 200
    assert client.post("/api/v1/auth/login", json={"email": org.admin_email, "password": "wrong-password"}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": org.admin_email, "password": PASSWORD}).status_code == 200


def test_me_lists_every_organization_the_user_belongs_to(client, org, other_org, outbox):
    client.post("/api/v1/members", headers=other_org.admin,
                json={"email": org.admin_email, "full_name": "Ada Admin", "role": "VIEWER"})
    me = client.get("/api/v1/auth/me", headers=org.admin).json()
    assert {o["slug"]: o["role"] for o in me["orgs"]} == {org.slug: "ADMIN", other_org.slug: "VIEWER"}


def test_without_mail_the_inviting_admin_gets_the_link_and_it_works(client, org, outbox):
    email, body = invite(client, org)
    assert body["delivered"] is False and body["needs_password"] is True
    assert token_from(body["link"]) == token_from(outbox[-1][2])
    r = client.post("/api/v1/auth/reset-password", json={"token": token_from(body["link"]), "new_password": PASSWORD})
    assert r.status_code == 200
    assert client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200


def test_with_mail_configured_the_link_is_not_shown_to_the_admin(client, org, outbox, monkeypatch):
    monkeypatch.setattr(mailer, "configured", lambda: True)
    _, body = invite(client, org)
    assert body["delivered"] is True and body["link"] is None and len(outbox) == 1


def test_admin_can_issue_a_password_link_for_a_member_when_there_is_no_mail(client, org, outbox):
    member = org.member("OPERATOR", outbox)
    user_id = client.get("/api/v1/auth/me", headers=member).json()["user_id"]
    email = client.get("/api/v1/auth/me", headers=member).json()["email"]

    assert client.post(f"/api/v1/members/{user_id}/password-link", headers=member).status_code == 403  # admins only
    r = client.post(f"/api/v1/members/{user_id}/password-link", headers=org.admin)
    assert r.status_code == 200, r.text
    new_password = "another-long-password"
    assert client.post("/api/v1/auth/reset-password",
                       json={"token": token_from(r.json()["link"]), "new_password": new_password}).status_code == 200
    assert client.post("/api/v1/auth/login", json={"email": email, "password": new_password}).status_code == 200
    # Single use, and recorded.
    assert client.post("/api/v1/auth/reset-password",
                       json={"token": token_from(r.json()["link"]), "new_password": PASSWORD}).status_code == 400
    assert "members.password_link" in [e["action"] for e in client.get("/api/v1/audit", headers=org.admin).json()]


def test_password_link_is_refused_across_organizations_and_when_mail_works(client, org, other_org, outbox, monkeypatch):
    stranger = client.get("/api/v1/auth/me", headers=other_org.admin).json()["user_id"]
    assert client.post(f"/api/v1/members/{stranger}/password-link", headers=org.admin).status_code == 404

    # A member who also belongs to another organization: an admin here must not be able to take the account over.
    client.post("/api/v1/members", headers=org.admin,
                json={"email": other_org.admin_email, "full_name": "Shared", "role": "VIEWER"})
    r = client.post(f"/api/v1/members/{stranger}/password-link", headers=org.admin)
    assert r.status_code == 409 and "another organization" in r.json()["detail"]

    member = org.member("VIEWER", outbox)
    user_id = client.get("/api/v1/auth/me", headers=member).json()["user_id"]
    monkeypatch.setattr(mailer, "configured", lambda: True)
    assert client.post(f"/api/v1/members/{user_id}/password-link", headers=org.admin).status_code == 409
