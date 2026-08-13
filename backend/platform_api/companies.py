"""
platform_api/companies.py — Company CRUD.

Company is the root tenant entity: every other table ultimately traces
back to it. This is the one resource the assignment asked for full CRUD
on, so it gets its own file; every other resource in reads.py is
read-only for this stage.

SOFT DELETE: Company.is_active is a soft-delete flag by design (see
02_tenant_core.sql's comment on that column). DELETE /companies/<id>
therefore does NOT run SQL DELETE — it sets is_active = false. This
isn't just a style choice: nearly every other table RESTRICTs on
company_id, so a real DELETE would raise a foreign-key violation the
instant any Team/app_user/DatabaseInstance/etc. exists for that
company anyway. Soft-delete is the only behavior that's actually
correct given the schema as designed.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from .db import get_db

companies_bp = Blueprint("companies", __name__)


@companies_bp.get("/companies")
def list_companies():
    """
    GET /companies
    GET /companies?include_inactive=true

    Frontend request  -> GET /companies
    Flask route       -> list_companies()
    Validation        -> include_inactive parsed as a boolean flag, no
                          other input to validate on a plain list
    SQL               -> SELECT ... FROM Company [WHERE is_active] ORDER BY name
    Returned data     -> list of company rows (as dicts, via dict_row)
    JSON response      -> {"companies": [...]}
    """
    include_inactive = request.args.get("include_inactive", "false").lower() == "true"

    conn = get_db()
    with conn.cursor() as cur:
        if include_inactive:
            cur.execute(
                "SELECT company_id, name, industry, created_at, is_active "
                "FROM Company ORDER BY name"
            )
        else:
            cur.execute(
                "SELECT company_id, name, industry, created_at, is_active "
                "FROM Company WHERE is_active ORDER BY name"
            )
        companies = cur.fetchall()

    return jsonify(companies=companies)


@companies_bp.get("/companies/<int:company_id>")
def get_company(company_id: int):
    """
    GET /companies/<id>

    Returns 404 (not a bare empty body) when the id doesn't exist —
    the frontend should be able to tell "no such company" apart from
    "company with zero of something".
    """
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT company_id, name, industry, created_at, is_active "
            "FROM Company WHERE company_id = %s",
            (company_id,),
        )
        company = cur.fetchone()

    if company is None:
        return jsonify(error="not_found", message=f"No company with id {company_id}"), 404

    return jsonify(company=company)


@companies_bp.post("/companies")
def create_company():
    """
    POST /companies
    Body: {"name": "...", "industry": "..."}   (industry optional)

    Validation      -> name is required and must be non-blank; industry
                        is optional and passed through as-is (NULL if
                        omitted)
    SQL             -> parameterized INSERT ... RETURNING, so the
                        newly-created row (including its generated
                        company_id and defaulted created_at/is_active)
                        comes back in one round trip
    Returned data   -> the new row
    JSON response    -> 201 Created + {"company": {...}}
    """
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    industry = body.get("industry")

    if not name:
        return jsonify(error="validation_error", message="'name' is required."), 400

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO Company (name, industry) VALUES (%s, %s) "
            "RETURNING company_id, name, industry, created_at, is_active",
            (name, industry),
        )
        company = cur.fetchone()
    conn.commit()

    return jsonify(company=company), 201


@companies_bp.put("/companies/<int:company_id>")
def update_company(company_id: int):
    """
    PUT /companies/<id>
    Body: any subset of {"name": "...", "industry": "..."}

    This is a PARTIAL update — only fields actually present in the JSON
    body are changed, via COALESCE(new_value, existing_value). This
    matters because a naive "UPDATE Company SET name=%s, industry=%s"
    would silently NULL out industry if the frontend only sent {"name":
    "..."} to rename a company.

    is_active is deliberately NOT settable here — reactivating or
    deactivating a company goes through DELETE (deactivate) below;
    there is currently no reactivate endpoint because nothing in the
    spec asked for one, and adding one un-asked-for would be exactly
    the kind of scope creep the project rules warn against. Flag this
    if you want a reactivate endpoint added in a later stage.
    """
    body = request.get_json(silent=True) or {}
    if not body:
        return jsonify(error="validation_error", message="Request body must include at least one field."), 400

    name = body.get("name")
    industry = body.get("industry")

    if "name" in body and not (name or "").strip():
        return jsonify(error="validation_error", message="'name' cannot be blank."), 400

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE Company
            SET name = COALESCE(%s, name),
                industry = COALESCE(%s, industry)
            WHERE company_id = %s
            RETURNING company_id, name, industry, created_at, is_active
            """,
            (name, industry, company_id),
        )
        company = cur.fetchone()
    conn.commit()

    if company is None:
        return jsonify(error="not_found", message=f"No company with id {company_id}"), 404

    return jsonify(company=company)


@companies_bp.delete("/companies/<int:company_id>")
def delete_company(company_id: int):
    """
    DELETE /companies/<id>

    Soft delete: sets is_active = false. See module docstring for why
    this is correct given the schema (RESTRICT everywhere) rather than
    a shortcut.
    """
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE Company SET is_active = false WHERE company_id = %s "
            "RETURNING company_id, name, industry, created_at, is_active",
            (company_id,),
        )
        company = cur.fetchone()
    conn.commit()

    if company is None:
        return jsonify(error="not_found", message=f"No company with id {company_id}"), 404

    return jsonify(company=company)
