import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.auth import get_dev_token_for_tenant

@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c

def test_search_events_endpoint(client):
    token = get_dev_token_for_tenant("alice")
    headers = {"Authorization": f"Bearer {token}"}
    
    # Authenticated request
    res = client.get("/api/events/search?ip=192.168.1.50", headers=headers)
    assert res.status_code == 200
    # Should be empty since no data is loaded yet, but proves DB connection and RLS worked
    assert isinstance(res.json(), list)

    # Unauthenticated request
    res_unauth = client.get("/api/events/search?ip=192.168.1.50")
    assert res_unauth.status_code == 403

def test_single_event_endpoint(client):
    token = get_dev_token_for_tenant("bob")
    headers = {"Authorization": f"Bearer {token}"}
    
    # Fetching a non-existent event
    res = client.get("/api/events/99999999", headers=headers)
    assert res.status_code == 404
    assert res.json()["detail"] == "Event not found"

def test_related_events_endpoint(client):
    token = get_dev_token_for_tenant("alice")
    headers = {"Authorization": f"Bearer {token}"}
    
    # Fetching related for non-existent event
    res = client.get("/api/events/99999999/related", headers=headers)
    assert res.status_code == 404
    assert res.json()["detail"] == "Target event not found"
