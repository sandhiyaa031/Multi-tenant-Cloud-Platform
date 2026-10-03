from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.deps import Principal, require_role

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditOut(BaseModel):
    id: int
    actor_user_id: UUID | None
    actor_email: str | None
    action: str
    entity_type: str
    entity_id: str | None
    detail: dict[str, Any]
    created_at: datetime


@router.get("", response_model=list[AuditOut])
async def list_audit(
    before_id: int | None = Query(default=None, description="return entries older than this id"),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require_role("VIEWER")),
):
    # Keyset pagination: "id < before_id ORDER BY id DESC" walks the
    # (org_id, id DESC) index and stays fast however deep the caller pages.
    async with principal.tx() as conn:
        cur = await conn.execute(
            """
            SELECT a.id, a.actor_user_id, u.email AS actor_email, a.action, a.entity_type,
                   a.entity_id, a.detail, a.created_at
            FROM cp.audit_log a
            LEFT JOIN cp.users u ON u.id = a.actor_user_id
            WHERE a.org_id = cp.current_org() AND (%s::bigint IS NULL OR a.id < %s)
            ORDER BY a.id DESC
            LIMIT %s
            """,
            (before_id, before_id, limit),
        )
        return await cur.fetchall()
