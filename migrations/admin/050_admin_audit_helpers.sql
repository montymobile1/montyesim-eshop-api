/*
  050_admin_audit_helpers.sql  (MS-5075)

  Generic audited writes for the admin API. Each call runs in ONE transaction
  (a PostgREST RPC is one transaction): the change, the optional
  APP_CACHE_KEY rotation and the audit_log row commit or roll back together,
  so a failed audit insert aborts the write by construction.

  Public functions (EXECUTE: service_role only):

    admin_write_audit(p_table text, p_operation text, p_record_id text,
                      p_old_data jsonb, p_new_data jsonb, p_actor uuid,
                      p_ip text, p_reason text, p_details jsonb DEFAULT '{}')
      -> jsonb  (the inserted audit_log row)

    admin_rotate_cache_key()
      -> text   (the new APP_CACHE_KEY value, NULL when the key row is missing)

    admin_insert_audited(p_table text, p_row jsonb, p_actor uuid,
                         p_ip text DEFAULT NULL, p_reason text DEFAULT NULL,
                         p_rotate_cache boolean DEFAULT false)
      -> jsonb  (the inserted row)

    admin_update_audited(p_table text, p_id text, p_patch jsonb, p_actor uuid,
                         p_ip text DEFAULT NULL, p_reason text DEFAULT NULL,
                         p_operation text DEFAULT 'UPDATE',
                         p_rotate_cache boolean DEFAULT false,
                         p_guard jsonb DEFAULT NULL)
      -> jsonb  (the updated row)

    admin_delete_audited(p_table text, p_id text, p_actor uuid,
                         p_ip text DEFAULT NULL, p_reason text DEFAULT NULL,
                         p_rotate_cache boolean DEFAULT false,
                         p_guard jsonb DEFAULT NULL)
      -> jsonb  (the deleted row)

  Rules
  - Table allowlist: app_config, banner, bundle, bundle_tag, promotion,
    promotion_rule, tag, tag_group, voucher. Primary key is "id" on all of
    them; p_id is passed as text and cast to the real key type
    (uuid / bigint / integer) through jsonb_populate_record.
  - Column names in p_row / p_patch / p_guard must be real columns
    (generated and GENERATED ALWAYS identity columns are not writable; "id"
    cannot be patched).
  - Dynamic SQL uses format() with %I for identifiers and bind parameters
    ($1) for values. The only %s placeholders receive column lists that are
    themselves built exclusively from format('%I ...') fragments.
  - app_config rows with key APP_CACHE_KEY cannot be touched here
    (use admin_rotate_cache_key).
  - When the table has an updated_at column and the patch does not set it,
    admin_update_audited sets it to now().

  Audit row written by the helpers
    table_name = p_table, record_id = id when it is a uuid (else NULL),
    operation = INSERT | p_operation | DELETE, changed_by = p_actor,
    old_data / new_data = full row snapshots (jsonb objects),
    operation_details = p_details || {ip_address, reason, client_reported:false,
                        record_id (text, always), rows_affected,
                        cache_key_rotated, changed_columns (update only)}

  Errors (SQLSTATE / MESSAGE / DETAIL is a JSON string with "code")
    AD404  not_found                 row with p_id does not exist
    AD409  no_change                 update would not change any column
    AD409  guard_failed              row does not match p_guard
                                     (DETAIL has "expected" and "actual")
    AD422  <human readable text>     DETAIL code: table_not_allowed,
                                     unknown_column, invalid_input,
                                     invalid_value, cache_key_forbidden
    Native errors are left as they are: 23505 unique (duplicate_*),
    23503 foreign key (rule_in_use, promotion_used, ...), 23502 not null,
    23514 check, P0001 from triggers (e.g. 'Invalid bundle code: X').

  p_guard: {"column": value, ...}; every column of the current row must be
  equal (jsonb equality on to_jsonb(row)) to the given value, otherwise
  AD409 guard_failed. Example: admin_delete_audited('voucher', '12', ...,
  p_guard => '{"is_used": false}').

  Idempotent (CREATE OR REPLACE, REVOKE/GRANT).
*/

------------------------------------------------------------------------------
-- Internal: allowlist check, returns the table's regclass
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_assert_table(p_table text)
RETURNS regclass
LANGUAGE plpgsql
STABLE
SET search_path = public, pg_temp
AS $$
DECLARE
    v_rel regclass;
BEGIN
    IF p_table IS NULL
       OR NOT (p_table = ANY (ARRAY['app_config', 'banner', 'bundle', 'bundle_tag', 'promotion',
                                    'promotion_rule', 'tag', 'tag_group', 'voucher'])) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = format('table not allowed: %s', coalesce(p_table, '<null>')),
            DETAIL  = jsonb_build_object('code', 'table_not_allowed', 'table', p_table)::text;
    END IF;

    v_rel := to_regclass(format('public.%I', p_table));

    IF v_rel IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = format('table does not exist: %s', p_table),
            DETAIL  = jsonb_build_object('code', 'table_not_allowed', 'table', p_table)::text;
    END IF;

    RETURN v_rel;
END;
$$;

------------------------------------------------------------------------------
-- Internal: validates the keys of a JSON object against the table columns.
--   p_writable: reject generated / GENERATED ALWAYS identity columns
--   p_allow_id: accept the "id" key
-- Returns the keys (sorted).
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_assert_columns(
    p_rel      regclass,
    p_data     jsonb,
    p_param    text,
    p_writable boolean,
    p_allow_id boolean
)
RETURNS text[]
LANGUAGE plpgsql
STABLE
SET search_path = public, pg_temp
AS $$
DECLARE
    v_keys text[];
    v_bad  text[];
BEGIN
    IF p_data IS NULL OR jsonb_typeof(p_data) <> 'object' OR p_data = '{}'::jsonb THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = format('%s must be a non-empty JSON object', p_param),
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', p_param)::text;
    END IF;

    SELECT array_agg(t.k ORDER BY t.k)
      INTO v_keys
      FROM jsonb_object_keys(p_data) AS t(k);

    SELECT array_agg(u.k ORDER BY u.k)
      INTO v_bad
      FROM unnest(v_keys) AS u(k)
     WHERE (NOT p_allow_id AND u.k = 'id')
        OR NOT EXISTS (
               SELECT 1
                 FROM pg_attribute a
                WHERE a.attrelid = p_rel
                  AND a.attname = u.k
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                  AND (NOT p_writable OR (a.attgenerated = '' AND a.attidentity <> 'a'))
           );

    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = format('unknown or read-only column(s) in %s: %s', p_param, array_to_string(v_bad, ', ')),
            DETAIL  = jsonb_build_object('code', 'unknown_column', 'param', p_param, 'columns', to_jsonb(v_bad))::text;
    END IF;

    RETURN v_keys;
END;
$$;

------------------------------------------------------------------------------
-- Internal: APP_CACHE_KEY is only changed by admin_rotate_cache_key
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_assert_not_cache_key(p_table text, p_a jsonb, p_b jsonb DEFAULT NULL)
RETURNS void
LANGUAGE plpgsql
IMMUTABLE
SET search_path = public, pg_temp
AS $$
BEGIN
    IF p_table = 'app_config'
       AND ('APP_CACHE_KEY' = (p_a ->> 'key') OR 'APP_CACHE_KEY' = (p_b ->> 'key')) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'APP_CACHE_KEY cannot be written directly',
            DETAIL  = jsonb_build_object('code', 'cache_key_forbidden')::text;
    END IF;
END;
$$;

------------------------------------------------------------------------------
-- Internal: AD409 guard_failed when the row does not match p_guard
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_check_guard(
    p_rel   regclass,
    p_table text,
    p_id    text,
    p_row   jsonb,
    p_guard jsonb
)
RETURNS void
LANGUAGE plpgsql
STABLE
SET search_path = public, pg_temp
AS $$
DECLARE
    v_actual   jsonb;
    v_expected jsonb;
BEGIN
    IF p_guard IS NULL OR p_guard = '{}'::jsonb THEN
        RETURN;
    END IF;

    PERFORM public.admin_assert_columns(p_rel, p_guard, 'p_guard', false, true);

    SELECT jsonb_object_agg(g.key, p_row -> g.key),
           jsonb_object_agg(g.key, g.value)
      INTO v_actual, v_expected
      FROM jsonb_each(p_guard) AS g
     WHERE (p_row -> g.key) IS DISTINCT FROM g.value;

    IF v_actual IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD409',
            MESSAGE = 'guard_failed',
            DETAIL  = jsonb_build_object('code', 'guard_failed', 'table', p_table, 'id', p_id,
                                         'expected', v_expected, 'actual', v_actual)::text;
    END IF;
END;
$$;

------------------------------------------------------------------------------
-- Internal: SELECT ... FOR UPDATE of one row by id, as jsonb (NULL = missing)
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_lock_row(p_table text, p_id text)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SET search_path = public, pg_temp
AS $$
DECLARE
    v_row jsonb;
BEGIN
    IF p_id IS NULL OR btrim(p_id) = '' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_id is required',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_id')::text;
    END IF;

    BEGIN
        EXECUTE format(
            'SELECT to_jsonb(t.*) FROM public.%I AS t '
            'WHERE t.id = (jsonb_populate_record(NULL::public.%I, $1)).id '
            'FOR UPDATE',
            p_table, p_table)
          INTO v_row
         USING jsonb_build_object('id', p_id);
    EXCEPTION WHEN data_exception THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = format('invalid id for %s: %s', p_table, p_id),
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_id', 'error', SQLERRM)::text;
    END;

    RETURN v_row;
END;
$$;

------------------------------------------------------------------------------
-- admin_write_audit: one audit_log row (also used directly by AuditService
-- for non-DB effects such as GoTrue ban/unban and exports; p_table is free
-- text there, e.g. 'auth.users').
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_write_audit(
    p_table     text,
    p_operation text,
    p_record_id text,
    p_old_data  jsonb,
    p_new_data  jsonb,
    p_actor     uuid,
    p_ip        text,
    p_reason    text,
    p_details   jsonb DEFAULT '{}'::jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_details jsonb;
    v_row     public.audit_log;
BEGIN
    IF p_actor IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_actor is required',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_actor')::text;
    END IF;

    IF p_table IS NULL OR btrim(p_table) = '' OR length(p_table) > 255 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_table is required (max 255 characters)',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_table')::text;
    END IF;

    IF p_operation IS NULL OR p_operation !~ '^[A-Z][A-Z_]{0,49}$' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = format('invalid operation: %s (expected upper-case letters and _, max 50)',
                             coalesce(p_operation, '<null>')),
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_operation')::text;
    END IF;

    IF p_details IS NOT NULL AND jsonb_typeof(p_details) <> 'object' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_details must be a JSON object',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_details')::text;
    END IF;

    -- Fixed keys last so callers cannot override them.
    v_details := coalesce(p_details, '{}'::jsonb)
                 || jsonb_build_object(
                        'ip_address',      nullif(btrim(p_ip), ''),
                        'reason',          nullif(btrim(p_reason), ''),
                        'client_reported', false);

    IF p_record_id IS NOT NULL THEN
        v_details := v_details || jsonb_build_object('record_id', p_record_id);
    END IF;

    INSERT INTO public.audit_log
        (table_name, record_id, operation, changed_by, old_data, new_data, operation_details)
    VALUES
        (p_table,
         CASE WHEN p_record_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
              THEN p_record_id::uuid END,
         p_operation,
         p_actor,
         p_old_data,
         p_new_data,
         v_details)
    RETURNING * INTO v_row;

    RETURN to_jsonb(v_row);
END;
$$;

------------------------------------------------------------------------------
-- admin_rotate_cache_key: same effect as the portal's refreshAppCacheKey()
-- (bundlesAPI.jsx / groupsAPI.jsx / settingsAPI.jsx): when the APP_CACHE_KEY
-- row exists, its value becomes a new random v4 UUID in canonical lower-case
-- text form (crypto.randomUUID() == gen_random_uuid()::text). When the row is
-- missing nothing is inserted (the portal skips silently too) and NULL is
-- returned. Not audited, as today.
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_rotate_cache_key()
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_value text;
BEGIN
    UPDATE public.app_config
       SET value = gen_random_uuid()::text
     WHERE key = 'APP_CACHE_KEY'
    RETURNING value INTO v_value;

    RETURN v_value;
END;
$$;

------------------------------------------------------------------------------
-- admin_insert_audited
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_insert_audited(
    p_table        text,
    p_row          jsonb,
    p_actor        uuid,
    p_ip           text    DEFAULT NULL,
    p_reason       text    DEFAULT NULL,
    p_rotate_cache boolean DEFAULT false
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_rel      regclass;
    v_cols     text[];
    v_col_list text;
    v_sel_list text;
    v_new      jsonb;
    v_rotated  boolean := false;
BEGIN
    v_rel := public.admin_assert_table(p_table);

    IF p_actor IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_actor is required',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_actor')::text;
    END IF;

    v_cols := public.admin_assert_columns(v_rel, p_row, 'p_row', true, true);
    PERFORM public.admin_assert_not_cache_key(p_table, p_row);

    SELECT string_agg(format('%I', u.c), ', ' ORDER BY u.n),
           string_agg(format('r.%I', u.c), ', ' ORDER BY u.n)
      INTO v_col_list, v_sel_list
      FROM unnest(v_cols) WITH ORDINALITY AS u(c, n);

    BEGIN
        EXECUTE format(
            'INSERT INTO public.%I AS t (%s) '
            'SELECT %s FROM jsonb_populate_record(NULL::public.%I, $1) AS r '
            'RETURNING to_jsonb(t.*)',
            p_table, v_col_list, v_sel_list, p_table)
          INTO v_new
         USING p_row;
    EXCEPTION WHEN data_exception THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = format('invalid value for %s: %s', p_table, SQLERRM),
            DETAIL  = jsonb_build_object('code', 'invalid_value', 'error', SQLERRM)::text;
    END;

    IF p_rotate_cache THEN
        v_rotated := public.admin_rotate_cache_key() IS NOT NULL;
    END IF;

    PERFORM public.admin_write_audit(
        p_table, 'INSERT', v_new ->> 'id', NULL, v_new, p_actor, p_ip, p_reason,
        jsonb_build_object('rows_affected', 1, 'cache_key_rotated', v_rotated));

    RETURN v_new;
END;
$$;

------------------------------------------------------------------------------
-- admin_update_audited
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_update_audited(
    p_table        text,
    p_id           text,
    p_patch        jsonb,
    p_actor        uuid,
    p_ip           text    DEFAULT NULL,
    p_reason       text    DEFAULT NULL,
    p_operation    text    DEFAULT 'UPDATE',
    p_rotate_cache boolean DEFAULT false,
    p_guard        jsonb   DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_rel     regclass;
    v_cols    text[];
    v_old     jsonb;
    v_target  jsonb;
    v_new     jsonb;
    v_set     text;
    v_rotated boolean := false;
BEGIN
    v_rel := public.admin_assert_table(p_table);

    IF p_actor IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_actor is required',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_actor')::text;
    END IF;

    v_cols := public.admin_assert_columns(v_rel, p_patch, 'p_patch', true, false);

    v_old := public.admin_lock_row(p_table, p_id);

    IF v_old IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD404',
            MESSAGE = 'not_found',
            DETAIL  = jsonb_build_object('code', 'not_found', 'table', p_table, 'id', p_id)::text;
    END IF;

    PERFORM public.admin_assert_not_cache_key(p_table, v_old, p_patch);
    PERFORM public.admin_check_guard(v_rel, p_table, p_id, v_old, p_guard);

    -- Normalise the patch through the column types, then compare with the
    -- current row: nothing differs -> AD409 no_change.
    BEGIN
        EXECUTE format('SELECT to_jsonb(r) FROM jsonb_populate_record(NULL::public.%I, $1) AS r', p_table)
          INTO v_target
         USING v_old || p_patch;
    EXCEPTION WHEN data_exception THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = format('invalid value for %s: %s', p_table, SQLERRM),
            DETAIL  = jsonb_build_object('code', 'invalid_value', 'error', SQLERRM)::text;
    END;

    IF NOT EXISTS (
        SELECT 1 FROM unnest(v_cols) AS u(c)
         WHERE (v_target -> u.c) IS DISTINCT FROM (v_old -> u.c)
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD409',
            MESSAGE = 'no_change',
            DETAIL  = jsonb_build_object('code', 'no_change', 'table', p_table, 'id', p_id)::text;
    END IF;

    SELECT string_agg(format('%I = r.%I', u.c, u.c), ', ')
      INTO v_set
      FROM unnest(v_cols) AS u(c);

    IF NOT ('updated_at' = ANY (v_cols)) AND EXISTS (
        SELECT 1 FROM pg_attribute a
         WHERE a.attrelid = v_rel AND a.attname = 'updated_at' AND a.attnum > 0 AND NOT a.attisdropped
    ) THEN
        v_set := v_set || ', ' || format('%I = now()', 'updated_at');
    END IF;

    BEGIN
        EXECUTE format(
            'UPDATE public.%I AS t SET %s '
            'FROM jsonb_populate_record(NULL::public.%I, $1) AS r '
            'WHERE t.id = r.id '
            'RETURNING to_jsonb(t.*)',
            p_table, v_set, p_table)
          INTO v_new
         USING p_patch || jsonb_build_object('id', p_id);
    EXCEPTION WHEN data_exception THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = format('invalid value for %s: %s', p_table, SQLERRM),
            DETAIL  = jsonb_build_object('code', 'invalid_value', 'error', SQLERRM)::text;
    END;

    IF p_rotate_cache THEN
        v_rotated := public.admin_rotate_cache_key() IS NOT NULL;
    END IF;

    PERFORM public.admin_write_audit(
        p_table, coalesce(p_operation, 'UPDATE'), v_new ->> 'id', v_old, v_new, p_actor, p_ip, p_reason,
        jsonb_build_object('rows_affected', 1,
                           'cache_key_rotated', v_rotated,
                           'changed_columns', to_jsonb(v_cols)));

    RETURN v_new;
END;
$$;

------------------------------------------------------------------------------
-- admin_delete_audited
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_delete_audited(
    p_table        text,
    p_id           text,
    p_actor        uuid,
    p_ip           text    DEFAULT NULL,
    p_reason       text    DEFAULT NULL,
    p_rotate_cache boolean DEFAULT false,
    p_guard        jsonb   DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_rel     regclass;
    v_old     jsonb;
    v_rotated boolean := false;
BEGIN
    v_rel := public.admin_assert_table(p_table);

    IF p_actor IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_actor is required',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_actor')::text;
    END IF;

    v_old := public.admin_lock_row(p_table, p_id);

    IF v_old IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD404',
            MESSAGE = 'not_found',
            DETAIL  = jsonb_build_object('code', 'not_found', 'table', p_table, 'id', p_id)::text;
    END IF;

    PERFORM public.admin_assert_not_cache_key(p_table, v_old);
    PERFORM public.admin_check_guard(v_rel, p_table, p_id, v_old, p_guard);

    -- FK violations (23503) propagate: the API maps them to 409 *_in_use / *_used.
    EXECUTE format(
        'DELETE FROM public.%I AS t '
        'WHERE t.id = (jsonb_populate_record(NULL::public.%I, $1)).id',
        p_table, p_table)
      USING jsonb_build_object('id', p_id);

    IF p_rotate_cache THEN
        v_rotated := public.admin_rotate_cache_key() IS NOT NULL;
    END IF;

    PERFORM public.admin_write_audit(
        p_table, 'DELETE', v_old ->> 'id', v_old, NULL, p_actor, p_ip, p_reason,
        jsonb_build_object('rows_affected', 1, 'cache_key_rotated', v_rotated));

    RETURN v_old;
END;
$$;

------------------------------------------------------------------------------
-- Privileges: nobody but service_role (and the owner) may execute these.
-- Supabase grants EXECUTE on new public functions to anon/authenticated by
-- default privileges, hence the explicit REVOKE.
------------------------------------------------------------------------------
REVOKE ALL ON FUNCTION public.admin_assert_table(text)                                   FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_assert_columns(regclass, jsonb, text, boolean, boolean) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_assert_not_cache_key(text, jsonb, jsonb)              FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_check_guard(regclass, text, text, jsonb, jsonb)       FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_lock_row(text, text)                                  FROM PUBLIC, anon, authenticated;

REVOKE ALL ON FUNCTION public.admin_write_audit(text, text, text, jsonb, jsonb, uuid, text, text, jsonb)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_rotate_cache_key()
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_insert_audited(text, jsonb, uuid, text, text, boolean)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_update_audited(text, text, jsonb, uuid, text, text, text, boolean, jsonb)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_delete_audited(text, text, uuid, text, text, boolean, jsonb)
    FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.admin_write_audit(text, text, text, jsonb, jsonb, uuid, text, text, jsonb)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.admin_rotate_cache_key()
    TO service_role;
GRANT EXECUTE ON FUNCTION public.admin_insert_audited(text, jsonb, uuid, text, text, boolean)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.admin_update_audited(text, text, jsonb, uuid, text, text, text, boolean, jsonb)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.admin_delete_audited(text, text, uuid, text, text, boolean, jsonb)
    TO service_role;
