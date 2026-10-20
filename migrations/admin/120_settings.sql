/*
  120_settings.sql  (MS-5091)

  One audited settings save for the admin API (PUT /api/v1/admin/settings). Replaces the four
  separate browser calls of the portal (upsert, audit insert, delete, APP_CACHE_KEY update).

  Public function (EXECUTE: service_role only):

    admin_upsert_settings(p_items jsonb, p_deleted text[], p_actor uuid,
                          p_ip text DEFAULT NULL, p_reason text DEFAULT NULL)
      -> jsonb {"updated": [keys], "deleted": [keys], "cache_key_rotated": bool}

    p_items: [{"key": "<text>", "value": "<text>"}, ...]

  In ONE transaction (a PostgREST RPC is one transaction):
    1. validates the input (AD422, see below);
    2. locks the touched app_config rows (FOR UPDATE, in key order);
    3. upserts every item whose value differs (or whose key is new) -> "updated";
    4. deletes every listed key that exists -> "deleted";
    5. nothing updated and nothing deleted -> AD409 no_change (nothing written, nothing audited);
    6. rotates APP_CACHE_KEY (admin_rotate_cache_key, 050) when FORBIDDEN_COUNTRIES was created,
       changed or deleted: the same rule as the portal's updateSettings() before the admin API;
    7. writes ONE audit row through admin_write_audit (050):
         table_name 'app_config', operation 'UPDATE', record_id NULL,
         old_data = {KEY: old value} of the updated keys that existed and of the deleted keys,
         new_data = {KEY: new value} of the updated keys,
         operation_details = {updated, deleted, rows_affected, cache_key_rotated}
                             + ip_address, reason, client_reported:false (added by admin_write_audit).
       Values of secret-shaped keys (admin_setting_is_secret) are stored as '***'.
  Any failure (including the audit insert) rolls the whole save back.

  Concurrency: two saves touching the same keys serialise on the row locks; last write wins per
  key and both are audited. A key inserted concurrently by another save is handled by
  ON CONFLICT (key) (unique app_config_key_key, 020 / supabase_ddl.sql).

  Errors (SQLSTATE / DETAIL code)
    AD422 invalid_input        p_items not an array of {key: text, value: text}, empty save,
                               key longer than 100, value longer than 10 000
    AD422 cache_key_forbidden  APP_CACHE_KEY in p_items or p_deleted
    AD422 secret_key           a secret-shaped key in p_items (secrets live in the environment,
                               app/config/helper.py is_secret_key); deleting one is allowed
    AD422 duplicate_key        a key twice in p_items, or in both lists
    AD409 no_change            nothing would change

  admin_setting_is_secret(text) mirrors is_secret_key() of app/config/helper.py: names ending in
  _KEY, _SECRET, _PASSWORD, _TOKEN or _USERNAME, and RESELLER_ID; APP_CACHE_KEY and CACHE_KEY are
  not secrets. Keep both in sync.

  Idempotent (CREATE OR REPLACE, REVOKE/GRANT). Depends on 050_admin_audit_helpers.sql.
*/

------------------------------------------------------------------------------
-- Internal: same rule as app/config/helper.py is_secret_key()
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_setting_is_secret(p_key text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
SET search_path = public, pg_temp
AS $$
    SELECT CASE
        WHEN p_key IS NULL THEN false
        WHEN upper(p_key) IN ('APP_CACHE_KEY', 'CACHE_KEY') THEN false
        ELSE upper(p_key) = 'RESELLER_ID'
             OR upper(p_key) ~ '(_KEY|_SECRET|_PASSWORD|_TOKEN|_USERNAME)$'
    END;
$$;

------------------------------------------------------------------------------
-- admin_upsert_settings
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_upsert_settings(
    p_items   jsonb,
    p_deleted text[],
    p_actor   uuid,
    p_ip      text DEFAULT NULL,
    p_reason  text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_items     jsonb  := coalesce(p_items, '[]'::jsonb);
    v_deleted   text[];
    v_item      jsonb;
    v_key       text;
    v_value     text;
    v_old       text;
    v_exists    boolean;
    v_bad       text;
    v_updated   text[] := ARRAY[]::text[];
    v_removed   text[] := ARRAY[]::text[];
    v_old_data  jsonb  := '{}'::jsonb;
    v_new_data  jsonb  := '{}'::jsonb;
    v_forbidden boolean := false;
    v_rotated   boolean := false;
BEGIN
    IF p_actor IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_actor is required',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_actor')::text;
    END IF;

    IF jsonb_typeof(v_items) <> 'array' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_items must be a JSON array',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_items')::text;
    END IF;

    SELECT coalesce(array_agg(DISTINCT d ORDER BY d), ARRAY[]::text[])
      INTO v_deleted
      FROM unnest(coalesce(p_deleted, ARRAY[]::text[])) AS d
     WHERE d IS NOT NULL;

    IF jsonb_array_length(v_items) = 0 AND cardinality(v_deleted) = 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'nothing to save',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_items')::text;
    END IF;

    -- shape: {key: text 1..100, value: text <= 10000}
    IF EXISTS (SELECT 1 FROM jsonb_array_elements(v_items) e
                WHERE jsonb_typeof(e) <> 'object'
                   OR jsonb_typeof(e -> 'key')   IS DISTINCT FROM 'string'
                   OR jsonb_typeof(e -> 'value') IS DISTINCT FROM 'string'
                   OR length(e ->> 'key') NOT BETWEEN 1 AND 100
                   OR length(e ->> 'value') > 10000)
       OR EXISTS (SELECT 1 FROM unnest(v_deleted) d WHERE length(d) NOT BETWEEN 1 AND 100) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'each item needs a text key (1-100) and a text value (max 10000)',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_items')::text;
    END IF;

    IF EXISTS (SELECT 1 FROM jsonb_array_elements(v_items) e WHERE e ->> 'key' = 'APP_CACHE_KEY')
       OR 'APP_CACHE_KEY' = ANY (v_deleted) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'APP_CACHE_KEY cannot be written directly',
            DETAIL  = jsonb_build_object('code', 'cache_key_forbidden')::text;
    END IF;

    SELECT e ->> 'key' INTO v_bad
      FROM jsonb_array_elements(v_items) e
     WHERE public.admin_setting_is_secret(e ->> 'key')
     LIMIT 1;
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = format('%s is a secret: secrets are set in the environment, not in app_config', v_bad),
            DETAIL  = jsonb_build_object('code', 'secret_key', 'key', v_bad)::text;
    END IF;

    SELECT k INTO v_bad
      FROM (SELECT e ->> 'key' AS k FROM jsonb_array_elements(v_items) e
            UNION ALL
            SELECT d FROM unnest(v_deleted) d) keys
     GROUP BY k
    HAVING count(*) > 1
     LIMIT 1;
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = format('key %s appears more than once', v_bad),
            DETAIL  = jsonb_build_object('code', 'duplicate_key', 'key', v_bad)::text;
    END IF;

    -- Lock the existing rows in a stable order (no deadlock between two saves).
    PERFORM 1
       FROM public.app_config c
      WHERE c.key IN (SELECT e ->> 'key' FROM jsonb_array_elements(v_items) e
                      UNION
                      SELECT d FROM unnest(v_deleted) d)
      ORDER BY c.key
        FOR UPDATE;

    -- Upserts
    FOR v_item IN
        SELECT e FROM jsonb_array_elements(v_items) e ORDER BY e ->> 'key'
    LOOP
        v_key   := v_item ->> 'key';
        v_value := v_item ->> 'value';

        SELECT c.value INTO v_old FROM public.app_config c WHERE c.key = v_key;
        v_exists := FOUND;

        IF v_exists AND v_old IS NOT DISTINCT FROM v_value THEN
            CONTINUE;
        END IF;

        IF v_exists THEN
            UPDATE public.app_config SET value = v_value WHERE key = v_key;
            v_old_data := v_old_data || jsonb_build_object(v_key, v_old);
        ELSE
            INSERT INTO public.app_config (key, value)
            VALUES (v_key, v_value)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
        END IF;

        v_updated  := v_updated || v_key;
        v_new_data := v_new_data || jsonb_build_object(v_key, v_value);

        IF v_key = 'FORBIDDEN_COUNTRIES' THEN
            v_forbidden := true;
        END IF;
    END LOOP;

    -- Deletes
    FOREACH v_key IN ARRAY v_deleted
    LOOP
        DELETE FROM public.app_config WHERE key = v_key RETURNING value INTO v_old;
        IF FOUND THEN
            v_removed  := v_removed || v_key;
            v_old_data := v_old_data || jsonb_build_object(
                              v_key, CASE WHEN public.admin_setting_is_secret(v_key) THEN '***' ELSE v_old END);
            IF v_key = 'FORBIDDEN_COUNTRIES' THEN
                v_forbidden := true;
            END IF;
        END IF;
    END LOOP;

    IF cardinality(v_updated) = 0 AND cardinality(v_removed) = 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD409',
            MESSAGE = 'no_change',
            DETAIL  = jsonb_build_object('code', 'no_change')::text;
    END IF;

    IF v_forbidden THEN
        v_rotated := public.admin_rotate_cache_key() IS NOT NULL;
    END IF;

    PERFORM public.admin_write_audit(
        'app_config', 'UPDATE', NULL, v_old_data, v_new_data, p_actor, p_ip, p_reason,
        jsonb_build_object(
            'updated',           to_jsonb(v_updated),
            'deleted',           to_jsonb(v_removed),
            'rows_affected',     cardinality(v_updated) + cardinality(v_removed),
            'cache_key_rotated', v_rotated));

    RETURN jsonb_build_object(
        'updated',           to_jsonb(v_updated),
        'deleted',           to_jsonb(v_removed),
        'cache_key_rotated', v_rotated);
END;
$$;

------------------------------------------------------------------------------
-- Privileges
------------------------------------------------------------------------------
REVOKE ALL ON FUNCTION public.admin_setting_is_secret(text) FROM PUBLIC, anon, authenticated;

REVOKE ALL ON FUNCTION public.admin_upsert_settings(jsonb, text[], uuid, text, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_upsert_settings(jsonb, text[], uuid, text, text)
    TO service_role;
