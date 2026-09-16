import sys
import asyncio
import uuid

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI, Depends, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager
from psycopg.rows import dict_row

from app.db import lifespan, get_tenant_connection, get_raw_connection, pool
from app.auth import verify_tenant_auth, create_access_token
from app.middleware.router import AdaptiveConcurrencyMiddleware, telemetry_loop
from app.middleware.admission_policy import controller

# ── Lifespan ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def app_lifespan(app: FastAPI):
    task = asyncio.create_task(telemetry_loop())
    async with lifespan(app):
        yield
    task.cancel()

# ── App factory ─────────────────────────────────────────────────────────────

app = FastAPI(title="Adaptive Cloud Data Platform API", lifespan=app_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AdaptiveConcurrencyMiddleware)

# ── Request/Response models ──────────────────────────────────────────────────

class LoginRequest(BaseModel):
    org_name: str   # e.g. "org_a"

class RegisterRequest(BaseModel):
    org_name: str   # Chosen display name for the new tenant

class TokenResponse(BaseModel):
    token: str
    org_id: str
    org_name: str

# ── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "api"}

# ── Auth: Login ──────────────────────────────────────────────────────────────

@app.post("/api/auth/login", response_model=TokenResponse)
async def login(req: LoginRequest):
    """
    Looks up the org by name in the database and issues a JWT tied to that org's UUID.
    This replaces the old hardcoded UUID approach.
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT org_id, name FROM app.organizations WHERE LOWER(name) = LOWER(%s);",
                (req.org_name,)
            )
            org = await cur.fetchone()

    if not org:
        raise HTTPException(status_code=401, detail=f"Organization '{req.org_name}' not found. Register first.")

    token = create_access_token(user_id="analyst", tenant_id=str(org["org_id"]))
    return TokenResponse(token=token, org_id=str(org["org_id"]), org_name=org["name"])

# Backwards-compat alias used by the old frontend
@app.post("/api/auth/dev_login")
async def dev_login(req: LoginRequest):
    return await login(req)

# ── Auth: Register ───────────────────────────────────────────────────────────

@app.post("/api/auth/register", response_model=TokenResponse, status_code=201)
async def register(req: RegisterRequest):
    """
    Creates a new tenant organisation in the database and returns a JWT.
    This is a real INSERT INTO app.organizations, not a mock.
    """
    org_name = req.org_name.strip()
    if not org_name:
        raise HTTPException(status_code=400, detail="org_name cannot be empty.")

    new_org_id = str(uuid.uuid4())

    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO app.organizations (org_id, name) VALUES (%s, %s) RETURNING org_id, name;",
                    (new_org_id, org_name)
                )
                row = await cur.fetchone()
            await conn.commit()
    except Exception as e:
        error_str = str(e)
        if "unique" in error_str.lower():
            raise HTTPException(status_code=409, detail=f"Organization '{org_name}' already exists. Please login instead.")
        raise HTTPException(status_code=500, detail=f"Database error: {error_str}")

    token = create_access_token(user_id="analyst", tenant_id=new_org_id)
    return TokenResponse(token=token, org_id=new_org_id, org_name=org_name)

# ── DB Health (RLS Proof) ────────────────────────────────────────────────────

@app.get("/api/db_health")
async def db_health_check(tenant_id: str = Depends(verify_tenant_auth)):
    """
    Proves RLS is active. The query has NO WHERE clause — Postgres enforces it via SET LOCAL.
    """
    async with get_tenant_connection(tenant_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM app.organizations;")
            res = await cur.fetchone()
            cnt = res[0] if res else -1
    return {"status": "ok", "visible_org_count": cnt}

# ── Controller Status (Live Telemetry) ───────────────────────────────────────

@app.get("/api/controller_status")
async def controller_status(tenant_id: str = Depends(verify_tenant_auth)):
    """
    Returns real-time state of the Adaptive Concurrency Controller.
    The frontend polls this every 2 seconds to draw the live latency dashboard.
    """
    return {
        "is_paused": controller.is_paused,
        "state": "THROTTLED" if controller.is_paused else "HEALTHY",
        "l_current_p95_ms": round(controller.L_current_p95, 2),
        "l_slo_ms": controller.L_slo,
        "i_active": controller.I_active,
        "a_active": controller.A_active,
        "consecutive_healthy_windows": controller.consecutive_healthy_windows,
    }

# ── Workload Endpoints ────────────────────────────────────────────────────────

@app.get("/api/investigate")
async def interactive_investigate(
    ip: str = Query(..., description="Target IP to investigate"),
    tenant_id: str = Depends(verify_tenant_auth)
):
    """
    Latency-sensitive B-Tree index lookup on source IP.
    Tagged as Interactive by the middleware — fast-tracked, never queued.
    """
    async with get_tenant_connection(tenant_id) as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute('''
                SELECT ts, uid, id_resp_h, proto, orig_bytes, conn_state
                FROM app.security_events
                WHERE id_orig_h = %s
                ORDER BY ts DESC
                LIMIT 50
            ''', (ip,))
            rows = await cur.fetchall()
            return {"status": "success", "type": "interactive", "results": len(rows), "data": rows}

@app.get("/api/aggregate")
async def analytical_aggregate(
    hours: int = Query(24, description="Hours of data to scan"),
    tenant_id: str = Depends(verify_tenant_auth)
):
    """
    Heavy GROUP BY sweep over the full event table.
    Tagged as Analytical — may be queued by the controller under load.
    """
    async with get_tenant_connection(tenant_id) as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute('''
                SELECT source, proto, count(*) as flow_count, sum(orig_bytes) as total_bytes
                FROM app.security_events
                WHERE ts >= NOW() - (%s * INTERVAL '1 hour')
                GROUP BY source, proto
                ORDER BY flow_count DESC
            ''', (hours,))
            rows = await cur.fetchall()
            return {"status": "success", "type": "analytical", "aggregated_groups": len(rows), "data": rows}

# ── Experiment Results ────────────────────────────────────────────────────────

@app.get("/api/experiment/results")
async def experiment_results(
    limit: int = Query(100),
    tenant_id: str = Depends(verify_tenant_auth)
):
    """
    Returns the last N recorded latency observations from the research schema.
    Used by the dashboard to plot the before/after chart.
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("""
                SELECT query_type, latency_ms, queue_time_ms, timestamp
                FROM research.query_observations
                ORDER BY timestamp DESC
                LIMIT %s;
            """, (limit,))
            observations = await cur.fetchall()

            await cur.execute("""
                SELECT action_taken, trigger_metric, timestamp
                FROM research.controller_decisions
                ORDER BY timestamp DESC
                LIMIT 50;
            """)
            decisions = await cur.fetchall()

    return {
        "observations": observations,
        "controller_decisions": decisions
    }

# ── Cloud Elasticity: Intelligent Control Plane & Job Tracking ──────────────

@app.post("/api/analytical/submit")
async def submit_analytical_job(
    request: Request,
    hours: int = Query(72, description="Hours of data to scan"),
    tenant_id: str = Depends(verify_tenant_auth)
):
    """
    Intelligent Cloud Control Plane Route
    Analyzes expected query cost. If heavy (e.g. > 24 hours), 
    offloads to the isolated async Elastic Analytical Tier.
    """
    # 1. Tenant-Aware Routing Decision
    if hours > 24:
        routing_decision = "OFFLOADED"
        workload_type = "heavy_aggregate"
    else:
        # For simplicity in this endpoint, everything hitting 'submit' could be queued, 
        # but to prove routing, we explicitly label it.
        routing_decision = "SHARED"
        workload_type = "light_aggregate"
        
    try:
        # If we had native postgres, we'd queue the job regardless, 
        # but let's queue it so the worker can pick it up.
        async with get_tenant_connection(tenant_id) as conn:
            async with conn.cursor() as cur:
                await cur.execute('''
                    INSERT INTO research.analytical_jobs 
                    (tenant_id, workload_type, routing_decision, status)
                    VALUES (%s, %s, %s, 'PENDING')
                    RETURNING job_id
                ''', (tenant_id, workload_type, 'OFFLOADED' if hours>24 else routing_decision))
                
                job_id = (await cur.fetchone())[0]
                await conn.commit()
                
        if routing_decision == "OFFLOADED":
            return {
                "status": "ACCEPTED", 
                "routing_decision": routing_decision, 
                "job_id": job_id, 
                "message": f"Query cost too high. Isolated compute container provisioning..."
            }
        else:
            # Here we might synchronously run it, but for architecture purity, 
            # we just notify it's in the shared queue vs isolated queue.
            return {
                "status": "ACCEPTED", 
                "routing_decision": routing_decision, 
                "job_id": job_id, 
                "message": f"Query permitted on shared tier. Processing..."
            }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/analytical/jobs/{job_id}")
async def get_analytical_job_status(
    job_id: str, 
    tenant_id: str = Depends(verify_tenant_auth)
):
    try:
        async with get_tenant_connection(tenant_id) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute('''
                    SELECT job_id, workload_type, routing_decision, status, 
                           created_at, started_at, completed_at, result_metadata, error_info
                    FROM research.analytical_jobs
                    WHERE job_id = %s
                ''', (job_id,))
                
                row = await cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Job not found or access denied by RLS.")
                    
                if row["started_at"] and row["completed_at"]:
                    row["execution_duration_s"] = (row["completed_at"] - row["started_at"]).total_seconds()
                    
                return row
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

