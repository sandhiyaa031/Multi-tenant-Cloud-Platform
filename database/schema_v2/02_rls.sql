-- 02_rls.sql
-- Implements Phase 1 RLS strictly bound to app.tenant_id via SET LOCAL.

-- Enable Row-Level Security on all application tables
ALTER TABLE app.organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.security_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.investigations ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.investigation_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.tags ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.investigation_tags ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.audit_log ENABLE ROW LEVEL SECURITY;

-- Note: We assume the application backend logs in as a singular pooled role (e.g., dbpilot_app).
-- A separate superuser or admin role manages migrations. Admin roles inherently Bypass RLS.

-- To securely map the tenant without relying on arbitrary client inputs, the backend
-- strictly controls the execution of `SET LOCAL app.tenant_id = 'UUID';`

CREATE POLICY tenant_isolation_organizations ON app.organizations
    FOR ALL USING (org_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    -- WITH CHECK defaults to USING automatically when FOR ALL is unseparated.

CREATE POLICY tenant_isolation_users ON app.users
    FOR ALL USING (org_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY tenant_isolation_security_events ON app.security_events
    FOR ALL USING (org_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY tenant_isolation_investigations ON app.investigations
    FOR ALL USING (org_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY tenant_isolation_evidence ON app.investigation_evidence
    FOR ALL USING (org_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY tenant_isolation_notes ON app.notes
    FOR ALL USING (org_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY tenant_isolation_tags ON app.tags
    FOR ALL USING (org_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY tenant_isolation_audit ON app.audit_log
    FOR ALL USING (org_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- Research tables intentionally bypass tenant RLS globally.
