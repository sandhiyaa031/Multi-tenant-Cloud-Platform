"""LLM agent proposer.

The model is given read-only tools over the Observer and one tool with an
effect: `propose_action`, whose input must validate against the typed action
space. It cannot run SQL, cannot change anything, and cannot approve what it
proposes; its output enters the same verification pipeline as any other
proposal. Every tool call is recorded, so a reviewer can see what the agent
looked at before it decided.
"""
import json
import os
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import anthropic
from pydantic import ValidationError

from app.observe import Observer
from app.proposers.rules import Proposal
from dbpilot_core import actions

MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5-5")
MAX_TURNS = 14
MAX_RESULT_CHARS = 12000

SYSTEM = """You are the diagnosis agent of DBPilot, a control plane for one PostgreSQL instance shared by several tenants.

How the instance is laid out:
- Tenants share the same tables. Each tenant connects as its own database role and owns a range of warehouse ids.
- The large tables (customer, history, orders, new_order, order_line, stock) are partitioned per tenant, so an index \
can be created on one tenant's partition only.
- Tenants label their connections OLTP or OLAP. Each tenant has latency objectives (SLOs) per class.

Your job: look at what the instance is doing and propose at most one change that would help a tenant, or decide that \
no change is warranted. Because everything is shared, a change that helps one tenant can hurt another: an index taxes \
every write to that table, memory and parallel workers given to one workload are taken from the rest. Prefer the \
narrowest scope that solves the problem (one tenant's partition or role over the whole instance), and say in your \
rationale which other tenants could be affected and why you expect them not to be.

What happens after you propose: your proposal is not applied. It is checked against static limits, then measured on a \
clone of the database under a replay of the real workload, for every tenant, and applied only if the target tenant is \
shown to benefit and no other tenant is shown or suspected to be harmed. A wrong proposal costs verification time, so \
investigate before proposing: read the SLO status and the expensive queries, look at plans and table profiles, and use \
the what-if tool before proposing an index.

Rules:
- You can only act through the propose_action tool. You cannot run SQL.
- propose_action takes one action object from the action space below. Anything else is rejected.
- If nothing is wrong, or the evidence does not support a specific change, propose {"type": "no_action", ...} and \
explain. Doing nothing is a correct answer when the problem is transient.
- Check the history tool first: do not re-propose something that was already rejected or rolled back unless the \
situation has changed, and say so if you do.
- Call propose_action exactly once, then stop.

Action space (JSON Schema):
"""

TOOLS = [
    {"name": "get_tenants", "description": "List the tenants on this cluster: role name, workload profile, warehouse range.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_slo_status",
     "description": "Each tenant's latency objective and the latency observed against it. Start here to see who is suffering.",
     "input_schema": {"type": "object", "properties": {"minutes": {"type": "integer", "minimum": 1, "maximum": 240}},
                      "additionalProperties": False}},
    {"name": "get_latency",
     "description": "Observed transaction latency (p50, p95, worst p99) per tenant and class over the last minutes.",
     "input_schema": {"type": "object", "properties": {"minutes": {"type": "integer", "minimum": 1, "maximum": 240}},
                      "additionalProperties": False}},
    {"name": "get_top_queries",
     "description": "The most expensive query fingerprints by total execution time, with calls, mean time, rows, "
                    "blocks read from disk, temporary blocks written (sorts/hashes spilling to disk) and WAL bytes. "
                    "Optionally for one tenant. Query text has literals replaced by $n.",
     "input_schema": {"type": "object", "properties": {
         "minutes": {"type": "integer", "minimum": 1, "maximum": 240},
         "tenant_role": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 40}},
         "additionalProperties": False}},
    {"name": "get_tenant_load",
     "description": "Calls and execution time per tenant per collector window. Use it to see bursts and workload shifts.",
     "input_schema": {"type": "object", "properties": {"minutes": {"type": "integer", "minimum": 1, "maximum": 240}},
                      "additionalProperties": False}},
    {"name": "explain_query",
     "description": "The planner's execution plan for one fingerprint (by queryid from get_top_queries). "
                    "Shows scan types, join methods and estimated costs. Runs on a standby, not on production.",
     "input_schema": {"type": "object", "properties": {"queryid": {"type": "string"}}, "required": ["queryid"],
                      "additionalProperties": False}},
    {"name": "get_table_profile",
     "description": "For one table: per-tenant partition sizes, sequential vs index scans, inserts/updates/deletes "
                    "(how write-heavy it is), rows changed since statistics were gathered, and existing indexes with "
                    "their usage. Use it to judge what an index would cost the writers.",
     "input_schema": {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"],
                      "additionalProperties": False}},
    {"name": "get_settings",
     "description": "Current values of every setting an action may change, instance-wide and per-tenant overrides.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "whatif_index",
     "description": "Ask the planner what a hypothetical index would do to the cost of the observed queries on that "
                    "table, without building it. Returns per-query cost before and after and the estimated index size. "
                    "An estimate only; the real effect is measured later on the twin.",
     "input_schema": {"type": "object", "properties": {
         "table": {"type": "string"}, "columns": {"type": "array", "items": {"type": "string"}, "minItems": 1},
         "tenant_role": {"type": "string", "description": "Limit the index to this tenant's partition."}},
         "required": ["table", "columns"], "additionalProperties": False}},
    {"name": "get_history",
     "description": "Earlier proposals on this cluster and their outcomes: what the twin predicted, whether the "
                    "canary held or rolled back, and the latency ratios observed in production.",
     "input_schema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
                      "additionalProperties": False}},
    {"name": "propose_action",
     "description": "Submit your decision: one action from the action space, with the reasoning behind it. "
                    "Call this exactly once, when you have enough evidence.",
     "input_schema": {"type": "object", "properties": {
         "action": {"type": "object", "description": "One action object matching the action-space schema."},
         "rationale": {"type": "string", "description": "What you observed, why this action, which tenants could be "
                                                         "affected and why you expect them not to be harmed."}},
         "required": ["action", "rationale"]}},
]


def _json(value) -> str:
    def default(o):
        if isinstance(o, (datetime, UUID, Decimal)):
            return str(o)
        raise TypeError(type(o).__name__)

    text = json.dumps(value, default=default)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + '... [truncated: ask for fewer rows]'


def _call(obs: Observer, name: str, args: dict):
    if name == "get_tenants":
        return obs.tenants()
    if name == "get_slo_status":
        return obs.slo_status(args.get("minutes", 15))
    if name == "get_latency":
        return obs.latency(args.get("minutes", 15))
    if name == "get_top_queries":
        return obs.top_queries(args.get("minutes", 15), args.get("tenant_role"), args.get("limit", 15))
    if name == "get_tenant_load":
        return obs.tenant_load(args.get("minutes", 30))
    if name == "explain_query":
        return obs.explain(args["queryid"])
    if name == "get_table_profile":
        return obs.table_profile(args["table"])
    if name == "get_settings":
        return obs.settings()
    if name == "whatif_index":
        return obs.whatif_index(args["table"], args["columns"], args.get("tenant_role"))
    if name == "get_history":
        return obs.history(args.get("limit", 20))
    raise ValueError(f"unknown tool {name}")


def propose(obs: Observer, client: anthropic.Anthropic | None = None, hint: str = "") -> Proposal:
    """Runs the agent loop and returns its single proposal, with the full trace as evidence."""
    client = client or anthropic.Anthropic()
    system = [{"type": "text", "text": SYSTEM + json.dumps(actions.action_json_schema()),
               "cache_control": {"type": "ephemeral"}}]
    task = "Diagnose this cluster and propose at most one action."
    messages = [{"role": "user", "content": f"{task}\n\nOperator note: {hint}" if hint else task}]
    trace: list[dict] = []
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "requests": 0}

    def evidence(**extra) -> dict:
        return {"proposer": "agent", "model": MODEL, "trace": trace, "usage": usage, **extra}

    for _ in range(MAX_TURNS):
        # Refusal fallback is opt-in on this model; "default" lets the API pick the fallback by refusal category.
        response = client.beta.messages.create(
            model=MODEL, max_tokens=16000, system=system, tools=TOOLS, messages=messages,
            output_config={"effort": "high"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        usage["requests"] += 1
        usage["input_tokens"] += response.usage.input_tokens
        usage["output_tokens"] += response.usage.output_tokens
        usage["cache_read_input_tokens"] += getattr(response.usage, "cache_read_input_tokens", 0) or 0

        if response.stop_reason == "refusal":
            return Proposal({"type": "no_action", "reason": "the model declined to diagnose this cluster", "escalate": True},
                            "The model declined the request.", evidence(stop_reason="refusal"))
        if response.stop_reason == "max_tokens":
            return Proposal({"type": "no_action", "reason": "the agent ran out of output budget", "escalate": True},
                            "The agent's response was cut off before it proposed anything.", evidence(stop_reason="max_tokens"))

        for block in response.content:
            if block.type == "text" and block.text.strip():
                trace.append({"kind": "note", "text": block.text.strip()[:4000]})
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            break
        messages.append({"role": "assistant", "content": response.content})

        results = []
        for use in tool_uses:
            if use.name == "propose_action":
                try:
                    action = actions.parse_action(use.input.get("action") or {})
                except ValidationError as exc:
                    # Outside the action space: tell the model exactly why, and let it correct itself.
                    message = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
                    trace.append({"kind": "tool", "name": use.name, "input": use.input, "error": message})
                    results.append({"type": "tool_result", "tool_use_id": use.id, "is_error": True,
                                    "content": f"Rejected, not a valid action: {message}"})
                    continue
                trace.append({"kind": "tool", "name": use.name, "input": use.input, "result": "accepted"})
                return Proposal(action.model_dump(), str(use.input.get("rationale", ""))[:4000], evidence())
            try:
                output = _json(_call(obs, use.name, use.input or {}))
                trace.append({"kind": "tool", "name": use.name, "input": use.input, "result": output[:3000]})
                results.append({"type": "tool_result", "tool_use_id": use.id, "content": output})
            except Exception as exc:
                trace.append({"kind": "tool", "name": use.name, "input": use.input, "error": str(exc)[:500]})
                results.append({"type": "tool_result", "tool_use_id": use.id, "is_error": True, "content": str(exc)[:500]})
        # All results of one assistant turn go back in a single user message.
        messages.append({"role": "user", "content": results})

    return Proposal({"type": "no_action", "reason": "the agent finished without proposing an action", "escalate": True},
                    "The agent did not call propose_action.", evidence(stop_reason="no_proposal"))
