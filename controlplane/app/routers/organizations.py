"""Organizations: the customers of DBPilot itself, as opposed to the tenants inside a cluster.

An organization owns members (with roles), clusters (a registered PostgreSQL deployment) and,
through them, tenants (the database roles inside a cluster that DBPilot observes). Creating an
organization creates rows in the control-plane database and nothing else: it does not create a
cluster, a database role or a tenant. Those are registered afterwards, and registering them
records where an existing data plane is; it does not build one.
"""
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.config import get_settings
from app.db import system_tx
from app.deps import Principal, get_principal, require_role
from app.routers.auth import TokenOut, _memberships, _pick_org, slugify
from app.security import create_access_token

router = APIRouter(prefix="/orgs", tags=["organizations"])


class OrgSummary(BaseModel):
    id: UUID
    slug: str
    name: str
    role: str
    created_at: datetime
    members: int
    clusters: int       # registered and not retired
    tenants: int        # tenants of those clusters
    current: bool       # the one this session is acting in


class OrgIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)


async def _summaries(principal: Principal) -> list[OrgSummary]:
    async with system_tx() as conn:
        cur = await conn.execute("SELECT * FROM cp.my_organizations(%s)", (principal.user_id,))
        rows = await cur.fetchall()
    return [OrgSummary(id=r["o_org_id"], slug=r["o_slug"], name=r["o_name"], role=r["o_role"], created_at=r["o_created_at"],
                       members=r["o_members"], clusters=r["o_clusters"], tenants=r["o_tenants"],
                       current=r["o_org_id"] == principal.org_id) for r in rows]


@router.get("", response_model=list[OrgSummary])
async def list_organizations(principal: Principal = Depends(get_principal)):
    """Every organization the signed-in user belongs to, with what hangs off each."""
    return await _summaries(principal)


@router.post("", response_model=OrgSummary, status_code=status.HTTP_201_CREATED)
async def create_organization(body: OrgIn, principal: Principal = Depends(get_principal)):
    """Starts another organization; the caller becomes its first ADMIN. Same switch as sign-up."""
    if not get_settings().signup_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "creating organizations is switched off on this deployment")
    slug = slugify(body.name)
    if len(slug) < 2:
        raise HTTPException(422, "organization name needs letters or digits")
    async with system_tx() as conn:
        cur = await conn.execute("SELECT cp.create_organization(%s, %s, %s) AS id", (principal.user_id, body.name, slug))
        org_id = (await cur.fetchone())["id"]
    return next(o for o in await _summaries(principal) if o.id == org_id)


@router.patch("/current", response_model=OrgSummary)
async def rename_organization(body: OrgIn, principal: Principal = Depends(require_role("ADMIN"))):
    """Renames the organization this session is acting in. The identifier (slug) never changes."""
    async with principal.tx() as conn:
        cur = await conn.execute("UPDATE cp.organizations SET name = btrim(%s) WHERE id = cp.current_org() RETURNING id, name",
                                 (body.name,))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "organization not found")
        await conn.execute("SELECT cp.audit('organizations.rename', 'organizations', %s, jsonb_build_object('name', %s::text))",
                           (str(row["id"]), row["name"]))
    return next(o for o in await _summaries(principal) if o.current)


@router.delete("/current", response_model=TokenOut)
async def archive_organization(principal: Principal = Depends(require_role("ADMIN"))):
    """Archives the organization this session is acting in. It is kept, with its audit history, and
    nobody can act in it afterwards. Returns a session in another of the caller's organizations."""
    async with principal.tx() as conn:
        await conn.execute("SELECT cp.archive_organization()")
    async with system_tx() as conn:
        orgs = await _memberships(conn, principal.user_id)
    org = _pick_org(orgs, None)
    return TokenOut(access_token=create_access_token(principal.user_id, org.id), org=org, orgs=orgs)
