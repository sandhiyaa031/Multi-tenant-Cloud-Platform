-- 10_triggers.sql
--
-- PURPOSE: Three triggers, each enforcing something that genuinely
-- belongs at the database level — either because the alternative is
-- "hope every application code path remembers to do this" (the two
-- alert triggers) or because the whole point is that even a buggy or
-- malicious application layer cannot bypass it (the audit-log trigger).
--
-- DEPENDENCIES: 08_functions.sql (trg_prediction_cost_alert calls
-- fn_get_active_quota_policy and fn_resolve_company_for_query_history).
--
-- PostgreSQL 18 compatible.

SET search_path TO dbpilot, public;

-- ─────────────────────────────────────────────────────────────────────
-- trg_prediction_cost_alert
--
-- EVENT: AFTER INSERT ON Prediction, FOR EACH ROW (row-level — the
-- decision depends on NEW's own corrected_cost, a statement-level
-- trigger would need to re-scan all rows from the statement for no
-- benefit here).
--
-- AFTER, not BEFORE: this trigger only needs to react to a Prediction
-- that already exists (it creates a separate Alert row referencing it);
-- it does not need to, and should not, modify the Prediction row being
-- inserted.
--
-- INSERT only, not UPDATE: deliberate design choice. Prediction rows
-- can in principle be revised, but firing a fresh Alert every time a
-- Prediction is updated (even if corrected_cost didn't change) would
-- spam Alert with duplicates. If Predictions start being revised
-- routinely in a later stage, this should become AFTER INSERT OR UPDATE
-- OF corrected_cost with a WHEN clause comparing OLD vs NEW — that is a
-- genuine future extension, not something to silently add now.
--
-- WHAT IT DOES: resolves the owning company via
-- fn_resolve_company_for_query_history, looks up that company's current
-- QuotaPolicy via fn_get_active_quota_policy, and if corrected_cost
-- exceeds cost_threshold, inserts an Alert. Severity is 'critical' if
-- the cost is more than double the threshold, otherwise 'warning'. If
-- the company has no current QuotaPolicy at all, the trigger does
-- nothing — there is no threshold to compare against, and silently
-- treating "no policy" as "infinite threshold" or "zero threshold"
-- would both be wrong guesses.
--
-- WHAT BREAKS IF REMOVED: nothing stops working, but governance
-- threshold breaches stop generating Alerts automatically — they would
-- only surface if/when someone manually queries vw_query_performance
-- and does the threshold comparison by hand. This is exactly the "an
-- application code path might forget" gap a trigger is for.
-- ─────────────────────────────────────────────────────────────────────
CREATE FUNCTION fn_trg_prediction_cost_alert() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = dbpilot, public
AS $$
DECLARE
    v_company_id  INT;
    v_policy      QuotaPolicy;
    v_severity    TEXT;
BEGIN
    v_company_id := fn_resolve_company_for_query_history(NEW.query_history_id);

    IF v_company_id IS NULL THEN
        RETURN NEW;  -- should not happen given FK constraints, defensive only
    END IF;

    v_policy := fn_get_active_quota_policy(v_company_id);

    IF v_policy.cost_threshold IS NULL THEN
        RETURN NEW;  -- company has no current QuotaPolicy; nothing to compare against
    END IF;

    IF NEW.corrected_cost > v_policy.cost_threshold THEN
        v_severity := CASE
            WHEN NEW.corrected_cost > 2 * v_policy.cost_threshold THEN 'critical'
            ELSE 'warning'
        END;

        INSERT INTO Alert (company_id, source_query_history_id, severity, message)
        VALUES (
            v_company_id,
            NEW.query_history_id,
            v_severity,
            format(
                'Corrected query cost %s exceeds company quota threshold %s',
                ROUND(NEW.corrected_cost::numeric, 2),
                ROUND(v_policy.cost_threshold::numeric, 2)
            )
        );
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_prediction_cost_alert
    AFTER INSERT ON Prediction
    FOR EACH ROW
    EXECUTE FUNCTION fn_trg_prediction_cost_alert();

COMMENT ON TRIGGER trg_prediction_cost_alert ON Prediction IS
    'Auto-creates an Alert when a Prediction''s corrected_cost exceeds '
    'the owning company''s current QuotaPolicy.cost_threshold. Fires on '
    'INSERT only (see file header for why).';

-- ─────────────────────────────────────────────────────────────────────
-- trg_replica_health_alert
--
-- EVENT: AFTER UPDATE OF health_status ON ReplicaDatabase, FOR EACH ROW,
-- with a WHEN clause restricting it to actual transitions into a bad
-- state (OLD.health_status IS DISTINCT FROM NEW.health_status AND
-- NEW.health_status IN ('degraded','unreachable')). Without the WHEN
-- clause, a health-check process that re-writes 'unreachable' every
-- heartbeat would create a new Alert every heartbeat; the WHEN clause
-- makes this fire only on the actual state transition.
--
-- AFTER, row-level, UPDATE only: a replica cannot be unhealthy at
-- INSERT time in this design (health_status is set by whatever process
-- is polling it after registration), so INSERT is intentionally not
-- covered.
--
-- WHAT BREAKS IF REMOVED: a replica silently going unreachable produces
-- no Alert; PolicyDecision rows might keep routing traffic to it
-- (ROUTE_TO_REPLICA) with nothing surfacing the problem on the
-- dashboard until a human happens to check ReplicaDatabase directly.
-- ─────────────────────────────────────────────────────────────────────
CREATE FUNCTION fn_trg_replica_health_alert() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = dbpilot, public
AS $$
DECLARE
    v_company_id INT;
    v_severity   TEXT;
BEGIN
    SELECT di.company_id INTO v_company_id
    FROM DatabaseInstance di
    WHERE di.database_instance_id = NEW.primary_instance_id;

    v_severity := CASE NEW.health_status
        WHEN 'unreachable' THEN 'critical'
        WHEN 'degraded' THEN 'warning'
        ELSE NULL
    END;

    IF v_severity IS NOT NULL AND v_company_id IS NOT NULL THEN
        INSERT INTO Alert (company_id, source_query_history_id, severity, message)
        VALUES (
            v_company_id,
            NULL,
            v_severity,
            format('Replica %s (host %s) health changed to %s',
                   NEW.replica_id, NEW.host, NEW.health_status)
        );
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_replica_health_alert
    AFTER UPDATE OF health_status ON ReplicaDatabase
    FOR EACH ROW
    WHEN (OLD.health_status IS DISTINCT FROM NEW.health_status
          AND NEW.health_status IN ('degraded', 'unreachable'))
    EXECUTE FUNCTION fn_trg_replica_health_alert();

COMMENT ON TRIGGER trg_replica_health_alert ON ReplicaDatabase IS
    'Auto-creates an Alert when a replica transitions INTO degraded or '
    'unreachable. The WHEN clause prevents duplicate alerts from '
    'repeated heartbeat writes of the same status.';

-- ─────────────────────────────────────────────────────────────────────
-- trg_auditlog_immutable
--
-- EVENT: BEFORE UPDATE OR DELETE ON AuditLog, FOR EACH ROW.
--
-- BEFORE, not AFTER: it must run before the operation is applied, so it
-- can RAISE EXCEPTION and prevent the change from ever happening at
-- all, rather than reacting after the fact.
--
-- WHY THIS BELONGS IN POSTGRES AND NOT JUST APPLICATION CODE: AuditLog
-- is described in the project spec as "append-only security/compliance
-- history." If that rule only lives in application code, a bug (or a
-- direct psql session, or a future engineer who doesn't know the rule)
-- can silently rewrite history. Enforcing it as a trigger means it is
-- true regardless of what wrote the query — that is the actual value
-- proposition of a compliance audit log.
--
-- WHAT BREAKS IF REMOVED: AuditLog rows become editable/deletable like
-- any other table, which defeats the entire purpose of having an audit
-- log — it stops being trustworthy evidence.
-- ─────────────────────────────────────────────────────────────────────
CREATE FUNCTION fn_trg_auditlog_immutable() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = dbpilot, public
AS $$
BEGIN
    RAISE EXCEPTION
        'AuditLog is append-only: % on audit_log_id % is not permitted',
        TG_OP, OLD.audit_log_id;
END;
$$;

CREATE TRIGGER trg_auditlog_immutable
    BEFORE UPDATE OR DELETE ON AuditLog
    FOR EACH ROW
    EXECUTE FUNCTION fn_trg_auditlog_immutable();

COMMENT ON TRIGGER trg_auditlog_immutable ON AuditLog IS
    'Makes AuditLog append-only at the database level: any UPDATE or '
    'DELETE is rejected regardless of which role or code path attempts '
    'it.';
