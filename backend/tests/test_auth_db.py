import pytest
import asyncio
import sys
from fastapi.testclient import TestClient
from app.main import app
from app.auth import get_dev_token_for_tenant
from app.db import pool, get_tenant_connection

def test_health():
    # Sync TestClient perfectly manages synchronous non-db routes
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "api"}

def test_tenant_boundaries_via_db():
    """
    Directly tests the translation of Tenant IDs to PostgreSQL RLS contexts
    using pure AsyncIO to bypass Windows/Starlette AnyIO deadlocks.
    """
    async def run_isolation():
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            
        await pool.open()
        
        # Test Alice boundaries
        alice_org = "11111111-1111-1111-1111-111111111111"
        try:
            async with get_tenant_connection(alice_org) as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT count(*) FROM app.organizations;")
                    res = await cur.fetchone()
                    assert res[0] == 1, "Alice should see precisely 1 organization"
                    
                    await cur.execute("SELECT org_id FROM app.organizations;")
                    row = await cur.fetchone()
                    assert str(row[0]) == alice_org, "Data leak detected!"
        finally:
            await pool.close()
            
    asyncio.run(run_isolation())
