-- 003_security.sql
-- Authorization inside the database.
--
-- The API checks roles before it runs a query. This file makes PostgreSQL check
-- again, independently: the API's database role (dbpilot_api) is not the table
-- owner, cannot bypass row-level security, and every policy below asks two
-- questions of the current transaction's context:
--   1. is the row in the caller's organization?
--   2. does the caller hold at least the required role in that organization?
-- A bug in an endpoint therefore cannot leak or change another organization's
-- data, and cannot let a VIEWER write.

-- SECURITY DEFINER so that the policy on memberships can call it without the
-- function's own read of memberships recursing into that same policy.
CREATE FUNCTION cp.has_role(p_min cp.org_role) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = cp, public, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1
        FROM memberships m
        JOIN users u ON u.id = m.user_id
        WHERE m.org_id = cp.current_org()
          AND m.user_id = cp.current_user_id()
          AND u.is_active
          AND m.role >= p_min
    )
$$;

ALTER TABLE cp.organizations         ENABLE ROW LEVEL SECURITY;
ALTER TABLE cp.users                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE cp.memberships           ENABLE ROW LEVEL SECURITY;
ALTER TABLE cp.password_reset_tokens ENABLE ROW LEVEL SECURITY;  -- no policies: invisible to the API role
ALTER TABLE cp.clusters              ENABLE ROW LEVEL SECURITY;
ALTER TABLE cp.tenants               ENABLE ROW LEVEL SECURITY;
ALTER TABLE cp.slos                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE cp.audit_log             ENABLE ROW LEVEL SECURITY;

CREATE POLICY organizations_read ON cp.organizations FOR SELECT
    USING (id = cp.current_org() AND cp.has_role('VIEWER'));
CREATE POLICY organizations_update ON cp.organizations FOR UPDATE
    USING (id = cp.current_org() AND cp.has_role('ADMIN'))
    WITH CHECK (id = cp.current_org() AND cp.has_role('ADMIN'));

-- A user can always see their own memberships (to pick an organization at login);
-- members of an organization can see that organization's member list.
CREATE POLICY memberships_read ON cp.memberships FOR SELECT
    USING (user_id = cp.current_user_id()
           OR (org_id = cp.current_org() AND cp.has_role('VIEWER')));
CREATE POLICY memberships_update ON cp.memberships FOR UPDATE
    USING (org_id = cp.current_org() AND cp.has_role('ADMIN'))
    WITH CHECK (org_id = cp.current_org() AND cp.has_role('ADMIN'));
CREATE POLICY memberships_delete ON cp.memberships FOR DELETE
    USING (org_id = cp.current_org() AND cp.has_role('ADMIN'));

CREATE POLICY users_read ON cp.users FOR SELECT
    USING (id = cp.current_user_id()
           OR (cp.has_role('VIEWER') AND EXISTS (
                 SELECT 1 FROM cp.memberships m
                 WHERE m.user_id = users.id AND m.org_id = cp.current_org())));
CREATE POLICY users_update_self ON cp.users FOR UPDATE
    USING (id = cp.current_user_id())
    WITH CHECK (id = cp.current_user_id());

CREATE POLICY audit_log_read ON cp.audit_log FOR SELECT
    USING (org_id = cp.current_org() AND cp.has_role('VIEWER'));

-- Org-owned resource tables share one shape: any member reads, a minimum role writes.
DO $$
DECLARE
    t record;
BEGIN
    FOR t IN SELECT * FROM (VALUES ('clusters', 'ADMIN'), ('tenants', 'OPERATOR'), ('slos', 'OPERATOR'))
                           AS v (tbl, write_role)
    LOOP
        EXECUTE format(
            'CREATE POLICY %1$s_read ON cp.%1$I FOR SELECT
                 USING (org_id = cp.current_org() AND cp.has_role(''VIEWER''))', t.tbl);
        EXECUTE format(
            'CREATE POLICY %1$s_insert ON cp.%1$I FOR INSERT
                 WITH CHECK (org_id = cp.current_org() AND cp.has_role(%2$L))', t.tbl, t.write_role);
        EXECUTE format(
            'CREATE POLICY %1$s_update ON cp.%1$I FOR UPDATE
                 USING (org_id = cp.current_org() AND cp.has_role(%2$L))
                 WITH CHECK (org_id = cp.current_org() AND cp.has_role(%2$L))', t.tbl, t.write_role);
        EXECUTE format(
            'CREATE POLICY %1$s_delete ON cp.%1$I FOR DELETE
                 USING (org_id = cp.current_org() AND cp.has_role(%2$L))', t.tbl, t.write_role);
    END LOOP;
END $$;

-- Least privilege for the API role. Note what is absent: no access to
-- password_reset_tokens, no INSERT on users/organizations/audit_log, and no
-- SELECT on users.password_hash. Those go through the functions in 004.
GRANT USAGE ON SCHEMA cp TO dbpilot_api;

GRANT SELECT ON cp.organizations TO dbpilot_api;
GRANT UPDATE (name) ON cp.organizations TO dbpilot_api;

GRANT SELECT (id, email, full_name, is_active, has_password, created_at) ON cp.users TO dbpilot_api;
GRANT UPDATE (full_name) ON cp.users TO dbpilot_api;

GRANT SELECT, DELETE ON cp.memberships TO dbpilot_api;
GRANT UPDATE (role) ON cp.memberships TO dbpilot_api;

GRANT SELECT, INSERT, UPDATE, DELETE ON cp.clusters, cp.tenants, cp.slos TO dbpilot_api;
GRANT SELECT ON cp.audit_log TO dbpilot_api;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA cp FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cp.current_org(), cp.current_user_id(), cp.has_role(cp.org_role),
                          cp.audit(text, text, text, jsonb) TO dbpilot_api;
