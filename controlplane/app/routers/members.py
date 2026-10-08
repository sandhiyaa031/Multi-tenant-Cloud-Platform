from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app import mailer
from app.config import get_settings
from app.db import system_tx
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
    link = None
    if row["o_needs_password"]:
        link = f"{settings.public_web_url}/reset-password?token={token}"
        mailer.send(
            body.email,
            "You have been invited to DBPilot",
            f"Set your password: {link}\nThis link expires in {settings.invite_token_ttl_hours} hours.",
        )
    # Without a mail server nothing was delivered: the admin who invited gets the link to pass on.
    return {"user_id": row["o_user_id"], "role": body.role, "needs_password": row["o_needs_password"],
            "delivered": mailer.configured(), "link": None if mailer.configured() else link}


@router.post("/{user_id}/password-link")
async def password_link(user_id: UUID, principal: Principal = Depends(require_role("ADMIN"))):
    """A single-use link with which a member sets a new password, for deployments without a mail
    server, where "forgot password" cannot reach anyone. Refused when mail works (the member can
    ask for a link themselves) and for an account that also belongs to another organization (an
    admin here must not be able to take over an account other organizations rely on)."""
    settings = get_settings()
    if mailer.configured():
        raise HTTPException(status.HTTP_409_CONFLICT, "mail delivery is configured; the member can use 'Forgot password'")
    token, token_hash = new_one_time_token()
    async with principal.tx() as conn:
        cur = await conn.execute(
            "SELECT u.email FROM cp.memberships m JOIN cp.users u ON u.id = m.user_id"
            " WHERE m.org_id = cp.current_org() AND m.user_id = %s", (user_id,))
        member = await cur.fetchone()
        if member is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
        cur = await conn.execute("SELECT count(*) AS n FROM cp.auth_memberships(%s)", (user_id,))
        if (await cur.fetchone())["n"] > 1:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "this account also belongs to another organization; a link cannot be issued here")
        await conn.execute("SELECT cp.audit('members.password_link', 'users', %s, '{}'::jsonb)", (str(user_id),))
    async with system_tx() as conn:
        cur = await conn.execute(
            "SELECT cp.create_reset_token(%s, %s, %s) AS created",
            (member["email"], token_hash, timedelta(minutes=settings.reset_token_ttl_minutes)))
        if not (await cur.fetchone())["created"]:
            raise HTTPException(status.HTTP_409_CONFLICT, "this account is not active")
    return {"link": f"{settings.public_web_url}/reset-password?token={token}",
            "expires_minutes": settings.reset_token_ttl_minutes}


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
