/*
  080_users.sql  (MS-5079 users page and user detail; fixes MS-5067)

  Read side of the Users page for the admin API. Never writes.

  Public functions (EXECUTE: service_role only):

    admin_search_users(p_search text DEFAULT NULL, p_ban_status text DEFAULT 'all',
                       p_limit integer DEFAULT 20, p_offset integer DEFAULT 0,
                       p_id uuid DEFAULT NULL)
      -> jsonb {"data": [user rows], "total": n, "facets": {"all": n, "banned": n, "active": n}}

    - Source: users_copy (email, metadata = auth.users.raw_user_meta_data) LEFT JOIN auth.users
      for banned_until, raw_app_meta_data->>'role', created_at and last_sign_in_at. The legacy
      portal read the ban state with one GoTrue call per row, or listed 20 000 auth users in the
      browser when the ban filter was on (MS-5067); this join replaces both.
    - Row: {id, email, first_name, last_name, msisdn, referral_code, email_verified,
            phone_verified, should_notify, role, banned_until, is_banned, created_at,
            last_sign_in_at}. is_banned = banned_until in the future; a lapsed ban is "active".
    - p_search: literal match on email (ILIKE with %, _ and \ escaped).
    - p_ban_status: all | banned | active, else AD422.
    - p_id: only that user (the detail route); the facets are over the searched set anyway.
    - facets: counts over the searched set (p_search and p_id) whatever p_ban_status says.
    - Order: auth.users.created_at DESC NULLS LAST, id DESC (stable paging, MS-5067).
    - p_limit 1..100, p_offset >= 0, else AD422.

    admin_wallet_transactions(p_user_id uuid, p_limit integer DEFAULT 20, p_offset integer DEFAULT 0)
      -> jsonb {"data": [{id, wallet_id, amount, status, source, currency, created_at}], "total": n}

    - Rewrite of get_wallet_transactions_with_count for one user, with the true total (the legacy
      "count" was the page length) and a stable order (created_at DESC, id DESC).
    - p_user_id is required (AD422 when NULL): the admin API only lists one user's wallet.

  Idempotent (CREATE OR REPLACE, REVOKE/GRANT).
*/

------------------------------------------------------------------------------
-- admin_search_users
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_search_users(
    p_search     text    DEFAULT NULL,
    p_ban_status text    DEFAULT 'all',
    p_limit      integer DEFAULT 20,
    p_offset     integer DEFAULT 0,
    p_id         uuid    DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_pattern text;
    v_status  text := coalesce(nullif(btrim(p_ban_status), ''), 'all');
    v_result  jsonb;
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 100 OR p_offset IS NULL OR p_offset < 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_limit must be 1..100 and p_offset >= 0',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_limit')::text;
    END IF;

    IF v_status NOT IN ('all', 'banned', 'active') THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_ban_status must be all, banned or active',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_ban_status')::text;
    END IF;

    IF p_search IS NOT NULL AND btrim(p_search) <> '' THEN
        -- literal match: escape the LIKE wildcards (default escape character is the backslash)
        v_pattern := '%' || replace(replace(replace(btrim(p_search), '\', '\\'), '%', '\%'), '_', '\_') || '%';
    END IF;

    WITH base AS (
        SELECT u.id,
               u.email,
               u.metadata ->> 'first_name'                       AS first_name,
               u.metadata ->> 'last_name'                        AS last_name,
               u.metadata ->> 'msisdn'                           AS msisdn,
               u.metadata ->> 'referral_code'                    AS referral_code,
               (u.metadata -> 'email_verified')::boolean          AS email_verified,
               (u.metadata -> 'phone_verified')::boolean          AS phone_verified,
               (u.metadata -> 'should_notify')::boolean           AS should_notify,
               au.raw_app_meta_data ->> 'role'                   AS role,
               au.banned_until,
               (au.banned_until IS NOT NULL AND au.banned_until > now()) AS is_banned,
               au.created_at,
               au.last_sign_in_at
          FROM public.users_copy u
          LEFT JOIN auth.users au ON au.id = u.id
         WHERE (p_id IS NULL OR u.id = p_id)
           AND (v_pattern IS NULL OR u.email ILIKE v_pattern)
    ),
    filtered AS (
        SELECT *
          FROM base
         WHERE v_status = 'all'
            OR (v_status = 'banned' AND is_banned)
            OR (v_status = 'active' AND NOT is_banned)
    ),
    page AS (
        SELECT f.*
          FROM filtered f
         ORDER BY f.created_at DESC NULLS LAST, f.id DESC
         LIMIT p_limit OFFSET p_offset
    )
    SELECT jsonb_build_object(
               'data', coalesce((SELECT jsonb_agg(to_jsonb(p) ORDER BY p.created_at DESC NULLS LAST, p.id DESC)
                                   FROM page p), '[]'::jsonb),
               'total', (SELECT count(*) FROM filtered),
               'facets', jsonb_build_object(
                   'all',    (SELECT count(*) FROM base),
                   'banned', (SELECT count(*) FROM base WHERE is_banned),
                   'active', (SELECT count(*) FROM base WHERE NOT is_banned)))
      INTO v_result;

    RETURN v_result;
END;
$$;

------------------------------------------------------------------------------
-- admin_wallet_transactions
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_wallet_transactions(
    p_user_id uuid,
    p_limit   integer DEFAULT 20,
    p_offset  integer DEFAULT 0
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_result jsonb;
BEGIN
    IF p_user_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_user_id is required',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_user_id')::text;
    END IF;

    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 100 OR p_offset IS NULL OR p_offset < 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_limit must be 1..100 and p_offset >= 0',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_limit')::text;
    END IF;

    WITH filtered AS (
        SELECT uwt.id,
               uwt.wallet_id,
               uwt.amount,
               uwt.status,
               uwt.source,
               uw.currency,
               uwt.created_at
          FROM public.user_wallet_transaction uwt
          JOIN public.user_wallet uw ON uw.id = uwt.wallet_id
         WHERE uw.user_id = p_user_id
    ),
    page AS (
        SELECT f.*
          FROM filtered f
         ORDER BY f.created_at DESC NULLS LAST, f.id DESC
         LIMIT p_limit OFFSET p_offset
    )
    SELECT jsonb_build_object(
               'data', coalesce((SELECT jsonb_agg(to_jsonb(p) ORDER BY p.created_at DESC NULLS LAST, p.id DESC)
                                   FROM page p), '[]'::jsonb),
               'total', (SELECT count(*) FROM filtered))
      INTO v_result;

    RETURN v_result;
END;
$$;

------------------------------------------------------------------------------
-- Privileges
------------------------------------------------------------------------------
REVOKE ALL ON FUNCTION public.admin_search_users(text, text, integer, integer, uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_search_users(text, text, integer, integer, uuid)
    TO service_role;

REVOKE ALL ON FUNCTION public.admin_wallet_transactions(uuid, integer, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_wallet_transactions(uuid, integer, integer)
    TO service_role;
