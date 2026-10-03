"""Rule-based proposer: fixed diagnostic rules of the kind a DBA runbook contains.

It is the non-LLM baseline. It sees exactly what the agent sees (the Observer)
and produces exactly what the agent produces (typed actions), so any difference
between the two in an experiment is down to the reasoning, not the plumbing.
"""
import re
from dataclasses import dataclass, field

from app.observe import Observer
from dbpilot_core import actions

_EQUALITY = re.compile(r"\b([a-z_]+_[a-z_0-9]+)\s*=\s*\$\d+")
_TABLE = re.compile(r"\bch\.([a-z_]+)")

SPILL_BLOCKS = 2000          # temp blocks written in the window that count as "spilling"
STALE_FRACTION = 0.2         # rows changed since ANALYZE, as a fraction of live rows
INDEX_MIN_SHARE = 0.05       # a query must be at least this share of a tenant's time to earn an index
INDEX_MIN_GAIN = 0.5         # planner cost must at least halve
BURST_FACTOR = 3.0


@dataclass
class Proposal:
    action: dict
    rationale: str
    evidence: dict = field(default_factory=dict)


def _spills(obs: Observer, queries: list[dict]) -> list[Proposal]:
    """A tenant whose queries write temporary files is sorting or hashing on disk: give that tenant more memory."""
    by_tenant: dict[str, int] = {}
    for q in queries:
        by_tenant[q["tenant_role"]] = by_tenant.get(q["tenant_role"], 0) + q["temp_blocks_written"]
    overrides = obs.settings()["tenants"]
    out = []
    for role, blocks in by_tenant.items():
        if blocks < SPILL_BLOCKS or any(o.startswith("work_mem=") for o in overrides.get(role, {}).get("overrides", [])):
            continue
        out.append(Proposal(
            {"type": "role_setting", "tenant_role": role, "name": "work_mem", "value": "65536"},
            f"{role} wrote {blocks} temporary blocks in the window: its sorts and hashes spill to disk. "
            "Raising work_mem for this tenant only keeps them in memory without changing other tenants' limits.",
            {"rule": "spills", "temp_blocks_written": blocks}))
    return out


def _missing_indexes(obs: Observer, queries: list[dict]) -> list[Proposal]:
    """For each expensive read with an equality predicate on an unindexed column, ask the planner
    whether an index on that column would at least halve its cost."""
    totals: dict[str, float] = {}
    for q in queries:
        totals[q["tenant_role"]] = totals.get(q["tenant_role"], 0.0) + q["total_exec_ms"]
    out, tried = [], set()
    for q in queries:
        text = q["query"]
        if not text.lstrip().upper().startswith("SELECT") or q["total_exec_ms"] < INDEX_MIN_SHARE * totals[q["tenant_role"]]:
            continue
        for table in set(_TABLE.findall(text)) & set(actions.TABLES):
            w_col, columns = actions.TABLES[table]
            for column in _EQUALITY.findall(text):
                # Columns that lead the primary key are already served by it.
                if column not in columns or column in columns[:2] or (table, column, q["tenant_role"]) in tried:
                    continue
                tried.add((table, column, q["tenant_role"]))
                role = q["tenant_role"] if table in actions.PARTITIONED else None
                result = obs.whatif_index(table, [column], role)
                best = min((r["ratio"] for r in result.get("queries", [])), default=1.0)
                if best <= INDEX_MIN_GAIN:
                    out.append(Proposal(
                        {"type": "create_index", "table": table, "columns": [column], "tenant_role": role},
                        f"{q['tenant_role']} spends {q['total_exec_ms']:.0f} ms on a query filtering {table}.{column} "
                        f"with no index on it; the planner estimates an index cuts its cost to {best:.0%}. "
                        "Built on this tenant's partition only, so other tenants' writes are not taxed.",
                        {"rule": "missing_index", "queryid": q["queryid"], "planner_ratio": best,
                         "estimated_index_bytes": result.get("estimated_index_bytes")}))
    return out


def _stale_statistics(obs: Observer) -> list[Proposal]:
    out = []
    for table in actions.PARTITIONED:
        for rel in obs.table_profile(table)["relations"]:
            changed, live = rel["rows_changed_since_analyze"], max(rel["live_rows"], 1)
            if changed > 10000 and changed / live > STALE_FRACTION and "_t_" in rel["relation"]:
                role = rel["relation"][len(table) + 1:]
                out.append(Proposal(
                    {"type": "analyze", "table": table, "tenant_role": role},
                    f"{changed} rows of {rel['relation']} changed since statistics were last gathered "
                    f"({changed / live:.0%} of the table); the planner is estimating from an outdated picture.",
                    {"rule": "stale_statistics", "rows_changed": changed, "live_rows": live}))
    return out


def _bursts(obs: Observer) -> list[Proposal]:
    """A tenant running at several times its usual rate while another tenant misses its SLO."""
    violated = [s for s in obs.slo_status(5) if s["windows"] and s["windows_violating"] * 2 >= s["windows"]]
    if not violated:
        return []
    series: dict[str, list[int]] = {}
    for point in obs.tenant_load(30):
        series.setdefault(point["tenant_role"], []).append(point["calls"])
    limits = obs.settings()["tenants"]
    out = []
    for role, calls in series.items():
        if len(calls) < 6 or limits.get(role, {}).get("connection_limit", -1) != -1:
            continue
        usual = sorted(calls)[len(calls) // 2]
        victims = [s["tenant_role"] for s in violated if s["tenant_role"] != role]
        if usual > 0 and calls[-1] > BURST_FACTOR * usual and victims:
            out.append(Proposal(
                {"type": "concurrency_cap", "tenant_role": role, "max_connections": 4},
                f"{role} is running at {calls[-1] / usual:.1f}x its usual rate while {', '.join(sorted(set(victims)))} "
                "miss their SLO. Capping its concurrent connections makes its burst queue instead of crowding the others.",
                {"rule": "burst", "calls_last_window": calls[-1], "usual_calls": usual, "victims": victims}))
    return out


def propose(obs: Observer, minutes: int = 15) -> list[Proposal]:
    queries = obs.top_queries(minutes, None, 60)
    return [*_spills(obs, queries), *_missing_indexes(obs, queries), *_stale_statistics(obs), *_bursts(obs)]
