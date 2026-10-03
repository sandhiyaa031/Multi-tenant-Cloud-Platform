import logging
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import pool
from app.routers import audit, auth, members, resources, telemetry

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dbpilot.api")

# Constraint name -> message a user can act on. Anything not listed gets a generic message,
# so internal names never leak.
CONSTRAINT_MESSAGES = {
    "organizations_slug_key": "an organization with that name already exists",
    "users_email_key": "an account with that email already exists",
    "memberships_pkey": "that user is already a member",
    "clusters_org_id_name_key": "a cluster with that name already exists",
    "tenants_cluster_id_name_key": "a tenant with that name already exists in this cluster",
    "tenants_cluster_id_db_role_key": "that database role is already used in this cluster",
    "tenants_no_overlapping_ranges": "warehouse range overlaps another tenant in this cluster",
    "tenants_cluster_id_org_id_fkey": "cluster not found",
}
# SQLSTATE -> HTTP status.
SQLSTATE_STATUS = {
    "23505": status.HTTP_409_CONFLICT,  # unique_violation
    "23P01": status.HTTP_409_CONFLICT,  # exclusion_violation
    "23503": status.HTTP_409_CONFLICT,  # foreign_key_violation
    "23514": 422,  # check_violation
    "42501": status.HTTP_403_FORBIDDEN,  # insufficient_privilege, incl. RLS WITH CHECK
    "DP001": status.HTTP_409_CONFLICT,  # last ADMIN guard
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool.open()
    yield
    await pool.close()


app = FastAPI(title="DBPilot control plane", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_settings().cors_origins),
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.exception_handler(psycopg.Error)
async def database_error(request: Request, exc: psycopg.Error):
    code = SQLSTATE_STATUS.get(exc.sqlstate or "")
    if code is None:
        log.exception("unhandled database error", exc_info=exc)
        return JSONResponse({"detail": "internal error"}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    if exc.sqlstate == "DP001":
        detail = "an organization must keep at least one ADMIN"
    elif exc.sqlstate == "42501":
        detail = "not permitted"
    else:
        detail = CONSTRAINT_MESSAGES.get(exc.diag.constraint_name or "", "request conflicts with existing data")
    return JSONResponse({"detail": detail}, status_code=code)


@app.get("/healthz", tags=["health"])
async def healthz():
    async with pool.connection() as conn:
        await conn.execute("SELECT 1")
    return {"status": "ok"}


API_PREFIX = "/api/v1"
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(members.router, prefix=API_PREFIX)
app.include_router(resources.router, prefix=API_PREFIX)
app.include_router(audit.router, prefix=API_PREFIX)
app.include_router(telemetry.router, prefix=API_PREFIX)
