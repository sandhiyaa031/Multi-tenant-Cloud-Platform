"""Proposals: creating them, following them through verification, deciding on them."""
import os
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ValidationError

from app.config import get_settings
from app.deps import ROLE_RANK, Principal, require_role
from app.observe import open_observer
from dbpilot_core import actions

router = APIRouter(tags=["proposals"])


class ProposalIn(BaseModel):
    action: dict[str, Any]
    rationale: str = Field(default="", max_length=4000)
    gate_mode: Literal["per_tenant", "aggregate"] = "per_tenant"
    verification: Literal["full", "canary_only", "none"] = "full"
    auto_approve: bool = False


class ProposalOut(BaseModel):
    id: UUID
    cluster_id: UUID
    target_tenant_id: UUID | None
    source: str
    action: dict[str, Any]
    rationale: str
    evidence: dict[str, Any]
    gate_mode: str
    verification: str
    auto_approve: bool
    state: str
    state_reason: str
    created_at: datetime
    updated_at: datetime


class StepOut(BaseModel):
    tier: str
    decision: str
    summary: str
    detail: dict[str, Any]
    seconds: float
    created_at: datetime


class ProposalDetail(ProposalOut):
    steps: list[StepOut]
    twin_runs: list[dict[str, Any]]
    canary: dict[str, Any] | None


class DecisionIn(BaseModel):
    reason: str = Field(default="", max_length=2000)


async def insert_proposal(conn, principal: Principal, cluster_id: UUID, body: ProposalIn, source: str,
                          evidence: dict | None = None) -> dict:
    """Validates the action against the typed action space and stores the proposal."""
    try:
        action = actions.parse_action(body.action)
    except ValidationError as exc:
        raise HTTPException(422, f"not a valid action: {exc.errors()[0]['msg']}")
    cur = await conn.execute("SELECT 1 FROM cp.clusters WHERE id = %s", (cluster_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cluster not found")

    target_id = None
    role = actions.target_tenant(action)
    if role is not None:
        cur = await conn.execute("SELECT id FROM cp.tenants WHERE cluster_id = %s AND db_role = %s", (cluster_id, role))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(422, f"{role} is not a tenant of this cluster")
        target_id = row["id"]

    cur = await conn.execute(
        """
        INSERT INTO cp.proposals (org_id, cluster_id, target_tenant_id, source, action, rationale, evidence,
                                  gate_mode, verification, auto_approve, created_by)
        VALUES (cp.current_org(), %s, %s, %s, %s, %s, %s, %s, %s, %s, cp.current_user_id()) RETURNING *
        """,
        # Stored in normalised form (defaults filled in), exactly as the executor will read it.
        (cluster_id, target_id, source, Jsonb(action.model_dump()), body.rationale, Jsonb(evidence or {}),
         body.gate_mode, body.verification, body.auto_approve),
    )
    return await cur.fetchone()


@router.get("/actions/schema")
async def action_schema(principal: Principal = Depends(require_role("VIEWER"))):
    """The complete action space: what a proposer may express, and nothing else."""
    return {"schema": actions.action_json_schema(),
            "role_settings": {k: vars(v) for k, v in actions.ROLE_SETTINGS.items()},
            "instance_settings": {k: vars(v) for k, v in actions.INSTANCE_SETTINGS.items()},
            "tables": {t: [w, *cols] for t, (w, cols) in actions.TABLES.items()}}


@router.post("/clusters/{cluster_id}/proposals", response_model=ProposalOut, status_code=status.HTTP_201_CREATED)
async def create_proposal(cluster_id: UUID, body: ProposalIn, principal: Principal = Depends(require_role("OPERATOR"))):
    async with principal.tx() as conn:
        return await insert_proposal(conn, principal, cluster_id, body, "manual")


class DiagnoseIn(BaseModel):
    source: Literal["rule", "agent"] = "rule"
    hint: str = Field(default="", max_length=2000)
    gate_mode: Literal["per_tenant", "aggregate"] = "per_tenant"
    verification: Literal["full", "canary_only", "none"] = "full"
    auto_approve: bool = False


def _diagnose(user_id: UUID, org_id: UUID, cluster_id: UUID, body: DiagnoseIn) -> list:
    """Runs a proposer over the cluster's observations. Synchronous: called off the event loop."""
    try:
        conn, observer = open_observer(get_settings().database_url, user_id, org_id, cluster_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cluster not found")
    try:
        if observer.cluster["primary_host"] is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "this cluster has no primary address registered; it is not observed")
        if body.source == "rule":
            from app.proposers import rules
            return rules.propose(observer)
        import anthropic

        from app.proposers import agent
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "the LLM agent is not configured (ANTHROPIC_API_KEY is not set); the rule-based proposer is available")
        try:
            return [agent.propose(observer, hint=body.hint)]
        except anthropic.APIError as exc:
            # The model could not be reached or refused the request at the transport level.
            # Nothing was proposed, so nothing is stored.
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"the model API call failed: {type(exc).__name__}: {str(exc)[:300]}")
    finally:
        conn.close()


@router.post("/clusters/{cluster_id}/diagnose", response_model=list[ProposalOut], status_code=status.HTTP_201_CREATED)
async def diagnose(cluster_id: UUID, body: DiagnoseIn, principal: Principal = Depends(require_role("OPERATOR"))):
    """OBSERVE -> DIAGNOSE -> PLAN: asks the rule-based proposer or the LLM agent for proposals
    and queues whatever it returns for verification."""
    found = await run_in_threadpool(_diagnose, principal.user_id, principal.org_id, cluster_id, body)
    created = []
    async with principal.tx() as conn:
        for p in found:
            created.append(await insert_proposal(
                conn, principal, cluster_id,
                ProposalIn(action=p.action, rationale=p.rationale, gate_mode=body.gate_mode,
                           verification=body.verification, auto_approve=body.auto_approve),
                body.source, p.evidence))
    return created


@router.get("/proposals", response_model=list[ProposalOut])
async def list_proposals(
    cluster_id: UUID | None = Query(default=None),
    state: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_role("VIEWER")),
):
    async with principal.tx() as conn:
        cur = await conn.execute(
            "SELECT * FROM cp.proposals WHERE (%s::uuid IS NULL OR cluster_id = %s) AND (%s::text IS NULL OR state = %s)"
            " ORDER BY created_at DESC LIMIT %s",
            (cluster_id, cluster_id, state, state, limit),
        )
        return await cur.fetchall()


@router.get("/proposals/{proposal_id}", response_model=ProposalDetail)
async def get_proposal(proposal_id: UUID, principal: Principal = Depends(require_role("VIEWER"))):
    async with principal.tx() as conn:
        cur = await conn.execute("SELECT * FROM cp.proposals WHERE id = %s", (proposal_id,))
        proposal = await cur.fetchone()
        if proposal is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "proposal not found")
        cur = await conn.execute("SELECT * FROM cp.verification_steps WHERE proposal_id = %s ORDER BY id", (proposal_id,))
        steps = await cur.fetchall()
        cur = await conn.execute(
            "SELECT id, window_s, transactions, repetitions, replay_errors, wal_ratio, storage_delta_bytes,"
            " apply_seconds, verdict, seconds, created_at FROM cp.twin_runs WHERE proposal_id = %s ORDER BY created_at",
            (proposal_id,))
        twin_runs = await cur.fetchall()
        cur = await conn.execute(
            "SELECT applied, inverse, contract, baseline, observations, result, outcome, outcome_reason,"
            " started_at, finished_at FROM cp.canaries WHERE proposal_id = %s", (proposal_id,))
        canary = await cur.fetchone()
    return {**proposal, "steps": steps, "twin_runs": twin_runs, "canary": canary}


async def _decide(proposal_id: UUID, principal: Principal, to_state: str, allowed_from: dict[str, str], reason: str):
    async with principal.tx() as conn:
        cur = await conn.execute("SELECT state FROM cp.proposals WHERE id = %s FOR UPDATE", (proposal_id,))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "proposal not found")
        needed = allowed_from.get(row["state"])
        if needed is None:
            raise HTTPException(status.HTTP_409_CONFLICT, f"a proposal in state {row['state']} cannot be moved to {to_state}")
        if ROLE_RANK[principal.role] < ROLE_RANK[needed]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"{needed} role required for a proposal in state {row['state']}")
        cur = await conn.execute(
            "UPDATE cp.proposals SET state = %s, state_reason = %s, decided_by = cp.current_user_id() WHERE id = %s RETURNING *",
            (to_state, reason or f"{to_state.lower().replace('_', ' ')} by a member", proposal_id))
        return await cur.fetchone()


@router.post("/proposals/{proposal_id}/approve", response_model=ProposalOut)
async def approve(proposal_id: UUID, body: DecisionIn, principal: Principal = Depends(require_role("OPERATOR"))):
    # Overriding an inconclusive verification is a bigger decision than confirming a passed one.
    return await _decide(proposal_id, principal, "APPROVED", {"AWAITING_APPROVAL": "OPERATOR", "INCONCLUSIVE": "ADMIN"},
                         body.reason)


@router.post("/proposals/{proposal_id}/reject", response_model=ProposalOut)
async def reject(proposal_id: UUID, body: DecisionIn, principal: Principal = Depends(require_role("OPERATOR"))):
    return await _decide(proposal_id, principal, "REJECTED", {"AWAITING_APPROVAL": "OPERATOR", "INCONCLUSIVE": "OPERATOR"},
                         body.reason)


@router.post("/proposals/{proposal_id}/rollback", response_model=ProposalOut)
async def request_rollback(proposal_id: UUID, body: DecisionIn, principal: Principal = Depends(require_role("OPERATOR"))):
    """Asks the engine to undo an applied change using the inverse recorded when it was applied."""
    return await _decide(proposal_id, principal, "ROLLBACK_REQUESTED", {"APPLIED": "OPERATOR"}, body.reason)


@router.get("/clusters/{cluster_id}/ledger")
async def ledger(cluster_id: UUID, limit: int = Query(default=200, ge=1, le=1000),
                 principal: Principal = Depends(require_role("VIEWER"))):
    """Prediction against outcome for every proposal: what the twin said, what production did."""
    async with principal.tx() as conn:
        cur = await conn.execute(
            "SELECT * FROM cp.outcome_ledger WHERE cluster_id = %s ORDER BY created_at DESC LIMIT %s", (cluster_id, limit))
        return await cur.fetchall()
