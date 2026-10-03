import os
import re
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import mailer
from app.main import app

PASSWORD = "correct-horse-battery"


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def outbox(monkeypatch):
    """Captures mail instead of logging it; each item is (to, subject, body)."""
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: sent.append((to, subject, body)))
    return sent


def token_from(body: str) -> str:
    return re.search(r"token=([\w-]+)", body).group(1)


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class Org:
    """A freshly signed-up organization with helpers to add members and resources."""

    def __init__(self, client: TestClient):
        self.client = client
        self.name = unique("acme")
        self.admin_email = f"{unique('admin')}@example.com"
        r = client.post(
            "/api/v1/auth/signup",
            json={"org_name": self.name, "email": self.admin_email, "password": PASSWORD, "full_name": "Ada Admin"},
        )
        assert r.status_code == 201, r.text
        self.id = r.json()["org"]["id"]
        self.slug = r.json()["org"]["slug"]
        self.admin = auth(r.json()["access_token"])

    def member(self, role: str, outbox: list) -> dict:
        """Invites a new user with `role`, completes the invite, returns their auth header."""
        email = f"{unique(role.lower())}@example.com"
        r = self.client.post(
            "/api/v1/members", headers=self.admin, json={"email": email, "full_name": f"{role} user", "role": role}
        )
        assert r.status_code == 201, r.text
        token = token_from(outbox[-1][2])
        r = self.client.post("/api/v1/auth/reset-password", json={"token": token, "new_password": PASSWORD})
        assert r.status_code == 200, r.text
        r = self.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        return auth(r.json()["access_token"])

    def cluster(self) -> str:
        r = self.client.post(
            "/api/v1/clusters",
            headers=self.admin,
            json={"name": unique("prod"), "pooler_host": "pgbouncer", "pooler_port": 6432, "database_name": "app"},
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def tenant_body(self, cluster_id: str, lo: int, hi: int) -> dict:
        suffix = uuid.uuid4().hex[:8]
        return {
            "cluster_id": cluster_id,
            "name": f"tenant-{suffix}",
            "db_role": f"tenant_{suffix}",
            "warehouse_lo": lo,
            "warehouse_hi": hi,
            "profile": "STEADY_OLTP",
        }


@pytest.fixture
def org(client):
    return Org(client)


@pytest.fixture
def other_org(client):
    return Org(client)


@pytest.fixture
def api_db():
    """A direct connection as the API's own database role, bypassing the HTTP layer."""
    with psycopg.connect(os.environ["CONTROL_DB_API_URL"]) as conn:
        yield conn
        conn.rollback()


@pytest.fixture
def owner_db():
    with psycopg.connect(os.environ["CONTROL_DB_OWNER_URL"]) as conn:
        yield conn
        conn.rollback()
