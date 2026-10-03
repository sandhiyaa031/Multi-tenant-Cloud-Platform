from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app import mailer
from app.config import get_settings
from app.deps import Principal, require_role
from app.security import new_one_time_token

router = APIRouter(prefix="/members", tags=["organization"])

Role = Literal["VIEWER", "OPERATOR", "ADMIN"]


class MemberOut(BaseModel):
    user_id: UUID
    email: str
    full_name: str
    role: Role
    pending: bool
    joined_at: datetime


class InviteIn(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=120)
    role: Role


class RoleIn(BaseModel):
    role: Role


@router.get("", response_model=list[MemberOut])
async def list_members(principal: Principal = Depends(require_role("VIEWER"))):
    async with principal.tx() as conn:
        cur = await conn.execute(
            """
            SELECT u.id AS user_id, u.email, u.full_name, m.role::text AS role,
                   NOT u.has_password AS pending, m.created_at AS joined_at
            FROM cp.memberships m
            JOIN cp.users u ON u.id = m.user_id
            WHERE m.org_id = cp.current_org()
            ORDER BY m.created_at
            """
        )
        return await cur.fetchall()


@router.post("", status_code=status.HTTP_201_CREATED)
async def invite_member(body: InviteIn, principal: Principal = Depends(require_role("ADMIN"))):
    settings = get_settings()
    token, token_hash = new_one_time_token()
    async with principal.tx() as conn:
        cur = await conn.execute(
            "SELECT * FROM cp.invite_member(%s, %s, %s, %s, %s)",
            (body.email, body.full_name, body.role, token_hash, timedelta(hours=settings.invite_token_ttl_hours)),
        )
        row = await cur.fetchone()
    if row["o_needs_password"]:
        mailer.send(
            body.email,
            "You have been invited to DBPilot",
            f"Set your password: {settings.public_web_url}/reset-password?token={token}\n"
            f"This link expires in {settings.invite_token_ttl_hours} hours.",
        )
    return {"user_id": row["o_user_id"], "role": body.role}


@router.patch("/{user_id}")
async def change_role(user_id: UUID, body: RoleIn, principal: Principal = Depends(require_role("ADMIN"))):
    async with principal.tx() as conn:
        cur = await conn.execute(
            "UPDATE cp.memberships SET role = %s WHERE org_id = cp.current_org() AND user_id = %s RETURNING user_id",
            (body.role, user_id),
        )
        if await cur.fetchone() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    return {"user_id": user_id, "role": body.role}


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(user_id: UUID, principal: Principal = Depends(require_role("ADMIN"))):
    async with principal.tx() as conn:
        cur = await conn.execute(
            "DELETE FROM cp.memberships WHERE org_id = cp.current_org() AND user_id = %s RETURNING user_id",
            (user_id,),
        )
        if await cur.fetchone() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
