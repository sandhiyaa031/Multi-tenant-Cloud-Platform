import re
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app import mailer
from app.config import get_settings
from app.db import org_tx, system_tx
from app.deps import Principal, get_principal
from app.security import (
    create_access_token,
    hash_one_time_token,
    hash_password,
    new_one_time_token,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

Password = Field(min_length=10, max_length=200)


class SignupIn(BaseModel):
    org_name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    password: str = Password
    full_name: str = Field(min_length=1, max_length=120)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=200)
    org_slug: str | None = None


class OrgOut(BaseModel):
    id: UUID
    slug: str
    name: str
    role: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    org: OrgOut
    orgs: list[OrgOut]


class ForgotIn(BaseModel):
    email: EmailStr


class ResetIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    new_password: str = Password


class SwitchOrgIn(BaseModel):
    org_slug: str


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")


async def _memberships(conn, user_id: UUID) -> list[OrgOut]:
    cur = await conn.execute("SELECT * FROM cp.auth_memberships(%s)", (user_id,))
    return [
        OrgOut(id=r["o_org_id"], slug=r["o_slug"], name=r["o_name"], role=r["o_role"]) for r in await cur.fetchall()
    ]


def _pick_org(orgs: list[OrgOut], slug: str | None) -> OrgOut:
    if not orgs:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this account belongs to no organization")
    if slug is None:
        return orgs[0]
    for org in orgs:
        if org.slug == slug.lower():
            return org
    raise HTTPException(status.HTTP_403_FORBIDDEN, "not a member of that organization")


@router.post("/signup", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
async def signup(body: SignupIn):
    slug = slugify(body.org_name)
    if len(slug) < 2:
        raise HTTPException(422, "organization name needs letters or digits")
    async with system_tx() as conn:
        cur = await conn.execute(
            "SELECT * FROM cp.signup(%s, %s, %s, %s, %s)",
            (body.org_name, slug, body.email, hash_password(body.password), body.full_name),
        )
        row = await cur.fetchone()
        orgs = await _memberships(conn, row["o_user_id"])
    org = _pick_org(orgs, slug)
    return TokenOut(access_token=create_access_token(row["o_user_id"], org.id), org=org, orgs=orgs)


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn):
    async with system_tx() as conn:
        cur = await conn.execute("SELECT * FROM cp.auth_get_user(%s)", (body.email,))
        user = await cur.fetchone()
        password_ok = verify_password(user["o_password_hash"] if user else None, body.password)
        if not user or not password_ok or not user["o_is_active"]:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "incorrect email or password")
        orgs = await _memberships(conn, user["o_user_id"])
    org = _pick_org(orgs, body.org_slug)

    async with org_tx(user["o_user_id"], org.id) as conn:
        await conn.execute("SELECT cp.audit('auth.login', 'users', %s, '{}'::jsonb)", (str(user["o_user_id"]),))
    return TokenOut(access_token=create_access_token(user["o_user_id"], org.id), org=org, orgs=orgs)


@router.post("/switch-org", response_model=TokenOut)
async def switch_org(body: SwitchOrgIn, principal: Principal = Depends(get_principal)):
    async with system_tx() as conn:
        orgs = await _memberships(conn, principal.user_id)
    org = _pick_org(orgs, body.org_slug)
    return TokenOut(access_token=create_access_token(principal.user_id, org.id), org=org, orgs=orgs)


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(body: ForgotIn):
    settings = get_settings()
    token, token_hash = new_one_time_token()
    async with system_tx() as conn:
        cur = await conn.execute(
            "SELECT cp.create_reset_token(%s, %s, %s) AS created",
            (body.email, token_hash, timedelta(minutes=settings.reset_token_ttl_minutes)),
        )
        created = (await cur.fetchone())["created"]
    if created:
        mailer.send(
            body.email,
            "Reset your DBPilot password",
            f"{settings.public_web_url}/reset-password?token={token}\n"
            f"This link expires in {settings.reset_token_ttl_minutes} minutes.",
        )
    # Same answer whether or not the address exists, so this cannot be used to probe for accounts.
    return {"detail": "if that address has an account, a reset link has been sent"}


@router.post("/reset-password")
async def reset_password(body: ResetIn):
    async with system_tx() as conn:
        cur = await conn.execute(
            "SELECT cp.consume_token(%s, %s) AS user_id",
            (hash_one_time_token(body.token), hash_password(body.new_password)),
        )
        user_id = (await cur.fetchone())["user_id"]
    if user_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "this link is invalid or has expired")
    return {"detail": "password updated"}


class MeOut(BaseModel):
    user_id: UUID
    email: str
    full_name: str
    org: OrgOut


@router.get("/me", response_model=MeOut)
async def me(principal: Principal = Depends(get_principal)):
    async with principal.tx() as conn:
        cur = await conn.execute(
            """
            SELECT u.email, u.full_name, o.id, o.slug, o.name
            FROM cp.users u, cp.organizations o
            WHERE u.id = cp.current_user_id() AND o.id = cp.current_org()
            """
        )
        row = await cur.fetchone()
    return MeOut(
        user_id=principal.user_id,
        email=row["email"],
        full_name=row["full_name"],
        org=OrgOut(id=row["id"], slug=row["slug"], name=row["name"], role=principal.role),
    )
