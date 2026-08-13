"""
platform_api/reads.py — Read-only endpoints beyond Company.

Every one of these is a straightforward parameterized SELECT. Where a
view from 07_views.sql already does the join, we SELECT from the view
instead of re-deriving the same join in Python — that's the whole
reason those views exist. Password hashes are never selected or
returned, anywhere, on purpose.

Common pattern across all of these:
    Frontend request (with optional ?company_id=, ?limit=, etc.)
      -> Flask route below
      -> optional query params parsed, no required body
      -> parameterized SELECT (against a base table or a view)
      -> rows returned as a JSON list
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from .db import get_db

reads_bp = Blueprint("reads", __name__)


def _optional_int(name: str) -> int | None:
    """Parses an optional integer query param; returns None if absent or invalid."""
    raw = request.args.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────
# Teams
# ─────────────────────────────────────────────────────────────────────
@reads_bp.get("/teams")
def list_teams():
    """GET /teams?company_id=<id>  (company_id optional; omit for all teams)"""
    company_id = _optional_int("company_id")
    conn = get_db()
    with conn.cursor() as cur:
        if company_id is not None:
            cur.execute(
                "SELECT team_id, company_id, team_name FROM Team "
                "WHERE company_id = %s ORDER BY team_name",
                (company_id,),
            )
        else:
            cur.execute("SELECT team_id, company_id, team_name FROM Team ORDER BY team_name")
        teams = cur.fetchall()
    return jsonify(teams=teams)


# ─────────────────────────────────────────────────────────────────────
# Users
# ─────────────────────────────────────────────────────────────────────
@reads_bp.get("/users")
def list_users():
    """
    GET /users?company_id=<id>

    password_hash is deliberately excluded from the SELECT list, not
    just filtered out after fetching — it should never travel over the
    wire to the frontend at all, even hashed.
    """
    company_id = _optional_int("company_id")
    conn = get_db()
    with conn.cursor() as cur:
        if company_id is not None:
            cur.execute(
                "SELECT user_id, company_id, email, full_name FROM app_user "
                "WHERE company_id = %s ORDER BY full_name",
                (company_id,),
            )
        else:
            cur.execute(
                "SELECT user_id, company_id, email, full_name FROM app_user ORDER BY full_name"
            )
        users = cur.fetchall()
    return jsonify(users=users)


# ─────────────────────────────────────────────────────────────────────
# Subscriptions
# ─────────────────────────────────────────────────────────────────────
@reads_bp.get("/subscriptions")
def list_subscriptions():
    """
    GET /subscriptions                    -> active subscriptions only,
                                              backed by vw_active_subscriptions
    GET /subscriptions?company_id=<id>     -> that company's active subscription
    GET /subscriptions?include_history=true -> full Subscription history
                                                instead (all statuses),
                                                base table, not the view
    """
    company_id = _optional_int("company_id")
    include_history = request.args.get("include_history", "false").lower() == "true"

    conn = get_db()
    with conn.cursor() as cur:
        if include_history:
            if company_id is not None:
                cur.execute(
                    "SELECT subscription_id, company_id, plan_id, start_date, "
                    "end_date, status FROM Subscription WHERE company_id = %s "
                    "ORDER BY start_date DESC",
                    (company_id,),
                )
            else:
                cur.execute(
                    "SELECT subscription_id, company_id, plan_id, start_date, "
                    "end_date, status FROM Subscription ORDER BY start_date DESC"
                )
        else:
            if company_id is not None:
                cur.execute(
                    "SELECT * FROM vw_active_subscriptions WHERE company_id = %s",
                    (company_id,),
                )
            else:
                cur.execute("SELECT * FROM vw_active_subscriptions ORDER BY company_name")
        subscriptions = cur.fetchall()
    return jsonify(subscriptions=subscriptions)


# ─────────────────────────────────────────────────────────────────────
# Database instances
# ─────────────────────────────────────────────────────────────────────
@reads_bp.get("/database-instances")
def list_database_instances():
    """GET /database-instances?company_id=<id>"""
    company_id = _optional_int("company_id")
    conn = get_db()
    with conn.cursor() as cur:
        if company_id is not None:
            cur.execute(
                "SELECT database_instance_id, company_id, instance_name, host, "
                "port, db_name, status FROM DatabaseInstance "
                "WHERE company_id = %s ORDER BY instance_name",
                (company_id,),
            )
        else:
            cur.execute(
                "SELECT database_instance_id, company_id, instance_name, host, "
                "port, db_name, status FROM DatabaseInstance ORDER BY instance_name"
            )
        instances = cur.fetchall()
    return jsonify(database_instances=instances)


# ─────────────────────────────────────────────────────────────────────
# Alerts
# ─────────────────────────────────────────────────────────────────────
@reads_bp.get("/alerts")
def list_alerts():
    """
    GET /alerts?company_id=<id>&unacknowledged=true

    unacknowledged=true reuses idx_alert_unacknowledged (the partial
    index from 05_governance_tables.sql) automatically — this is
    exactly the query shape that index was built for.
    """
    company_id = _optional_int("company_id")
    unacknowledged_only = request.args.get("unacknowledged", "false").lower() == "true"

    conditions = []
    params: list = []
    if company_id is not None:
        conditions.append("company_id = %s")
        params.append(company_id)
    if unacknowledged_only:
        conditions.append("acknowledged_at IS NULL")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT alert_id, company_id, source_query_history_id, severity, "
            f"message, acknowledged_by, acknowledged_at FROM Alert "
            f"{where_clause} ORDER BY alert_id DESC",
            params,
        )
        alerts = cur.fetchall()
    return jsonify(alerts=alerts)


# ─────────────────────────────────────────────────────────────────────
# Query performance (view-backed)
# ─────────────────────────────────────────────────────────────────────
@reads_bp.get("/query-performance")
def query_performance():
    """
    GET /query-performance?company_id=<id>&limit=50

    Backed entirely by vw_query_performance — no joins written here.
    limit defaults to 100 and is capped at 500 so a frontend bug can't
    accidentally request the entire QueryHistory table in one response.
    """
    company_id = _optional_int("company_id")
    limit = _optional_int("limit") or 100
    limit = min(max(limit, 1), 500)

    conn = get_db()
    with conn.cursor() as cur:
        if company_id is not None:
            cur.execute(
                "SELECT * FROM vw_query_performance WHERE company_id = %s "
                "ORDER BY executed_at DESC LIMIT %s",
                (company_id, limit),
            )
        else:
            cur.execute(
                "SELECT * FROM vw_query_performance ORDER BY executed_at DESC LIMIT %s",
                (limit,),
            )
        rows = cur.fetchall()
    return jsonify(query_performance=rows)


# ─────────────────────────────────────────────────────────────────────
# Company health (view-backed)
# ─────────────────────────────────────────────────────────────────────
@reads_bp.get("/company-health")
def company_health_all():
    """GET /company-health — every active company's dashboard summary row."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM vw_company_health ORDER BY company_name")
        rows = cur.fetchall()
    return jsonify(company_health=rows)


@reads_bp.get("/company-health/<int:company_id>")
def company_health_one(company_id: int):
    """GET /company-health/<id> — one company's dashboard summary row."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM vw_company_health WHERE company_id = %s", (company_id,))
        row = cur.fetchone()
    if row is None:
        return jsonify(error="not_found", message=f"No company_health row for id {company_id}"), 404
    return jsonify(company_health=row)
