"""
plan_parser.py — Turns a raw PostgreSQL
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) output into a flat, ML-ready
feature dict.

Design choice: the ENTIRE raw plan JSON is kept in storage alongside
these extracted features (see storage.py). Feature extraction logic
will change as the research direction evolves; re-running EXPLAIN
ANALYZE against a 1.5M-row order_items table is expensive, but
re-parsing stored JSON is free. Never throw the raw plan away.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_SCAN_NODE_TYPES = {
    "Seq Scan", "Index Scan", "Index Only Scan",
    "Bitmap Heap Scan", "Bitmap Index Scan", "Tid Scan",
}
_JOIN_NODE_TYPES = {"Nested Loop", "Hash Join", "Merge Join"}
_SORT_NODE_TYPES = {"Sort", "Incremental Sort"}


@dataclass
class PlanFeatures:
    planner_startup_cost: float
    planner_total_cost: float
    planner_estimated_rows: int
    actual_rows: int
    actual_loops: int
    planning_time_ms: float
    execution_time_ms: float
    shared_blks_hit: int
    shared_blks_read: int
    jit_enabled: bool
    parallel_workers_planned: int
    parallel_workers_launched: int
    num_joins: int
    num_scans: int
    plan_node_count: int
    plan_depth: int
    scan_types: list[str] = field(default_factory=list)
    join_types: list[str] = field(default_factory=list)
    has_seqscan: bool = False
    has_index_scan: bool = False
    has_bitmap_scan: bool = False
    has_sort: bool = False
    has_hash_agg: bool = False
    has_group_agg: bool = False
    has_nested_loop: bool = False
    has_hash_join: bool = False
    has_merge_join: bool = False


def _walk(node: dict, acc: dict, depth: int) -> None:
    node_type = node.get("Node Type", "")
    acc["plan_node_count"] += 1
    acc["plan_depth"] = max(acc["plan_depth"], depth)
    acc["shared_blks_hit"] += node.get("Shared Hit Blocks", 0) or 0
    acc["shared_blks_read"] += node.get("Shared Read Blocks", 0) or 0

    if node_type in _SCAN_NODE_TYPES:
        acc["num_scans"] += 1
        acc["scan_types"].append(node_type)
        if node_type == "Seq Scan":
            acc["has_seqscan"] = True
        elif node_type in ("Index Scan", "Index Only Scan"):
            acc["has_index_scan"] = True
        elif node_type in ("Bitmap Heap Scan", "Bitmap Index Scan"):
            acc["has_bitmap_scan"] = True

    if node_type in _JOIN_NODE_TYPES:
        acc["num_joins"] += 1
        acc["join_types"].append(node_type)
        if node_type == "Nested Loop":
            acc["has_nested_loop"] = True
        elif node_type == "Hash Join":
            acc["has_hash_join"] = True
        elif node_type == "Merge Join":
            acc["has_merge_join"] = True

    if node_type in _SORT_NODE_TYPES:
        acc["has_sort"] = True

    if node_type == "HashAggregate":
        acc["has_hash_agg"] = True
    elif node_type == "GroupAggregate":
        acc["has_group_agg"] = True

    for child in node.get("Plans", []):
        _walk(child, acc, depth + 1)


def parse_explain_json(explain_output: list[dict]) -> PlanFeatures:
    """
    explain_output is the parsed JSON result of
    EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) — psycopg returns this as a
    Python list with one element for a single-statement query.
    """
    top = explain_output[0]
    plan = top["Plan"]

    acc = {
        "plan_node_count": 0,
        "plan_depth": 0,
        "shared_blks_hit": 0,
        "shared_blks_read": 0,
        "num_scans": 0,
        "num_joins": 0,
        "scan_types": [],
        "join_types": [],
        "has_seqscan": False,
        "has_index_scan": False,
        "has_bitmap_scan": False,
        "has_sort": False,
        "has_hash_agg": False,
        "has_group_agg": False,
        "has_nested_loop": False,
        "has_hash_join": False,
        "has_merge_join": False,
    }
    _walk(plan, acc, depth=0)

    jit_info = top.get("JIT", {}) or {}
    jit_enabled = bool(jit_info.get("Functions", 0))

    return PlanFeatures(
        planner_startup_cost=plan.get("Startup Cost", 0.0),
        planner_total_cost=plan.get("Total Cost", 0.0),
        planner_estimated_rows=plan.get("Plan Rows", 0),
        actual_rows=plan.get("Actual Rows", 0),
        actual_loops=plan.get("Actual Loops", 1),
        planning_time_ms=top.get("Planning Time", 0.0),
        execution_time_ms=top.get("Execution Time", 0.0),
        shared_blks_hit=acc["shared_blks_hit"],
        shared_blks_read=acc["shared_blks_read"],
        jit_enabled=jit_enabled,
        parallel_workers_planned=plan.get("Workers Planned", 0),
        parallel_workers_launched=plan.get("Workers Launched", 0),
        num_joins=acc["num_joins"],
        num_scans=acc["num_scans"],
        plan_node_count=acc["plan_node_count"],
        plan_depth=acc["plan_depth"],
        scan_types=acc["scan_types"],
        join_types=acc["join_types"],
        has_seqscan=acc["has_seqscan"],
        has_index_scan=acc["has_index_scan"],
        has_bitmap_scan=acc["has_bitmap_scan"],
        has_sort=acc["has_sort"],
        has_hash_agg=acc["has_hash_agg"],
        has_group_agg=acc["has_group_agg"],
        has_nested_loop=acc["has_nested_loop"],
        has_hash_join=acc["has_hash_join"],
        has_merge_join=acc["has_merge_join"],
    )
