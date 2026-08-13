-- 08_functions.sql
--
-- PURPOSE: Reusable lookup/computation logic that belongs in the
-- database because multiple things need the SAME answer to the SAME
-- question (the trigger in 10_triggers.sql, the procedures in
-- 09_procedures.sql, and the application backend all need to resolve
-- "what is this company's current quota policy" and "which company
-- does this query belong to" — putting the logic here means there is
-- exactly one place that can get it wrong, instead of three.
--
-- DEPENDENCIES: 00 through 07 (reads only).
--
-- PostgreSQL 18 compatible. No version-specific syntax used.

SET search_path TO dbpilot, public;

-- ─────────────────────────────────────────────────────────────────────
-- fn_get_active_quota_policy(p_company_id)
--
-- PURPOSE: Returns the company's CURRENT QuotaPolicy row (the one with
-- effective_to IS NULL). This is safe as a single-row-returning
-- function specifically because ux_quotapolicy_one_current_per_company
-- (the partial unique index from 03_infra_tables.sql) guarantees at
-- most one such row exists per company — the function is only correct
-- because that index exists; if the index were ever dropped this
-- function would need to change to RETURNS SETOF.
--
-- PARAMETERS: p_company_id INT
-- RETURNS: quotapolicy  (the full row type; all columns NULL if the
--          company has no current policy — callers must check
--          cost_threshold IS NULL rather than assume a row means a
--          policy exists)
-- WHY IN THE DATABASE: this exact join is needed by a trigger (10_) at
-- millisecond insert-time, where there is no application round-trip to
-- do it in — it has to already be in the database.
--
-- EXAMPLE CALL:
--   SELECT * FROM fn_get_active_quota_policy(1);
--   SELECT cost_threshold FROM fn_get_active_quota_policy(1);
-- ─────────────────────────────────────────────────────────────────────
CREATE FUNCTION fn_get_active_quota_policy(p_company_id INT)
RETURNS QuotaPolicy
LANGUAGE sql
STABLE
SET search_path = dbpilot, public
AS $$
    SELECT *
    FROM QuotaPolicy
    WHERE company_id = p_company_id
      AND effective_to IS NULL;
$$;

COMMENT ON FUNCTION fn_get_active_quota_policy(INT) IS
    'Returns the single current (effective_to IS NULL) QuotaPolicy row '
    'for a company, or a row of NULLs if none exists. Relies on '
    'ux_quotapolicy_one_current_per_company for correctness.';

-- ─────────────────────────────────────────────────────────────────────
-- fn_resolve_company_for_query_history(p_query_history_id)
--
-- PURPOSE: Walks QueryHistory -> DatabaseInstance -> Company to answer
-- "which company owns this query?" QueryHistory has no direct
-- company_id column (deliberately — it already has database_instance_id,
-- and duplicating company_id onto it would be a transitive dependency,
-- i.e. exactly the kind of redundancy the 3NF discussion in the design
-- doc argues against). This function is the single place that walks
-- the chain instead of every caller re-deriving it.
--
-- PARAMETERS: p_query_history_id BIGINT
-- RETURNS: INT  (company_id), or NULL if the query_history_id does not exist
-- WHY IN THE DATABASE: used by the Prediction-cost trigger in 10_, which
-- has only NEW.query_history_id available and needs company_id to look
-- up the quota policy above.
--
-- EXAMPLE CALL:
--   SELECT fn_resolve_company_for_query_history(1);
-- ─────────────────────────────────────────────────────────────────────
CREATE FUNCTION fn_resolve_company_for_query_history(p_query_history_id BIGINT)
RETURNS INT
LANGUAGE sql
STABLE
SET search_path = dbpilot, public
AS $$
    SELECT di.company_id
    FROM QueryHistory qh
    JOIN DatabaseInstance di ON di.database_instance_id = qh.database_instance_id
    WHERE qh.query_history_id = p_query_history_id;
$$;

COMMENT ON FUNCTION fn_resolve_company_for_query_history(BIGINT) IS
    'Resolves the owning company_id for a QueryHistory row by walking '
    'the FK chain through DatabaseInstance. Kept as a function instead '
    'of a stored/duplicated column on QueryHistory to avoid a '
    'transitive dependency (company_id already follows from '
    'database_instance_id).';
