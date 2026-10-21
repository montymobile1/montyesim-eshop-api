/*
  061_bundles_admin.sql  (MS-5083)

  Audited bundle writes for the admin API. Each function runs in ONE transaction (a PostgREST
  RPC is one transaction): the change, the APP_CACHE_KEY rotation (admin_rotate_cache_key, 050)
  and the audit_log row (admin_write_audit, 050) commit or roll back together. The portal used to
  do the same in three browser calls (update, audit insert, cache update), none of them atomic.

  Public functions (EXECUTE: service_role only):

    admin_set_bundle_status(p_id uuid, p_is_active boolean, p_actor uuid,
                            p_ip text DEFAULT NULL, p_reason text DEFAULT NULL)
      -> jsonb {"id", "is_active"}
         PATCH /bundles/{id}/status. Locks the row (FOR UPDATE): two admins toggling the same
         bundle get one success and one AD409 no_change.
         Audit: table 'bundle', operation 'UPDATE', record_id = the bundle id,
         old_data / new_data = {"affected_bundle_ids": [id], "status": "active"|"inactive"}
         (the shape the portal wrote for every bundle status change, so the audit log reads the
         same for a single toggle and a bulk action), operation_details = {rows_affected: 1,
         cache_key_rotated, changed_columns: ["is_active"]}.

    admin_set_all_bundles_status(p_is_active boolean, p_actor uuid,
                                 p_ip text DEFAULT NULL, p_reason text DEFAULT NULL)
      -> jsonb {"rows_affected", "affected_bundle_ids": [uuid...]}
         POST /bundles/status/bulk. Replaces activate_all_bundles() and
         deactivate_all_bundles(p_reason) (untouched until the clean-up story). Only the rows
         that change are updated (is_active IS NOT TRUE to activate, IS TRUE to deactivate), so
         already-matching bundles keep their updated_at. One audit row, operation 'BULK',
         record_id NULL, old_data / new_data in the legacy shape above (all affected ids).

    admin_set_bundle_tags(p_bundle_id uuid, p_tag_ids uuid[], p_actor uuid,
                          p_ip text DEFAULT NULL, p_reason text DEFAULT NULL)
      -> jsonb {"id", "tag_ids": [uuid...], "added": n, "removed": n, "cache_key_rotated": bool}
         PUT /bundles/{id}/tags. Replaces the bundle's tag set exactly:
           DELETE ... WHERE tag_id <> ALL (p_tag_ids)   -- an empty array removes every tag (MS-5065)
           INSERT ... ON CONFLICT (bundle_id, tag_id) DO NOTHING
         Duplicate ids in p_tag_ids are ignored. When the set is already the requested one,
         nothing is written, nothing is audited and the cache key is not rotated.
         Audit: table 'bundle', operation 'UPDATE', record_id = the bundle id,
         old_data / new_data = {"tag_ids": [...]}, operation_details = {rows_affected, added,
         removed, cache_key_rotated, changed_columns: ["tags"]}.

  Errors (SQLSTATE / MESSAGE / DETAIL is a JSON string with "code")
    AD404  not_found      unknown bundle id
    AD409  no_change      the bundle already has that state / no bundle would change
    AD422  invalid_input  p_actor missing; a reason is required to deactivate (backstop of the
                          API rule); p_tag_ids NULL
    AD422  unknown_tag    a tag id does not exist (DETAIL has "tag_ids")

  Idempotent (CREATE OR REPLACE, REVOKE/GRANT). Depends on 050_admin_audit_helpers.sql and on
  the unique key bundle_tag(bundle_id, tag_id) (unique_bundle_tag in supabase_ddl.sql, also
  ensured by 020_constraints.sql).
*/

------------------------------------------------------------------------------
-- Internal: shared checks
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_bundles_assert_actor_reason(
    p_actor     uuid,
    p_is_active boolean,
    p_reason    text
)
RETURNS void
LANGUAGE plpgsql
IMMUTABLE
SET search_path = public, pg_temp
AS $$
BEGIN
    IF p_actor IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_actor is required',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_actor')::text;
    END IF;

    IF p_is_active IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_is_active is required',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_is_active')::text;
    END IF;

    IF p_is_active IS FALSE AND nullif(btrim(coalesce(p_reason, '')), '') IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'a reason is required to deactivate',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_reason')::text;
    END IF;
END;
$$;

------------------------------------------------------------------------------
-- admin_set_bundle_status
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_set_bundle_status(
    p_id        uuid,
    p_is_active boolean,
    p_actor     uuid,
    p_ip        text DEFAULT NULL,
    p_reason    text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_current boolean;
    v_found   boolean;
    v_rotated boolean := false;
    v_old     text;
    v_new     text;
BEGIN
    PERFORM public.admin_bundles_assert_actor_reason(p_actor, p_is_active, p_reason);

    IF p_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD404',
            MESSAGE = 'not_found',
            DETAIL  = jsonb_build_object('code', 'not_found', 'table', 'bundle')::text;
    END IF;

    SELECT b.is_active INTO v_current
      FROM public.bundle b
     WHERE b.id = p_id
       FOR UPDATE;
    v_found := FOUND;

    IF NOT v_found THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD404',
            MESSAGE = 'not_found',
            DETAIL  = jsonb_build_object('code', 'not_found', 'table', 'bundle', 'id', p_id::text)::text;
    END IF;

    -- NULL counts as inactive, as everywhere else in the bundles admin.
    IF coalesce(v_current, false) = p_is_active THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD409',
            MESSAGE = 'no_change',
            DETAIL  = jsonb_build_object('code', 'no_change', 'table', 'bundle', 'id', p_id::text)::text;
    END IF;

    UPDATE public.bundle
       SET is_active = p_is_active,
           updated_at = now()
     WHERE id = p_id;

    v_rotated := public.admin_rotate_cache_key() IS NOT NULL;

    v_old := CASE WHEN p_is_active THEN 'inactive' ELSE 'active' END;
    v_new := CASE WHEN p_is_active THEN 'active' ELSE 'inactive' END;

    PERFORM public.admin_write_audit(
        'bundle', 'UPDATE', p_id::text,
        jsonb_build_object('affected_bundle_ids', jsonb_build_array(p_id::text), 'status', v_old),
        jsonb_build_object('affected_bundle_ids', jsonb_build_array(p_id::text), 'status', v_new),
        p_actor, p_ip, p_reason,
        jsonb_build_object('rows_affected', 1,
                           'cache_key_rotated', v_rotated,
                           'changed_columns', jsonb_build_array('is_active')));

    RETURN jsonb_build_object('id', p_id::text, 'is_active', p_is_active);
END;
$$;

------------------------------------------------------------------------------
-- admin_set_all_bundles_status
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_set_all_bundles_status(
    p_is_active boolean,
    p_actor     uuid,
    p_ip        text DEFAULT NULL,
    p_reason    text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_ids     jsonb;
    v_count   integer;
    v_rotated boolean := false;
    v_old     text;
    v_new     text;
BEGIN
    PERFORM public.admin_bundles_assert_actor_reason(p_actor, p_is_active, p_reason);

    -- Only the rows that change: IS NOT TRUE also picks up bundles whose is_active was never set.
    WITH updated AS (
        UPDATE public.bundle
           SET is_active = p_is_active,
               updated_at = now()
         WHERE (p_is_active IS TRUE  AND is_active IS NOT TRUE)
            OR (p_is_active IS FALSE AND is_active IS TRUE)
        RETURNING id
    )
    SELECT coalesce(jsonb_agg(id::text ORDER BY id), '[]'::jsonb), count(*)::int
      INTO v_ids, v_count
      FROM updated;

    IF v_count = 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD409',
            MESSAGE = 'no_change',
            DETAIL  = jsonb_build_object('code', 'no_change', 'table', 'bundle')::text;
    END IF;

    v_rotated := public.admin_rotate_cache_key() IS NOT NULL;

    v_old := CASE WHEN p_is_active THEN 'inactive' ELSE 'active' END;
    v_new := CASE WHEN p_is_active THEN 'active' ELSE 'inactive' END;

    PERFORM public.admin_write_audit(
        'bundle', 'BULK', NULL,
        jsonb_build_object('affected_bundle_ids', v_ids, 'status', v_old),
        jsonb_build_object('affected_bundle_ids', v_ids, 'status', v_new),
        p_actor, p_ip, p_reason,
        jsonb_build_object('rows_affected', v_count,
                           'cache_key_rotated', v_rotated,
                           'changed_columns', jsonb_build_array('is_active')));

    RETURN jsonb_build_object('rows_affected', v_count, 'affected_bundle_ids', v_ids);
END;
$$;

------------------------------------------------------------------------------
-- admin_set_bundle_tags
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_set_bundle_tags(
    p_bundle_id uuid,
    p_tag_ids   uuid[],
    p_actor     uuid,
    p_ip        text DEFAULT NULL,
    p_reason    text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_tag_ids  uuid[];
    v_missing  uuid[];
    v_old_ids  jsonb;
    v_new_ids  jsonb;
    v_removed  integer := 0;
    v_added    integer := 0;
    v_rotated  boolean := false;
BEGIN
    IF p_actor IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_actor is required',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_actor')::text;
    END IF;

    IF p_tag_ids IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_tag_ids is required (an empty array removes every tag)',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_tag_ids')::text;
    END IF;

    -- unique ids, NULLs dropped
    SELECT coalesce(array_agg(DISTINCT t), ARRAY[]::uuid[])
      INTO v_tag_ids
      FROM unnest(p_tag_ids) AS t
     WHERE t IS NOT NULL;

    -- lock the bundle: concurrent saves on the same bundle serialise (AD404 when missing)
    PERFORM 1 FROM public.bundle b WHERE b.id = p_bundle_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD404',
            MESSAGE = 'not_found',
            DETAIL  = jsonb_build_object('code', 'not_found', 'table', 'bundle',
                                         'id', coalesce(p_bundle_id::text, '<null>'))::text;
    END IF;

    SELECT array_agg(t ORDER BY t)
      INTO v_missing
      FROM unnest(v_tag_ids) AS t
     WHERE NOT EXISTS (SELECT 1 FROM public.tag g WHERE g.id = t);

    IF v_missing IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = format('unknown tag id(s): %s', array_to_string(v_missing, ', ')),
            DETAIL  = jsonb_build_object('code', 'unknown_tag', 'tag_ids', to_jsonb(v_missing))::text;
    END IF;

    SELECT coalesce(jsonb_agg(bt.tag_id::text ORDER BY bt.tag_id), '[]'::jsonb)
      INTO v_old_ids
      FROM public.bundle_tag bt
     WHERE bt.bundle_id = p_bundle_id;

    -- tag_id <> ALL('{}') is true for every row: an empty set clears the bundle (MS-5065)
    WITH removed AS (
        DELETE FROM public.bundle_tag bt
         WHERE bt.bundle_id = p_bundle_id
           AND bt.tag_id <> ALL (v_tag_ids)
        RETURNING bt.id
    )
    SELECT count(*)::int INTO v_removed FROM removed;

    WITH added AS (
        INSERT INTO public.bundle_tag (bundle_id, tag_id, is_active)
        SELECT p_bundle_id, t, true
          FROM unnest(v_tag_ids) AS t
        ON CONFLICT (bundle_id, tag_id) DO NOTHING
        RETURNING id
    )
    SELECT count(*)::int INTO v_added FROM added;

    SELECT coalesce(jsonb_agg(bt.tag_id::text ORDER BY bt.tag_id), '[]'::jsonb)
      INTO v_new_ids
      FROM public.bundle_tag bt
     WHERE bt.bundle_id = p_bundle_id;

    IF v_removed = 0 AND v_added = 0 THEN
        -- same set as before: nothing to audit, nothing to invalidate
        RETURN jsonb_build_object('id', p_bundle_id::text, 'tag_ids', v_new_ids,
                                  'added', 0, 'removed', 0, 'cache_key_rotated', false);
    END IF;

    v_rotated := public.admin_rotate_cache_key() IS NOT NULL;

    PERFORM public.admin_write_audit(
        'bundle', 'UPDATE', p_bundle_id::text,
        jsonb_build_object('tag_ids', v_old_ids),
        jsonb_build_object('tag_ids', v_new_ids),
        p_actor, p_ip, p_reason,
        jsonb_build_object('rows_affected', v_removed + v_added,
                           'added', v_added,
                           'removed', v_removed,
                           'cache_key_rotated', v_rotated,
                           'changed_columns', jsonb_build_array('tags')));

    RETURN jsonb_build_object('id', p_bundle_id::text, 'tag_ids', v_new_ids,
                              'added', v_added, 'removed', v_removed, 'cache_key_rotated', v_rotated);
END;
$$;

------------------------------------------------------------------------------
-- Privileges
------------------------------------------------------------------------------
REVOKE ALL ON FUNCTION public.admin_bundles_assert_actor_reason(uuid, boolean, text)
    FROM PUBLIC, anon, authenticated;

REVOKE ALL ON FUNCTION public.admin_set_bundle_status(uuid, boolean, uuid, text, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_set_bundle_status(uuid, boolean, uuid, text, text)
    TO service_role;

REVOKE ALL ON FUNCTION public.admin_set_all_bundles_status(boolean, uuid, text, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_set_all_bundles_status(boolean, uuid, text, text)
    TO service_role;

REVOKE ALL ON FUNCTION public.admin_set_bundle_tags(uuid, uuid[], uuid, text, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_set_bundle_tags(uuid, uuid[], uuid, text, text)
    TO service_role;
