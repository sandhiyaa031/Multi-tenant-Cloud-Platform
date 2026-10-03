"""Views the control-plane UI needs that combine several sources: SLO status,
query plans, the state of the twin, and the experiment summary."""
import os
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool

from app.config import get_settings
from app.deps import Principal, require_role
from app.observe import open_observer

router = APIRouter(prefix="/clusters/{cluster_id}", tags=["insight"])

# A tenant's p95 this much above its pre-change baseline counts as harmed in production.
HARM_RATIO = 1.10
# Changes smaller than this are treated as "no change" when comparing twin and production direction.
DEADBAND = 0.03


def _with_observer(principal: Principal, cluster_id: UUID, fn):
    try:
        conn, observer = open_observer(get_settings().database_url, principal.user_id, principal.org_id, cluster_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cluster not found")
    try:
        return fn(observer)
    finally:
        conn.close()


@router.get("/slo-status")
async def slo_status(cluster_id: UUID, minutes: int = Query(default=15, ge=1, le=1440),
                     principal: Principal = Depends(require_role("VIEWER"))):
    return await run_in_threadpool(_with_observer, principal, cluster_id, lambda o: o.slo_status(minutes))


@router.get("/queries/{queryid}/explain")
async def explain(cluster_id: UUID, queryid: str, principal: Principal = Depends(require_role("VIEWER"))):
    """The planner's plan for a fingerprint, produced on the twin source, not on production."""
    def run(o):
        try:
            return o.explain(queryid)
        except ValueError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
        except httpx.HTTPError:
            return {"explained": False, "error": "the twin node is not reachable"}
    return await run_in_threadpool(_with_observer, principal, cluster_id, run)


@router.get("/tables/{table}")
async def table_profile(cluster_id: UUID, table: str, principal: Principal = Depends(require_role("VIEWER"))):
    def run(o):
        try:
            return o.table_profile(table)
        except ValueError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return await run_in_threadpool(_with_observer, principal, cluster_id, run)


@router.get("/settings")
async def settings(cluster_id: UUID, principal: Principal = Depends(require_role("VIEWER"))):
    return await run_in_threadpool(_with_observer, principal, cluster_id, lambda o: o.settings())


@router.get("/twin")
async def twin_status(cluster_id: UUID, principal: Principal = Depends(require_role("VIEWER"))):
    """Live state of the experimentation plane: how far the twin source trails production, and whether a run is in progress."""
    async with principal.tx() as conn:
        cur = await conn.execute("SELECT 1 FROM cp.clusters WHERE id = %s", (cluster_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "cluster not found")
    try:
        async with httpx.AsyncClient(base_url=os.environ.get("TWIN_URL", "http://twin:8080"), timeout=5,
                                     headers={"Authorization": f"Bearer {os.environ.get('TWIN_TOKEN', '')}"}) as twin:
            return {"available": True, **(await twin.get("/status")).raise_for_status().json()}
    except httpx.HTTPError as exc:
        return {"available": False, "error": type(exc).__name__}


def summarise(rows: list[dict[str, Any]], roles: dict[str, str]) -> list[dict]:
    """Groups the outcome ledger by how proposals were produced and verified.

    `roles` maps tenant id -> database role, to tell the target tenant's keys from the others'.
    """
    groups: dict[tuple, dict] = {}
    for r in rows:
        key = (r["source"], r["verification"], r["gate_mode"])
        g = groups.setdefault(key, {
            "source": key[0], "verification": key[1], "gate_mode": key[2], "proposals": 0, "states": {},
            "reached_production": 0, "harmful_in_production": 0, "rolled_back": 0,
            "direction_checks": 0, "direction_agreements": 0})
        g["proposals"] += 1
        g["states"][r["state"]] = g["states"].get(r["state"], 0) + 1
        g["rolled_back"] += r["state"] == "ROLLED_BACK"

        production = r["production_ratios"] or {}
        if production:
            g["reached_production"] += 1
            target = roles.get(str(r["target_tenant_id"])) if r["target_tenant_id"] else None
            others = {k: v for k, v in production.items() if target is None or not k.startswith(target + "/")}
            g["harmful_in_production"] += any(v > HARM_RATIO for v in others.values())
        for k, effect in (r["twin_effects"] or {}).items():
            if effect.get("ratio") is None or k not in production:
                continue

            def direction(x: float) -> int:
                return 0 if abs(x - 1) <= DEADBAND else (1 if x > 1 else -1)

            g["direction_checks"] += 1
            g["direction_agreements"] += direction(effect["ratio"]) == direction(production[k])
    return sorted(groups.values(), key=lambda g: (g["source"], g["verification"], g["gate_mode"]))


@router.get("/experiments")
async def experiments(cluster_id: UUID, principal: Principal = Depends(require_role("VIEWER"))):
    """Computed from the outcome ledger: for each way of producing and verifying proposals,
    how many reached production, how many harmed another tenant there, and how often the
    twin predicted the direction production then showed."""
    async with principal.tx() as conn:
        cur = await conn.execute(
            "SELECT source, verification, gate_mode, state, target_tenant_id, twin_effects, production_ratios"
            " FROM cp.outcome_ledger WHERE cluster_id = %s", (cluster_id,))
        rows = await cur.fetchall()
        cur = await conn.execute("SELECT id::text, db_role FROM cp.tenants WHERE cluster_id = %s", (cluster_id,))
        roles = {r["id"]: r["db_role"] for r in await cur.fetchall()}
    return {"harm_ratio": HARM_RATIO, "deadband": DEADBAND, "groups": summarise(rows, roles)}
