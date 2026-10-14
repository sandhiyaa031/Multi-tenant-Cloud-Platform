-- 008_organizations.sql
-- Organizations as a managed resource: create more than one per user, rename, archive.
--
-- Two rules shape this file.
--   * The audit log is append-only and references organizations (ON DELETE RESTRICT), so an
--     organization can never be deleted without destroying history. "Removing" one is therefore
--     an ARCHIVE: the row and everything about it stay, and nobody can act in it any more.
--   * Creating an organization touches rows the API role may not insert (organizations and the
--     first membership), so it goes through a SECURITY DEFINER function, as signup does.

ALTER TABLE cp.organizations ADD COLUMN archived_at timestamptz;

-- One change makes an archived organization unreachable everywhere: every RLS policy asks
-- has_role(), and has_role() is false for an archived organization.
CREATE OR REPLACE FUNCTION cp.has_role(p_min cp.org_role) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = cp, public, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1
        FROM memberships m
        JOIN users u ON u.id = m.user_id
        JOIN organizations o ON o.id = m.org_id
        WHERE m.org_id = cp.current_org()
          AND m.user_id = cp.current_user_id()
          AND u.is_active
          AND o.archived_at IS NULL
          AND m.role >= p_min
    )
$$;

-- Sign-in and the organization switcher list only organizations that are not archived.
CREATE OR REPLACE FUNCTION cp.auth_memberships(p_user_id uuid)
RETURNS TABLE (o_org_id uuid, o_slug citext, o_name text, o_role cp.org_role)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = cp, public, pg_temp AS $$
    SELECT o.id, o.slug, o.name, m.role
    FROM memberships m
    JOIN organizations o ON o.id = m.org_id
    WHERE m.user_id = p_user_id AND o.archived_at IS NULL
    ORDER BY m.created_at, o.slug
$$;

-- A signed-in user starts another organization and becomes its first ADMIN, atomically.
-- The user id comes from the verified token, never from the request body.
CREATE FUNCTION cp.create_organization(p_user_id uuid, p_name text, p_slug citext)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cp, public, pg_temp AS $$
DECLARE
    v_org uuid;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM users WHERE id = p_user_id AND is_active) THEN
        RAISE EXCEPTION 'unknown or inactive user' USING ERRCODE = '42501';
    END IF;
    INSERT INTO organizations (name, slug) VALUES (btrim(p_name), p_slug) RETURNING id INTO v_org;
    PERFORM set_config('app.user_id', p_user_id::text, true);   -- so the audit trigger names the actor
    INSERT INTO memberships (org_id, user_id, role) VALUES (v_org, p_user_id, 'ADMIN');
    INSERT INTO audit_log (org_id, actor_user_id, action, entity_type, entity_id, detail)
    VALUES (v_org, p_user_id, 'organizations.create', 'organizations', v_org::text,
            jsonb_build_object('slug', p_slug, 'name', btrim(p_name)));
    RETURN v_org;
END $$;

-- Archive the CURRENT organization (the one the request is bound to).
--   DP004  it still has a live cluster: retire the clusters first, so no data plane is left
--          being observed on behalf of an organization nobody can reach
--   DP005  it is the caller's only organization: archiving it would lock the caller out
CREATE FUNCTION cp.archive_organization() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cp, public, pg_temp AS $$
DECLARE
    v_org  uuid := cp.current_org();
    v_user uuid := cp.current_user_id();
BEGIN
    IF v_org IS NULL OR NOT cp.has_role('ADMIN') THEN
        RAISE EXCEPTION 'not permitted' USING ERRCODE = '42501';
    END IF;
    PERFORM 1 FROM organizations WHERE id = v_org FOR UPDATE;
    IF EXISTS (SELECT 1 FROM clusters WHERE org_id = v_org AND status <> 'RETIRED') THEN
        RAISE EXCEPTION 'organization still has clusters' USING ERRCODE = 'DP004';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM memberships m JOIN organizations o ON o.id = m.org_id
        WHERE m.user_id = v_user AND m.org_id <> v_org AND o.archived_at IS NULL
    ) THEN
        RAISE EXCEPTION 'last organization of the caller' USING ERRCODE = 'DP005';
    END IF;
    INSERT INTO audit_log (org_id, actor_user_id, action, entity_type, entity_id, detail)
    VALUES (v_org, v_user, 'organizations.archive', 'organizations', v_org::text, '{}'::jsonb);
    UPDATE organizations SET archived_at = now() WHERE id = v_org;
END $$;

-- The organizations a user belongs to, each with how much hangs off it. The counts are what
-- makes the relation to data-plane tenants visible: an organization owns clusters, and a
-- cluster owns tenants. Counting across several organizations needs to see past RLS, which is
-- scoped to one; membership in each is what entitles the caller to its counts.
CREATE FUNCTION cp.my_organizations(p_user_id uuid)
RETURNS TABLE (o_org_id uuid, o_slug citext, o_name text, o_role cp.org_role, o_created_at timestamptz,
               o_members integer, o_clusters integer, o_tenants integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = cp, public, pg_temp AS $$
    SELECT o.id, o.slug, o.name, m.role, o.created_at,
           (SELECT count(*)::int FROM memberships x WHERE x.org_id = o.id),
           (SELECT count(*)::int FROM clusters c WHERE c.org_id = o.id AND c.status <> 'RETIRED'),
           (SELECT count(*)::int FROM tenants t JOIN clusters c ON c.id = t.cluster_id
             WHERE t.org_id = o.id AND c.status <> 'RETIRED')
    FROM memberships m
    JOIN organizations o ON o.id = m.org_id
    WHERE m.user_id = p_user_id AND o.archived_at IS NULL
    ORDER BY m.created_at, o.slug
$$;

REVOKE ALL ON FUNCTION cp.create_organization(uuid, text, citext), cp.archive_organization(),
                       cp.my_organizations(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cp.create_organization(uuid, text, citext), cp.archive_organization(),
                          cp.my_organizations(uuid) TO dbpilot_api;
