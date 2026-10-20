/*
  110_audit_search.sql  (MS-5048 audit log, MS-5046 settings history)

  Read side of audit_log for the admin API. Never writes.

  Public function (EXECUTE: service_role only):

    admin_audit_log_search(p_table text DEFAULT NULL, p_operation text DEFAULT NULL,
                           p_changed_by uuid DEFAULT NULL,
                           p_from timestamp DEFAULT NULL, p_to timestamp DEFAULT NULL,
                           p_setting_key text DEFAULT NULL,
                           p_limit integer DEFAULT 20, p_offset integer DEFAULT 0)
      -> jsonb {"data": [raw audit_log rows], "total": n, "facets_by_table": {"<table>": n}}

    - p_table: exact table_name (the API only sends values of its enum).
    - p_operation: matched against admin_audit_operation(operation, operation_details), i.e. the
      NORMALISED operation (INSERT | UPDATE | DELETE | BAN | UNBAN | BULK). Legacy browser values
      are mapped: 'Ban User' -> BAN, 'Unban User' -> UNBAN, 'Activate Bundle' / 'Deactivate Bundle'
      -> BULK when operation_details.rows_affected > 1, else UPDATE; other values are upper-cased.
      Same mapping as normalize_operation() in app/services/admin/audit_query_service.py.
    - p_from inclusive, p_to EXCLUSIVE (the API passes the day after the "to" date).
      changed_at is "timestamp without time zone" (UTC on Supabase).
    - p_setting_key: only app_config rows where that key's value changed between old_data and
      new_data (both shapes: legacy JSON strings of [{key,value}] lists, and {KEY: value} objects
      written by admin_upsert_settings, and {id,key,value,created_at} row snapshots).
    - Order: changed_at DESC NULLS LAST, id DESC (index audit_log_changed_at_id_idx, 010).
    - facets_by_table: counts per table over every filter EXCEPT p_table, so the portal can show
      a count next to each table option.
    - p_limit 1..100, p_offset >= 0, else AD422.

  Internal helpers (no grant to anyone but the owner):
    admin_audit_jsonb(jsonb)          JSON strings parsed to jsonb, NULL when unparsable
    admin_settings_map(jsonb)         {key: value} from any settings audit shape, NULL when unreadable
    admin_audit_operation(text, jsonb) normalised operation

  Performance: filters on table_name / changed_at / changed_by use the indexes of 010. The key
  filter has to read the old/new data of the app_config rows (legacy rows are JSON strings, so no
  jsonb index can serve them); it runs only on the app_config rows that pass the other filters.

  Idempotent (CREATE OR REPLACE, REVOKE/GRANT).
*/

------------------------------------------------------------------------------
-- Internal: legacy rows hold JSON *strings* inside jsonb (JSON.stringify).
-- Returns the parsed value for a string that contains JSON, the value itself
-- otherwise, and NULL for a string that is not JSON.
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_audit_jsonb(p_value jsonb)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
SET search_path = public, pg_temp
AS $$
BEGIN
    IF p_value IS NULL OR jsonb_typeof(p_value) <> 'string' THEN
        RETURN p_value;
    END IF;
    BEGIN
        RETURN (p_value #>> '{}')::jsonb;
    EXCEPTION WHEN others THEN
        RETURN NULL;
    END;
END;
$$;

------------------------------------------------------------------------------
-- Internal: {key: value} from a settings audit payload. NULL when unreadable.
--   NULL                                  -> {}
--   [{"key": k, "value": v, ...}, ...]    -> {k: v, ...}        (legacy browser rows)
--   {"id":..,"key":k,"value":v,...}       -> {k: v}             (row snapshot)
--   {"K": v, ...}                         -> itself             (admin_upsert_settings)
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_settings_map(p_value jsonb)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
SET search_path = public, pg_temp
AS $$
DECLARE
    v_data jsonb;
    v_out  jsonb;
BEGIN
    IF p_value IS NULL OR jsonb_typeof(p_value) = 'null' THEN
        RETURN '{}'::jsonb;
    END IF;

    v_data := public.admin_audit_jsonb(p_value);

    IF v_data IS NULL THEN
        RETURN NULL;
    END IF;

    IF jsonb_typeof(v_data) = 'array' THEN
        IF EXISTS (SELECT 1 FROM jsonb_array_elements(v_data) e
                    WHERE jsonb_typeof(e) <> 'object' OR jsonb_typeof(e -> 'key') IS DISTINCT FROM 'string') THEN
            RETURN NULL;
        END IF;
        SELECT coalesce(jsonb_object_agg(e ->> 'key', e -> 'value'), '{}'::jsonb)
          INTO v_out
          FROM jsonb_array_elements(v_data) e;
        RETURN v_out;
    END IF;

    IF jsonb_typeof(v_data) = 'object' THEN
        IF jsonb_typeof(v_data -> 'key') = 'string' AND v_data ? 'value' AND v_data ? 'id' THEN
            RETURN jsonb_build_object(v_data ->> 'key', v_data -> 'value');
        END IF;
        RETURN v_data;
    END IF;

    RETURN NULL;
END;
$$;

------------------------------------------------------------------------------
-- Internal: normalised operation (see header)
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_audit_operation(p_operation text, p_details jsonb)
RETURNS text
LANGUAGE sql
IMMUTABLE
SET search_path = public, pg_temp
AS $$
    SELECT CASE
        WHEN upper(btrim(p_operation)) IN ('INSERT', 'UPDATE', 'DELETE', 'BAN', 'UNBAN', 'BULK')
            THEN upper(btrim(p_operation))
        WHEN upper(btrim(p_operation)) = 'BAN USER'   THEN 'BAN'
        WHEN upper(btrim(p_operation)) = 'UNBAN USER' THEN 'UNBAN'
        WHEN upper(btrim(p_operation)) IN ('ACTIVATE BUNDLE', 'DEACTIVATE BUNDLE') THEN
            CASE WHEN jsonb_typeof(p_details) = 'object'
                      AND (p_details ->> 'rows_affected') ~ '^[0-9]{1,9}$'
                      AND (p_details ->> 'rows_affected')::integer > 1
                 THEN 'BULK' ELSE 'UPDATE' END
        ELSE btrim(p_operation)
    END;
$$;

------------------------------------------------------------------------------
-- admin_audit_log_search
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_audit_log_search(
    p_table       text      DEFAULT NULL,
    p_operation   text      DEFAULT NULL,
    p_changed_by  uuid      DEFAULT NULL,
    p_from        timestamp DEFAULT NULL,
    p_to          timestamp DEFAULT NULL,
    p_setting_key text      DEFAULT NULL,
    p_limit       integer   DEFAULT 20,
    p_offset      integer   DEFAULT 0
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
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 100 OR p_offset IS NULL OR p_offset < 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_limit must be 1..100 and p_offset >= 0',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_limit')::text;
    END IF;

    IF p_from IS NOT NULL AND p_to IS NOT NULL AND p_from >= p_to THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_from must be before p_to',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_from')::text;
    END IF;

    WITH base AS (
        SELECT a.*
          FROM public.audit_log a
         WHERE (p_changed_by IS NULL OR a.changed_by = p_changed_by)
           AND (p_from IS NULL OR a.changed_at >= p_from)
           AND (p_to   IS NULL OR a.changed_at <  p_to)
           AND (p_operation IS NULL
                OR public.admin_audit_operation(a.operation, a.operation_details) = p_operation)
           AND (p_setting_key IS NULL
                OR (a.table_name = 'app_config'
                    AND public.admin_settings_map(a.old_data) IS NOT NULL
                    AND public.admin_settings_map(a.new_data) IS NOT NULL
                    AND (public.admin_settings_map(a.old_data) -> p_setting_key)
                        IS DISTINCT FROM
                        (public.admin_settings_map(a.new_data) -> p_setting_key)))
    ),
    filtered AS (
        SELECT * FROM base WHERE p_table IS NULL OR table_name = p_table
    ),
    page AS (
        SELECT f.*
          FROM filtered f
         ORDER BY f.changed_at DESC NULLS LAST, f.id DESC
         LIMIT p_limit OFFSET p_offset
    )
    SELECT jsonb_build_object(
               'data', coalesce((SELECT jsonb_agg(to_jsonb(p) ORDER BY p.changed_at DESC NULLS LAST, p.id DESC)
                                   FROM page p), '[]'::jsonb),
               'total', (SELECT count(*) FROM filtered),
               'facets_by_table', coalesce((SELECT jsonb_object_agg(t.table_name, t.n)
                                              FROM (SELECT table_name, count(*) AS n
                                                      FROM base GROUP BY table_name) t), '{}'::jsonb))
      INTO v_result;

    RETURN v_result;
END;
$$;

------------------------------------------------------------------------------
-- Privileges
------------------------------------------------------------------------------
REVOKE ALL ON FUNCTION public.admin_audit_jsonb(jsonb)            FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_settings_map(jsonb)           FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_audit_operation(text, jsonb)  FROM PUBLIC, anon, authenticated;

REVOKE ALL ON FUNCTION public.admin_audit_log_search(text, text, uuid, timestamp, timestamp, text, integer, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_audit_log_search(text, text, uuid, timestamp, timestamp, text, integer, integer)
    TO service_role;
