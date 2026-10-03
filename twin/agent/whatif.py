"""Tier T1: ask the planner what it would do, without building anything.

HypoPG registers a hypothetical index that exists only in this session's
planner. EXPLAIN then shows whether the planner would use it and what it thinks
the cost becomes. It runs on the twin source (a read-only standby), so
production is not involved. It is cheap and sometimes wrong, which is why the
twin replay (T2) follows it.
"""
import json
import time

import psycopg

from agent import pg
from dbpilot_core import actions


def _cost(conn: psycopg.Connection, query: str) -> float | None:
    """Planner cost of a parameterised statement. GENERIC_PLAN plans `$1`-style
    text without needing parameter values. Statements that cannot be explained
    (utility commands, writes on a standby that fail to plan) return None."""
    try:
        row = conn.execute(f"EXPLAIN (GENERIC_PLAN, FORMAT JSON) {query}").fetchone()
    except psycopg.Error:
        return None
    plan = row[0] if isinstance(row[0], list) else json.loads(row[0])
    return float(plan[0]["Plan"]["Total Cost"])


def explain(query: str) -> dict:
    """The planner's plan for a parameterised statement, as text, from the twin source."""
    with pg.connect(pg.SOURCE_PORT) as conn:
        try:
            rows = conn.execute(f"EXPLAIN (GENERIC_PLAN, VERBOSE off) {query}").fetchall()
        except psycopg.Error as exc:
            return {"explained": False, "error": str(exc).splitlines()[0]}
    return {"explained": True, "plan": "\n".join(r[0] for r in rows)}


def index_whatif(action_data: dict, queries: list[str]) -> dict:
    action = actions.parse_action(action_data)
    if not isinstance(action, actions.CreateIndex):
        return {"supported": False, "reason": f"planner what-if does not apply to {action.type}"}
    # The twin source is a standby: a query on it can be cancelled when replay needs
    # to remove rows the query might still see. That is transient, so try again.
    for attempt in range(4):
        try:
            return _index_whatif(action, queries)
        except psycopg.errors.SerializationFailure:
            if attempt == 3:
                raise
            time.sleep(1 + attempt)


def _index_whatif(action: actions.CreateIndex, queries: list[str]) -> dict:
    with pg.connect(pg.SOURCE_PORT) as conn:
        plan = actions.plan(action, conn)
        before = {q: _cost(conn, q) for q in queries}
        conn.execute("SELECT hypopg_reset()")
        for statement in plan.apply:
            # The executor's own statement, minus the keywords HypoPG does not parse.
            ddl = statement.replace("CONCURRENTLY IF NOT EXISTS ", "").split(" ON ", 1)
            conn.execute("SELECT hypopg_create_index(%s)", (f"CREATE INDEX ON {ddl[1]}",))
        size = conn.execute("SELECT coalesce(sum(hypopg_relation_size(indexrelid)), 0) FROM hypopg_list_indexes").fetchone()[0]
        after = {q: _cost(conn, q) for q in queries}
        conn.execute("SELECT hypopg_reset()")

    results = []
    for q in queries:
        if before[q] is None or after[q] is None:
            continue
        results.append({"query": q, "cost_before": before[q], "cost_after": after[q],
                        "ratio": after[q] / before[q] if before[q] else 1.0})
    improved = [r for r in results if r["ratio"] < 0.9]
    return {"supported": True, "explained": len(results), "improved": len(improved),
            "estimated_index_bytes": int(size), "queries": sorted(results, key=lambda r: r["ratio"])[:20]}
