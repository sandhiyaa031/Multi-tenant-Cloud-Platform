-- 004_auth_functions.sql
-- The narrow, audited doorways through which the API touches credentials.
--
-- Authentication happens before any organization context exists, so it cannot
-- go through row-level security. Instead of giving the API role broad access to
-- users and tokens, each operation is one SECURITY DEFINER function that does
-- exactly one thing atomically.

-- New organization + its first user + ADMIN membership, all or nothing.
CREATE FUNCTION cp.signup(p_org_name text, p_slug citext, p_email citext,
                          p_password_hash text, p_full_name text)
RETURNS TABLE (o_org_id uuid, o_user_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cp, public, pg_temp AS $$
DECLARE
    v_org  uuid;
    v_user uuid;
BEGIN
    INSERT INTO organizations (name, slug) VALUES (btrim(p_org_name), p_slug) RETURNING id INTO v_org;
    INSERT INTO users (email, password_hash, full_name)
    VALUES (p_email, p_password_hash, btrim(p_full_name)) RETURNING id INTO v_user;

    PERFORM set_config('app.user_id', v_user::text, true);   -- so the audit trigger names the actor
    INSERT INTO memberships (org_id, user_id, role) VALUES (v_org, v_user, 'ADMIN');
    INSERT INTO audit_log (org_id, actor_user_id, action, entity_type, entity_id, detail)
    VALUES (v_org, v_user, 'organizations.signup', 'organizations', v_org::text,
            jsonb_build_object('slug', p_slug));

    RETURN QUERY SELECT v_org, v_user;
END $$;

-- The only way the API can read a password hash.
CREATE FUNCTION cp.auth_get_user(p_email citext)
RETURNS TABLE (o_user_id uuid, o_email citext, o_password_hash text, o_full_name text, o_is_active boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = cp, public, pg_temp AS $$
    SELECT id, email, password_hash, full_name, is_active FROM users WHERE email = p_email
$$;

CREATE FUNCTION cp.auth_memberships(p_user_id uuid)
RETURNS TABLE (o_org_id uuid, o_slug citext, o_name text, o_role cp.org_role)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = cp, public, pg_temp AS $$
    SELECT o.id, o.slug, o.name, m.role
    FROM memberships m
    JOIN organizations o ON o.id = m.org_id
    WHERE m.user_id = p_user_id
    ORDER BY m.created_at, o.slug
$$;

-- Returns false when the email is unknown; the API answers identically either way.
CREATE FUNCTION cp.create_reset_token(p_email citext, p_token_hash bytea, p_ttl interval)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cp, public, pg_temp AS $$
DECLARE
    v_user uuid;
BEGIN
    SELECT id INTO v_user FROM users WHERE email = p_email AND is_active;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    INSERT INTO password_reset_tokens (user_id, token_hash, purpose, expires_at)
    VALUES (v_user, p_token_hash, 'reset', now() + p_ttl);
    RETURN true;
END $$;

-- Single use. FOR UPDATE makes two concurrent requests with the same token
-- queue on the row; the second one then sees used_at set and gets NULL.
CREATE FUNCTION cp.consume_token(p_token_hash bytea, p_password_hash text)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cp, public, pg_temp AS $$
DECLARE
    v_user uuid;
BEGIN
    SELECT user_id INTO v_user
    FROM password_reset_tokens
    WHERE token_hash = p_token_hash AND used_at IS NULL AND expires_at > now()
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    UPDATE users SET password_hash = p_password_hash WHERE id = v_user;
    -- Setting a new password invalidates every outstanding token for that user.
    UPDATE password_reset_tokens SET used_at = now() WHERE user_id = v_user AND used_at IS NULL;
    RETURN v_user;
END $$;

-- ADMIN adds a member. Creates the user if the email is new (no password yet)
-- and issues an invite token with which they set one. Someone who already has an
-- account is simply added; no token is created for them.
CREATE FUNCTION cp.invite_member(p_email citext, p_full_name text, p_role cp.org_role,
                                 p_token_hash bytea, p_ttl interval)
RETURNS TABLE (o_user_id uuid, o_needs_password boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = cp, public, pg_temp AS $$
DECLARE
    v_user uuid;
    v_needs_password boolean;
BEGIN
    IF NOT cp.has_role('ADMIN') THEN
        RAISE EXCEPTION 'ADMIN role required' USING ERRCODE = '42501';
    END IF;

    INSERT INTO users (email, full_name) VALUES (p_email, btrim(p_full_name))
    ON CONFLICT (email) DO NOTHING;
    SELECT id, password_hash IS NULL INTO v_user, v_needs_password FROM users WHERE email = p_email;

    INSERT INTO memberships (org_id, user_id, role) VALUES (cp.current_org(), v_user, p_role);
    IF v_needs_password THEN
        INSERT INTO password_reset_tokens (user_id, token_hash, purpose, expires_at)
        VALUES (v_user, p_token_hash, 'invite', now() + p_ttl);
    END IF;
    RETURN QUERY SELECT v_user, v_needs_password;
END $$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA cp FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    cp.current_org(), cp.current_user_id(), cp.has_role(cp.org_role), cp.audit(text, text, text, jsonb),
    cp.signup(text, citext, citext, text, text),
    cp.auth_get_user(citext),
    cp.auth_memberships(uuid),
    cp.create_reset_token(citext, bytea, interval),
    cp.consume_token(bytea, text),
    cp.invite_member(citext, text, cp.org_role, bytea, interval)
TO dbpilot_api;
