-- 002_audit.sql
-- Append-only audit log, written by triggers so that no code path can change
-- org-owned data without leaving a record, and guarded by a trigger so that
-- nobody (not even the table owner) can rewrite history with UPDATE/DELETE.

-- Request context. The API sets these with set_config(..., true) at the start of
-- every transaction; they vanish at COMMIT/ROLLBACK, so a pooled connection can
-- never carry one request's identity into the next.
CREATE FUNCTION cp.current_org() RETURNS uuid
LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('app.org_id', true), '')::uuid $$;

CREATE FUNCTION cp.current_user_id() RETURNS uuid
LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$;

CREATE TABLE cp.audit_log (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id         uuid NOT NULL REFERENCES cp.organizations (id) ON DELETE RESTRICT,
    actor_user_id  uuid,          -- no FK: the record must outlive the user
    action         text NOT NULL,
    entity_type    text NOT NULL,
    entity_id      text,
    detail         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_org_time_idx ON cp.audit_log (org_id, id DESC);

CREATE FUNCTION cp.audit_log_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only' USING ERRCODE = 'DP002';
END $$;

CREATE TRIGGER audit_log_no_rewrite
    BEFORE UPDATE OR DELETE ON cp.audit_log
    FOR EACH ROW EXECUTE FUNCTION cp.audit_log_immutable();
CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON cp.audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION cp.audit_log_immutable();

-- Generic row-change recorder. SECURITY DEFINER because the API role has no
-- INSERT privilege on audit_log: it can cause audit rows, never forge them.
CREATE FUNCTION cp.audit_row() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cp, public, pg_temp AS $$
DECLARE
    v_old jsonb;
    v_new jsonb;
    v_row jsonb;
BEGIN
    IF TG_OP = 'DELETE' THEN
        v_old := to_jsonb(OLD); v_row := v_old;
    ELSIF TG_OP = 'UPDATE' THEN
        v_old := to_jsonb(OLD); v_new := to_jsonb(NEW); v_row := v_new;
    ELSE
        v_new := to_jsonb(NEW); v_row := v_new;
    END IF;

    INSERT INTO audit_log (org_id, actor_user_id, action, entity_type, entity_id, detail)
    VALUES ((v_row ->> 'org_id')::uuid,
            cp.current_user_id(),
            TG_TABLE_NAME || '.' || lower(TG_OP),
            TG_TABLE_NAME,
            coalesce(v_row ->> 'id', v_row ->> 'user_id'),
            jsonb_strip_nulls(jsonb_build_object('old', v_old, 'new', v_new)));
    RETURN NULL;
END $$;

CREATE TRIGGER clusters_audit    AFTER INSERT OR UPDATE OR DELETE ON cp.clusters
    FOR EACH ROW EXECUTE FUNCTION cp.audit_row();
CREATE TRIGGER tenants_audit     AFTER INSERT OR UPDATE OR DELETE ON cp.tenants
    FOR EACH ROW EXECUTE FUNCTION cp.audit_row();
CREATE TRIGGER slos_audit        AFTER INSERT OR UPDATE OR DELETE ON cp.slos
    FOR EACH ROW EXECUTE FUNCTION cp.audit_row();
CREATE TRIGGER memberships_audit AFTER INSERT OR UPDATE OR DELETE ON cp.memberships
    FOR EACH ROW EXECUTE FUNCTION cp.audit_row();

-- Explicit events that are not row changes (login, approvals, ...).
CREATE FUNCTION cp.audit(p_action text, p_entity_type text, p_entity_id text, p_detail jsonb)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cp, public, pg_temp AS $$
BEGIN
    IF cp.current_org() IS NULL THEN
        RAISE EXCEPTION 'no organization context' USING ERRCODE = '42501';
    END IF;
    INSERT INTO audit_log (org_id, actor_user_id, action, entity_type, entity_id, detail)
    VALUES (cp.current_org(), cp.current_user_id(), p_action, p_entity_type, p_entity_id,
            coalesce(p_detail, '{}'::jsonb));
END $$;

-- An organization must always keep one ADMIN. Two admins demoting each other at
-- the same moment would each see "the other one is still an admin" and both
-- succeed; locking the organization row first makes the two checks run one after
-- the other.
CREATE FUNCTION cp.guard_last_admin() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cp, public, pg_temp AS $$
DECLARE
    v_losing_admin boolean := false;
BEGIN
    IF OLD.role = 'ADMIN' THEN
        IF TG_OP = 'DELETE' THEN
            v_losing_admin := true;
        ELSIF NEW.role <> 'ADMIN' OR NEW.org_id <> OLD.org_id OR NEW.user_id <> OLD.user_id THEN
            v_losing_admin := true;
        END IF;
    END IF;

    IF v_losing_admin THEN
        PERFORM 1 FROM organizations WHERE id = OLD.org_id FOR UPDATE;
        IF FOUND AND NOT EXISTS (
            SELECT 1 FROM memberships
            WHERE org_id = OLD.org_id AND role = 'ADMIN' AND user_id <> OLD.user_id
        ) THEN
            RAISE EXCEPTION 'an organization must keep at least one ADMIN' USING ERRCODE = 'DP001';
        END IF;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER memberships_guard_last_admin
    BEFORE UPDATE OR DELETE ON cp.memberships
    FOR EACH ROW EXECUTE FUNCTION cp.guard_last_admin();
