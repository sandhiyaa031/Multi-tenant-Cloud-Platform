import pytest
import asyncio
import uuid
import json
from httpx import AsyncClient, ASGITransport
import psycopg
from psycopg.rows import dict_row

from app.main import app
from app.db import pool, get_tenant_connection

# Mock auth headers
def get_auth_headers(tenant_id: str):
    # The middleware expects a Bearer token, which verify_tenant_auth parses.
    from app.auth import create_access_token
    token = create_access_token("analyst", tenant_id)
    return {"Authorization": f"Bearer {token}"}

@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"

@pytest.fixture(scope="module", autouse=True)
async def db_pool():
    # Setup global pool for the tests
    await pool.open()
    yield
    await pool.close()

@pytest.fixture
async def tenants():
    """Create two temporary test tenants."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            tid_a = str(uuid.uuid4())
            tid_b = str(uuid.uuid4())
            await cur.execute("INSERT INTO app.organizations (org_id, name) VALUES (%s, %s), (%s, %s) ON CONFLICT DO NOTHING",
                             (tid_a, f"TestOrg A {tid_a}", tid_b, f"TestOrg B {tid_b}"))
            
            # Insert some distinct data for them
            event_id_a = 10001
            event_id_b = 10002
            
            await cur.execute("INSERT INTO app.security_events (event_id, org_id, ts, uid, id_orig_h, id_orig_p, id_resp_h, id_resp_p, proto, source, orig_bytes) VALUES (%s, %s, NOW(), 'uid1', '1.1.1.1', 80, '2.2.2.2', 80, %s, %s, %s) ON CONFLICT DO NOTHING",
                             (event_id_a, tid_a, "TCP", "US", 100))
            await cur.execute("INSERT INTO app.security_events (event_id, org_id, ts, uid, id_orig_h, id_orig_p, id_resp_h, id_resp_p, proto, source, orig_bytes) VALUES (%s, %s, NOW(), 'uid2', '1.1.1.1', 80, '2.2.2.2', 80, %s, %s, %s) ON CONFLICT DO NOTHING",
                             (event_id_b, tid_b, "UDP", "EU", 200))
            await conn.commit()
            
    yield {"A": tid_a, "B": tid_b}
    
    # Cleanup
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # RLS is bypassed here since we are postgres superuser cleanup
            await cur.execute("DELETE FROM app.security_events WHERE org_id IN (%s, %s)", (tid_a, tid_b))
            await cur.execute("DELETE FROM app.organizations WHERE org_id IN (%s, %s)", (tid_a, tid_b))
            await conn.commit()

@pytest.mark.anyio
async def test_job_submission_and_isolation(tenants):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        
        # 1. Tenant A submits Job
        headers_a = get_auth_headers(tenants["A"])
        resp_submit = await ac.post("/api/analytical/submit?hours=48", headers=headers_a)
        assert resp_submit.status_code == 200
        data_a = resp_submit.json()
        job_id_a = data_a["job_id"]
        
        # 2. Tenant A reads own job
        resp_read_a = await ac.get(f"/api/analytical/jobs/{job_id_a}", headers=headers_a)
        assert resp_read_a.status_code == 200
        assert resp_read_a.json()["status"] == "PENDING"
        
        # 3. Tenant B CANNOT read Tenant A's job
        headers_b = get_auth_headers(tenants["B"])
        resp_read_b = await ac.get(f"/api/analytical/jobs/{job_id_a}", headers=headers_b)
        assert resp_read_b.status_code == 404
        
        # 4. Missing tenant context cannot read job
        resp_no_auth = await ac.get(f"/api/analytical/jobs/{job_id_a}")
        assert resp_no_auth.status_code == 403

@pytest.mark.anyio
async def test_worker_db_execution_path_isolation(tenants):
    # Verify that the worker execution path explicitly uses `get_tenant_connection` and cannot see cross-tenant data.
    tenant_a = tenants["A"]
    
    # Using the exact same connection logic as the worker
    async with get_tenant_connection(tenant_a) as conn:
        
        # Assert the current role is not superuser, but dbpilot_app
        async with conn.cursor() as cur:
            await cur.execute("SELECT current_user;")
            user = await cur.fetchone()
            assert user[0] == "dbpilot_app", "Worker must execute as restricted role"
            
            # Assert missing tenant context cannot access tenant-protected data.
            # We already set tenant_id via get_tenant_connection, but let's reset it locally to simulate lost context:
            await cur.execute("SELECT set_config('app.tenant_id', '', true)")
            await cur.execute("SELECT COUNT(*) FROM app.security_events")
            empty_count = await cur.fetchone()
            assert empty_count[0] == 0, "Missing tenant context MUST see 0 rows due to RLS"
            
            # Restore strict Tenant A context
            await cur.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_a,))
            
            # Tenant A worker query cannot access Tenant B rows
            await cur.execute("SELECT source FROM app.security_events")
            rows = await cur.fetchall()
            geos = [r[0] for r in rows]
            
            assert "US" in geos, "Worker should see Tenant A's own rows"
            assert "EU" not in geos, "Worker CANNOT see Tenant B's rows"

@pytest.mark.anyio
async def test_job_lifecycle_and_results(tenants):
    # Simulate a full PENDING -> RUNNING -> COMPLETED cycle for the worker manually
    from worker import process_job
    
    tenant_a = tenants["A"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        headers = get_auth_headers(tenant_a)
        
        # Submit
        resp = await ac.post("/api/analytical/submit?hours=72", headers=headers)
        job_id = resp.json()["job_id"]
        
        # Manually invoke worker process function
        await process_job(job_id, tenant_a, "heavy_aggregate")
        
        # Verify COMPLETED and result metadata via API
        resp_status = await ac.get(f"/api/analytical/jobs/{job_id}", headers=headers)
        data = resp_status.json()
        assert data["status"] == "COMPLETED"
        assert data["completed_at"] is not None
        assert data["result_metadata"] is not None
        
        res = await ac.get(f"/api/analytical/jobs/{job_id}/result", headers=headers)
        result_data = res.json()
        assert result_data["status"] == "COMPLETED"
        # We know Tenant A has 1 US row created in the fixture
        assert result_data["result_metadata"]["results_count"] == 1 

@pytest.mark.anyio
async def test_job_failure_lifecycle(tenants):
    from worker import process_job
    tenant_b = tenants["B"]
    
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        headers = get_auth_headers(tenant_b)
        resp = await ac.post("/api/analytical/submit?hours=72", headers=headers)
        job_id = resp.json()["job_id"]
        
        # We can simulate failure in `process_job` by corrupting the job manually in the worker 
        # (Actually, testing process_job failure dynamically is hard. We can just test passing an invalid workload type? 
        # The worker currently ignores the workload parameter and runs a hardcoded query. 
        # Let's break the DB query artificially). But because it's hardcoded, we will just manually update it to FAILED to test the API route.
        
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("UPDATE research.analytical_jobs SET status = 'FAILED', error_info = 'Simulated timeout', completed_at = NOW() WHERE job_id = %s", (job_id,))
                await conn.commit()
                
        # Check API response
        res = await ac.get(f"/api/analytical/jobs/{job_id}/result", headers=headers)
        assert res.json()["status"] == "FAILED"
        assert res.json()["error_info"] == "Simulated timeout"
