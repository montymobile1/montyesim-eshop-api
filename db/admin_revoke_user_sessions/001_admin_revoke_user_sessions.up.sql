-- 001_admin_revoke_user_sessions (up)
-- Signs a user out of every session for the admin ban (TEOS-149, TEOS-111 cases 8 and 9).
-- Supabase Auth (GoTrue v2.197) has no admin "log out user X" route, and the previous approach
-- (minting an HS256 user JWT for POST /logout) fails on projects using asymmetric signing keys.
-- This does what GoTrue's own global logout does: delete the user's refresh tokens and sessions
-- (auth.refresh_tokens and auth.mfa_amr_claims also cascade from auth.sessions). Afterwards no
-- refresh token of the user works, and GoTrue answers /user with 403 session_not_found for every
-- access token issued before (their session_id no longer exists).
--
-- SECURITY DEFINER with a pinned search_path and schema-qualified names; EXECUTE for service_role
-- only. The owner (the role running this file, postgres on Supabase) needs DELETE on
-- auth.sessions and auth.refresh_tokens AND must bypass row level security: GoTrue enables RLS on
-- those tables without policies, so an owner subject to RLS would see no rows, delete nothing and
-- report success. Both are checked here (the migration fails) and again on every call (the call
-- fails instead of claiming the sessions were revoked).
-- Idempotent: CREATE OR REPLACE.

BEGIN;

DO $$
BEGIN
    IF NOT has_table_privilege(current_user, 'auth.sessions', 'DELETE')
       OR NOT has_table_privilege(current_user, 'auth.refresh_tokens', 'DELETE') THEN
        RAISE EXCEPTION '% needs DELETE on auth.sessions and auth.refresh_tokens', current_user;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles
                    WHERE rolname = current_user AND (rolsuper OR rolbypassrls)) THEN
        RAISE EXCEPTION '% must bypass row level security to see the rows of auth.sessions', current_user;
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION public.admin_revoke_user_sessions(p_user_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_refresh_tokens integer;
    v_sessions       integer;
BEGIN
    IF p_user_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'admin_revoke_user_sessions: p_user_id is required';
    END IF;
    -- current_user is the owner here (SECURITY DEFINER): without RLS bypass the deletes see nothing
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles
                    WHERE rolname = current_user AND (rolsuper OR rolbypassrls)) THEN
        RAISE EXCEPTION USING ERRCODE = '42501',
            MESSAGE = 'admin_revoke_user_sessions: the function owner must bypass row level security';
    END IF;

    -- every refresh token of the user, including legacy ones that have no session
    DELETE FROM auth.refresh_tokens WHERE user_id = p_user_id::text;
    GET DIAGNOSTICS v_refresh_tokens = ROW_COUNT;

    DELETE FROM auth.sessions WHERE user_id = p_user_id;
    GET DIAGNOSTICS v_sessions = ROW_COUNT;

    RETURN jsonb_build_object('sessions', v_sessions, 'refresh_tokens', v_refresh_tokens);
END;
$$;

REVOKE ALL ON FUNCTION public.admin_revoke_user_sessions(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_revoke_user_sessions(uuid) TO service_role;

COMMIT;

-- PostgREST caches the schema; make the new function callable right away.
NOTIFY pgrst, 'reload schema';
