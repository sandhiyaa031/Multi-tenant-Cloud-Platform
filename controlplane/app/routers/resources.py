"""Clusters, tenants and SLOs: the resources an organization manages."""
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.deps import Principal, require_role

router = APIRouter(tags=["resources"])

NAME = r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$"


class ClusterIn(BaseModel):
    name: str = Field(pattern=NAME)
    pooler_host: str = Field(min_length=1, max_length=253)
    pooler_port: int = Field(ge=1, le=65535)
    database_name: str = Field(min_length=1, max_length=63)
    # Where the telemetry collector reaches the primary; without it the cluster is not observed.
    primary_host: str | None = Field(default=None, max_length=253)
    primary_port: int | None = Field(default=None, ge=1, le=65535)


class ClusterOut(ClusterIn):
    id: UUID
    status: str
    created_at: datetime


class TenantIn(BaseModel):
    cluster_id: UUID
    name: str = Field(pattern=NAME)
    db_role: str = Field(pattern=r"^[a-z][a-z0-9_]{2,62}$")
    warehouse_lo: int = Field(ge=1)
    warehouse_hi: int = Field(ge=1)
    profile: Literal["STEADY_OLTP", "BURSTY_OLTP", "ANALYTICAL", "MIXED"]


class TenantOut(TenantIn):
    id: UUID
    created_at: datetime


class SloIn(BaseModel):
    query_class: Literal["OLTP", "OLAP"]
    percentile: Literal[50, 95, 99]
    threshold_ms: Decimal = Field(gt=0)
    target_ratio: Decimal = Field(default=Decimal("0.99"), ge=Decimal("0.5"), le=1)


class SloOut(SloIn):
    id: UUID
    tenant_id: UUID


@router.get("/clusters", response_model=list[ClusterOut])
async def list_clusters(principal: Principal = Depends(require_role("VIEWER"))):
    async with principal.tx() as conn:
        cur = await conn.execute("SELECT * FROM cp.clusters ORDER BY created_at")
        return await cur.fetchall()


@router.post("/clusters", response_model=ClusterOut, status_code=status.HTTP_201_CREATED)
async def create_cluster(body: ClusterIn, principal: Principal = Depends(require_role("ADMIN"))):
    async with principal.tx() as conn:
        cur = await conn.execute(
            """
            INSERT INTO cp.clusters (org_id, name, pooler_host, pooler_port, database_name,
                                     primary_host, primary_port)
            VALUES (cp.current_org(), %s, %s, %s, %s, %s, %s) RETURNING *
            """,
            (body.name, body.pooler_host, body.pooler_port, body.database_name,
             body.primary_host, body.primary_port),
        )
        return await cur.fetchone()


@router.get("/tenants", response_model=list[TenantOut])
async def list_tenants(
    cluster_id: UUID | None = Query(default=None), principal: Principal = Depends(require_role("VIEWER"))
):
    async with principal.tx() as conn:
        cur = await conn.execute(
            "SELECT * FROM cp.tenants WHERE %s::uuid IS NULL OR cluster_id = %s ORDER BY cluster_id, warehouse_lo",
            (cluster_id, cluster_id),
        )
        return await cur.fetchall()


@router.post("/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def create_tenant(body: TenantIn, principal: Principal = Depends(require_role("OPERATOR"))):
    if body.warehouse_hi < body.warehouse_lo:
        raise HTTPException(422, "warehouse_hi must be >= warehouse_lo")
    async with principal.tx() as conn:
        cur = await conn.execute(
            """
            INSERT INTO cp.tenants (org_id, cluster_id, name, db_role, warehouse_lo, warehouse_hi, profile)
            VALUES (cp.current_org(), %s, %s, %s, %s, %s, %s) RETURNING *
            """,
            (body.cluster_id, body.name, body.db_role, body.warehouse_lo, body.warehouse_hi, body.profile),
        )
        return await cur.fetchone()


@router.delete("/tenants/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tenant(tenant_id: UUID, principal: Principal = Depends(require_role("OPERATOR"))):
    async with principal.tx() as conn:
        cur = await conn.execute("DELETE FROM cp.tenants WHERE id = %s RETURNING id", (tenant_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")


@router.get("/tenants/{tenant_id}/slos", response_model=list[SloOut])
async def list_slos(tenant_id: UUID, principal: Principal = Depends(require_role("VIEWER"))):
    async with principal.tx() as conn:
        cur = await conn.execute(
            "SELECT * FROM cp.slos WHERE tenant_id = %s ORDER BY query_class, percentile", (tenant_id,)
        )
        return await cur.fetchall()


@router.put("/tenants/{tenant_id}/slos", response_model=SloOut)
async def upsert_slo(tenant_id: UUID, body: SloIn, principal: Principal = Depends(require_role("OPERATOR"))):
    async with principal.tx() as conn:
        # The tenant lookup goes through RLS: another organization's tenant id is simply not found.
        cur = await conn.execute("SELECT 1 FROM cp.tenants WHERE id = %s", (tenant_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")
        cur = await conn.execute(
            """
            INSERT INTO cp.slos (org_id, tenant_id, query_class, percentile, threshold_ms, target_ratio)
            VALUES (cp.current_org(), %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, query_class, percentile)
            DO UPDATE SET threshold_ms = EXCLUDED.threshold_ms, target_ratio = EXCLUDED.target_ratio
            RETURNING *
            """,
            (tenant_id, body.query_class, body.percentile, body.threshold_ms, body.target_ratio),
        )
        return await cur.fetchone()
