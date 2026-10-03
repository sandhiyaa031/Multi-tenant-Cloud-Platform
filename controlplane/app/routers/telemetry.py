"""Read side of the telemetry the collector writes. Everything here is computed
from stored measurements; nothing is synthesised."""
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.deps import Principal, require_role

router = APIRouter(prefix="/clusters/{cluster_id}", tags=["telemetry"])


class TopQuery(BaseModel):
    tenant_id: UUID
    tenant: str
    queryid: str  # 64-bit; sent as text because JavaScript numbers cannot hold it exactly
    query: str
    calls: int
    total_exec_ms: float
    mean_exec_ms: float
    time_share: float  # fraction of all execution time in the window
    rows: int
    shared_blks_read: int
    temp_blks_written: int
    wal_bytes: Decimal


class TenantLoadPoint(BaseModel):
    tenant_id: UUID
    tenant: str
    window_end: datetime
    window_seconds: float
    calls: int
    total_exec_ms: float
    wal_bytes: Decimal


class InstancePoint(BaseModel):
    window_end: datetime
    window_seconds: float
    xact_commit: int
    xact_rollback: int
    blks_read: int
    blks_hit: int
    temp_bytes: int
    deadlocks: int
    wal_bytes: Decimal
    active_connections: int
    replica_lag_bytes: int | None
    database_bytes: int


@router.get("/top-queries", response_model=list[TopQuery])
async def top_queries(
    cluster_id: UUID,
    minutes: int = Query(default=15, ge=1, le=1440),
    tenant_id: UUID | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(require_role("VIEWER")),
):
    async with principal.tx() as conn:
        cur = await conn.execute(
            """
            SELECT s.tenant_id, t.name AS tenant, s.queryid::text AS queryid, f.query,
                   sum(s.calls)::bigint AS calls,
                   sum(s.total_exec_ms) AS total_exec_ms,
                   sum(s.total_exec_ms) / sum(s.calls) AS mean_exec_ms,
                   sum(s.total_exec_ms) / NULLIF(sum(sum(s.total_exec_ms)) OVER (), 0) AS time_share,
                   sum(s.rows)::bigint AS rows,
                   sum(s.shared_blks_read)::bigint AS shared_blks_read,
                   sum(s.temp_blks_written)::bigint AS temp_blks_written,
                   sum(s.wal_bytes) AS wal_bytes
            FROM cp.query_stats s
            JOIN cp.tenants t ON t.id = s.tenant_id
            JOIN cp.query_fingerprints f ON f.cluster_id = s.cluster_id AND f.queryid = s.queryid
            WHERE s.cluster_id = %s
              AND s.window_end > now() - make_interval(mins => %s)
              AND (%s::uuid IS NULL OR s.tenant_id = %s)
            GROUP BY s.tenant_id, t.name, s.queryid, f.query
            ORDER BY total_exec_ms DESC
            LIMIT %s
            """,
            (cluster_id, minutes, tenant_id, tenant_id, limit),
        )
        return await cur.fetchall()


@router.get("/tenant-load", response_model=list[TenantLoadPoint])
async def tenant_load(
    cluster_id: UUID,
    minutes: int = Query(default=60, ge=1, le=1440),
    principal: Principal = Depends(require_role("VIEWER")),
):
    async with principal.tx() as conn:
        cur = await conn.execute(
            """
            SELECT s.tenant_id, t.name AS tenant, s.window_end,
                   extract(epoch FROM max(s.window_end - s.window_start)) AS window_seconds,
                   sum(s.calls)::bigint AS calls, sum(s.total_exec_ms) AS total_exec_ms,
                   sum(s.wal_bytes) AS wal_bytes
            FROM cp.query_stats s
            JOIN cp.tenants t ON t.id = s.tenant_id
            WHERE s.cluster_id = %s AND s.window_end > now() - make_interval(mins => %s)
            GROUP BY s.tenant_id, t.name, s.window_end
            ORDER BY s.window_end, t.name
            """,
            (cluster_id, minutes),
        )
        return await cur.fetchall()


@router.get("/instance", response_model=list[InstancePoint])
async def instance(
    cluster_id: UUID,
    minutes: int = Query(default=60, ge=1, le=1440),
    principal: Principal = Depends(require_role("VIEWER")),
):
    async with principal.tx() as conn:
        cur = await conn.execute(
            """
            SELECT window_end, extract(epoch FROM window_end - window_start) AS window_seconds,
                   xact_commit, xact_rollback, blks_read, blks_hit, temp_bytes, deadlocks, wal_bytes,
                   active_connections, replica_lag_bytes, database_bytes
            FROM cp.instance_stats
            WHERE cluster_id = %s AND window_end > now() - make_interval(mins => %s)
            ORDER BY window_end
            """,
            (cluster_id, minutes),
        )
        return await cur.fetchall()
