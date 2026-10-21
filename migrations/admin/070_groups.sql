/*
  070_groups.sql  (MS-5085)

  Group (tag_group) writes of the admin API, each in ONE transaction with its
  audit_log row and the APP_CACHE_KEY rotation. Rewrites of the admin portal's
  insert_group_with_tags / edit_tag_group / delete_group_if_no_bundle
  (src/core/procedures/*.sql in the portal repo) under admin_* names; the
  legacy functions are left untouched until the clean-up story.

  Public functions (EXECUTE: service_role only):

    admin_insert_group_with_tags(p_name text, p_type integer, p_group_category text,
                                 p_tags jsonb, p_actor uuid, p_ip text DEFAULT NULL,
                                 p_reason text DEFAULT NULL, p_rotate_cache boolean DEFAULT true)
      -> jsonb {group, tags}
         p_tags = [{id? uuid, name, icon?, data?}, ...]; a missing id is generated.

    admin_edit_tag_group(p_id bigint, p_name text, p_type integer, p_group_category text,
                         p_new_tags jsonb, p_updated_tags jsonb, p_deleted_tag_ids uuid[],
                         p_actor uuid, p_ip text DEFAULT NULL, p_reason text DEFAULT NULL,
                         p_rotate_cache boolean DEFAULT true)
      -> jsonb {group, tags}
         p_updated_tags = [{id, name, icon, data}, ...] (icon null clears it);
         tag_translation.data is kept in sync for the updated tags, and the
         translations of the deleted tags are removed (as edit_tag_group did).

    admin_delete_group(p_id bigint, p_actor uuid, p_ip text DEFAULT NULL,
                       p_reason text DEFAULT NULL, p_rotate_cache boolean DEFAULT true)
      -> jsonb {group, tags}   (the deleted rows, so the API can remove the icons)

    admin_reorder_tags(p_group_id bigint, p_tag_ids uuid[], p_actor uuid,
                       p_ip text DEFAULT NULL, p_reason text DEFAULT NULL,
                       p_rotate_cache boolean DEFAULT true)
      -> jsonb {tag_ids}       sorting_number = position in p_tag_ids (1-based)

  Errors (SQLSTATE / MESSAGE / DETAIL is a JSON string with "code")
    AD404  not_found          group does not exist
    AD409  linked_bundles     a tag of the group (delete) or a deleted tag (edit)
                              is assigned to a bundle; DETAIL has "bundle_count".
                              Nothing is changed (fixes MS-5064).
    AD422  tag_not_in_group   an updated / deleted / reordered tag belongs to
                              another group or does not exist (DETAIL "tag_ids")
    AD422  incomplete_order   p_tag_ids does not list every tag of the group
    AD422  invalid_input      bad argument
    Native errors propagate: 23505 tag_group_name_key (duplicate_group_name),
    23505 unique_tag_name_per_group (duplicate_tag_name), 22P02 bad uuid (422).

  Audit rows (admin_write_audit of 050_admin_audit_helpers.sql): table_name
  'tag_group', operation INSERT / UPDATE / DELETE, old_data and new_data =
  {group, tags} snapshots, operation_details.record_id = the group id (bigint,
  so audit_log.record_id stays NULL), plus tags_inserted / tags_updated /
  deleted_tag_ids / reordered and cache_key_rotated.

  Requires 000_schema_drift.sql (tag.sorting_number, tag_group.group_category),
  010_audit_log.sql, 020_constraints.sql and 050_admin_audit_helpers.sql.
  Idempotent (CREATE OR REPLACE, REVOKE/GRANT).
*/

------------------------------------------------------------------------------
-- Internal: {group, tags} snapshot of a group (tags ordered as the app shows them)
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_group_snapshot(p_group_id bigint)
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = public, pg_temp
AS $$
    SELECT jsonb_build_object(
        'group', to_jsonb(g.*),
        'tags',  coalesce((SELECT jsonb_agg(to_jsonb(t.*) ORDER BY t.sorting_number, t.name, t.id)
                             FROM public.tag AS t
                            WHERE t.tag_group_id = g.id), '[]'::jsonb))
      FROM public.tag_group AS g
     WHERE g.id = p_group_id;
$$;

------------------------------------------------------------------------------
-- Internal: SELECT ... FOR UPDATE of the group, AD404 when missing
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_lock_group(p_group_id bigint)
RETURNS public.tag_group
LANGUAGE plpgsql
VOLATILE
SET search_path = public, pg_temp
AS $$
DECLARE
    v_group public.tag_group;
BEGIN
    IF p_group_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_id is required',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_id')::text;
    END IF;

    SELECT * INTO v_group FROM public.tag_group WHERE id = p_group_id FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD404',
            MESSAGE = 'not_found',
            DETAIL  = jsonb_build_object('code', 'not_found', 'table', 'tag_group', 'id', p_group_id)::text;
    END IF;

    RETURN v_group;
END;
$$;

------------------------------------------------------------------------------
-- Internal: common argument checks
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_assert_group_args(p_actor uuid, p_name text, p_type integer,
                                                          p_group_category text)
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
    IF p_name IS NULL OR btrim(p_name) = '' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_name is required',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_name')::text;
    END IF;
    IF p_type IS NULL OR p_type NOT IN (1, 2) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_type must be 1 (group) or 2 (flat)',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_type')::text;
    END IF;
    IF p_group_category IS NULL OR btrim(p_group_category) = '' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_group_category is required',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_group_category')::text;
    END IF;
END;
$$;

------------------------------------------------------------------------------
-- admin_insert_group_with_tags
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_insert_group_with_tags(
    p_name           text,
    p_type           integer,
    p_group_category text,
    p_tags           jsonb,
    p_actor          uuid,
    p_ip             text    DEFAULT NULL,
    p_reason         text    DEFAULT NULL,
    p_rotate_cache   boolean DEFAULT true
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_group    public.tag_group;
    v_tags     integer := 0;
    v_new      jsonb;
    v_rotated  boolean := false;
BEGIN
    PERFORM public.admin_assert_group_args(p_actor, p_name, p_type, p_group_category);

    IF p_tags IS NULL OR jsonb_typeof(p_tags) <> 'array' OR jsonb_array_length(p_tags) = 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_tags must be a non-empty JSON array',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_tags')::text;
    END IF;

    INSERT INTO public.tag_group (name, type, group_category)
    VALUES (btrim(p_name), p_type, btrim(p_group_category))
    RETURNING * INTO v_group;

    INSERT INTO public.tag (id, name, icon, tag_group_id, data)
    SELECT coalesce((t.value ->> 'id')::uuid, gen_random_uuid()),
           t.value ->> 'name',
           t.value ->> 'icon',
           v_group.id,
           CASE WHEN jsonb_typeof(t.value -> 'data') = 'object' THEN t.value -> 'data' END
      FROM jsonb_array_elements(p_tags) AS t(value);
    GET DIAGNOSTICS v_tags = ROW_COUNT;

    v_new := public.admin_group_snapshot(v_group.id);

    IF p_rotate_cache THEN
        v_rotated := public.admin_rotate_cache_key() IS NOT NULL;
    END IF;

    PERFORM public.admin_write_audit(
        'tag_group', 'INSERT', v_group.id::text, NULL, v_new, p_actor, p_ip, p_reason,
        jsonb_build_object('rows_affected', 1 + v_tags, 'tags_inserted', v_tags, 'cache_key_rotated', v_rotated));

    RETURN v_new;
END;
$$;

------------------------------------------------------------------------------
-- admin_edit_tag_group
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_edit_tag_group(
    p_id              bigint,
    p_name            text,
    p_type            integer,
    p_group_category  text,
    p_new_tags        jsonb,
    p_updated_tags    jsonb,
    p_deleted_tag_ids uuid[],
    p_actor           uuid,
    p_ip              text    DEFAULT NULL,
    p_reason          text    DEFAULT NULL,
    p_rotate_cache    boolean DEFAULT true
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_group        public.tag_group;
    v_old          jsonb;
    v_new          jsonb;
    v_bad          uuid[];
    v_deleted      uuid[] := coalesce(p_deleted_tag_ids, ARRAY[]::uuid[]);
    v_new_tags     jsonb  := coalesce(p_new_tags, '[]'::jsonb);
    v_updated_tags jsonb  := coalesce(p_updated_tags, '[]'::jsonb);
    v_bundle_count integer;
    v_inserted     integer := 0;
    v_updated      integer := 0;
    v_removed      integer := 0;
    v_rotated      boolean := false;
BEGIN
    PERFORM public.admin_assert_group_args(p_actor, p_name, p_type, p_group_category);

    IF jsonb_typeof(v_new_tags) <> 'array' OR jsonb_typeof(v_updated_tags) <> 'array' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_new_tags and p_updated_tags must be JSON arrays',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_new_tags')::text;
    END IF;

    v_group := public.admin_lock_group(p_id);
    v_old   := public.admin_group_snapshot(p_id);

    -- every updated or deleted tag must belong to this group
    SELECT array_agg(s.tag_id)
      INTO v_bad
      FROM (SELECT (t.value ->> 'id')::uuid AS tag_id FROM jsonb_array_elements(v_updated_tags) AS t(value)
            UNION ALL
            SELECT d.tag_id FROM unnest(v_deleted) AS d(tag_id)) AS s
     WHERE s.tag_id IS NULL
        OR NOT EXISTS (SELECT 1 FROM public.tag AS t WHERE t.id = s.tag_id AND t.tag_group_id = p_id);

    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'tag_not_in_group',
            DETAIL  = jsonb_build_object('code', 'tag_not_in_group', 'group_id', p_id, 'tag_ids', to_jsonb(v_bad))::text;
    END IF;

    -- a tag assigned to a bundle cannot be deleted (bundle_tag.tag_id has no cascade)
    SELECT count(*) INTO v_bundle_count
      FROM public.bundle_tag AS bt
     WHERE bt.tag_id = ANY (v_deleted);

    IF v_bundle_count > 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD409',
            MESSAGE = 'linked_bundles',
            DETAIL  = jsonb_build_object('code', 'linked_bundles', 'bundle_count', v_bundle_count)::text;
    END IF;

    UPDATE public.tag_group
       SET name = btrim(p_name), type = p_type, group_category = btrim(p_group_category), updated_at = now()
     WHERE id = p_id;

    IF cardinality(v_deleted) > 0 THEN
        DELETE FROM public.tag_translation WHERE tag_id = ANY (v_deleted);
        DELETE FROM public.tag WHERE id = ANY (v_deleted) AND tag_group_id = p_id;
        GET DIAGNOSTICS v_removed = ROW_COUNT;
    END IF;

    INSERT INTO public.tag (id, name, icon, tag_group_id, data)
    SELECT coalesce((t.value ->> 'id')::uuid, gen_random_uuid()),
           t.value ->> 'name',
           t.value ->> 'icon',
           p_id,
           CASE WHEN jsonb_typeof(t.value -> 'data') = 'object' THEN t.value -> 'data' END
      FROM jsonb_array_elements(v_new_tags) AS t(value);
    GET DIAGNOSTICS v_inserted = ROW_COUNT;

    UPDATE public.tag AS tg
       SET name       = t.value ->> 'name',
           icon       = t.value ->> 'icon',
           data       = CASE WHEN jsonb_typeof(t.value -> 'data') = 'object' THEN t.value -> 'data' ELSE tg.data END,
           updated_at = now()
      FROM jsonb_array_elements(v_updated_tags) AS t(value)
     WHERE tg.id = (t.value ->> 'id')::uuid
       AND tg.tag_group_id = p_id;
    GET DIAGNOSTICS v_updated = ROW_COUNT;

    -- keep every locale of tag_translation in sync (as edit_tag_group did)
    UPDATE public.tag_translation AS tt
       SET data = t.value -> 'data', updated_at = now()
      FROM jsonb_array_elements(v_updated_tags) AS t(value)
     WHERE tt.tag_id = (t.value ->> 'id')::uuid
       AND jsonb_typeof(t.value -> 'data') = 'object';

    v_new := public.admin_group_snapshot(p_id);

    IF p_rotate_cache THEN
        v_rotated := public.admin_rotate_cache_key() IS NOT NULL;
    END IF;

    PERFORM public.admin_write_audit(
        'tag_group', 'UPDATE', p_id::text, v_old, v_new, p_actor, p_ip, p_reason,
        jsonb_build_object('rows_affected', 1 + v_inserted + v_updated + v_removed,
                           'tags_inserted', v_inserted, 'tags_updated', v_updated,
                           'deleted_tag_ids', to_jsonb(v_deleted), 'cache_key_rotated', v_rotated));

    RETURN v_new;
END;
$$;

------------------------------------------------------------------------------
-- admin_delete_group
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_delete_group(
    p_id           bigint,
    p_actor        uuid,
    p_ip           text    DEFAULT NULL,
    p_reason       text    DEFAULT NULL,
    p_rotate_cache boolean DEFAULT true
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_group        public.tag_group;
    v_old          jsonb;
    v_bundle_count integer;
    v_tags         integer := 0;
    v_rotated      boolean := false;
BEGIN
    IF p_actor IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_actor is required',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_actor')::text;
    END IF;

    v_group := public.admin_lock_group(p_id);

    -- bundles reached through bundle_tag -> tag -> this group: refuse, change nothing
    SELECT count(*) INTO v_bundle_count
      FROM public.bundle_tag AS bt
      JOIN public.tag AS t ON t.id = bt.tag_id
     WHERE t.tag_group_id = p_id;

    IF v_bundle_count > 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD409',
            MESSAGE = 'linked_bundles',
            DETAIL  = jsonb_build_object('code', 'linked_bundles', 'bundle_count', v_bundle_count)::text;
    END IF;

    v_old := public.admin_group_snapshot(p_id);

    DELETE FROM public.tag_translation
     WHERE tag_id IN (SELECT id FROM public.tag WHERE tag_group_id = p_id);
    DELETE FROM public.tag WHERE tag_group_id = p_id;
    GET DIAGNOSTICS v_tags = ROW_COUNT;
    DELETE FROM public.tag_group WHERE id = p_id;

    IF p_rotate_cache THEN
        v_rotated := public.admin_rotate_cache_key() IS NOT NULL;
    END IF;

    PERFORM public.admin_write_audit(
        'tag_group', 'DELETE', p_id::text, v_old, NULL, p_actor, p_ip, p_reason,
        jsonb_build_object('rows_affected', 1 + v_tags, 'tags_deleted', v_tags, 'cache_key_rotated', v_rotated));

    RETURN v_old;
END;
$$;

------------------------------------------------------------------------------
-- admin_reorder_tags
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_reorder_tags(
    p_group_id     bigint,
    p_tag_ids      uuid[],
    p_actor        uuid,
    p_ip           text    DEFAULT NULL,
    p_reason       text    DEFAULT NULL,
    p_rotate_cache boolean DEFAULT true
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_group   public.tag_group;
    v_bad     uuid[];
    v_missing uuid[];
    v_old     jsonb;
    v_rows    integer := 0;
    v_rotated boolean := false;
BEGIN
    IF p_actor IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_actor is required',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_actor')::text;
    END IF;

    IF p_tag_ids IS NULL OR cardinality(p_tag_ids) = 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_tag_ids must list every tag of the group',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_tag_ids')::text;
    END IF;

    IF (SELECT count(DISTINCT x) FROM unnest(p_tag_ids) AS x) <> cardinality(p_tag_ids) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_tag_ids contains a repeated id',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_tag_ids')::text;
    END IF;

    v_group := public.admin_lock_group(p_group_id);

    SELECT array_agg(x) INTO v_bad
      FROM unnest(p_tag_ids) AS x
     WHERE NOT EXISTS (SELECT 1 FROM public.tag AS t WHERE t.id = x AND t.tag_group_id = p_group_id);

    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'tag_not_in_group',
            DETAIL  = jsonb_build_object('code', 'tag_not_in_group', 'group_id', p_group_id,
                                         'tag_ids', to_jsonb(v_bad))::text;
    END IF;

    SELECT array_agg(t.id) INTO v_missing
      FROM public.tag AS t
     WHERE t.tag_group_id = p_group_id
       AND t.id <> ALL (p_tag_ids);

    IF v_missing IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'incomplete_order',
            DETAIL  = jsonb_build_object('code', 'incomplete_order', 'group_id', p_group_id,
                                         'missing_tag_ids', to_jsonb(v_missing))::text;
    END IF;

    SELECT coalesce(jsonb_agg(t.id ORDER BY t.sorting_number, t.name, t.id), '[]'::jsonb)
      INTO v_old
      FROM public.tag AS t
     WHERE t.tag_group_id = p_group_id;

    UPDATE public.tag AS t
       SET sorting_number = o.n, updated_at = now()
      FROM unnest(p_tag_ids) WITH ORDINALITY AS o(id, n)
     WHERE t.id = o.id
       AND t.tag_group_id = p_group_id;
    GET DIAGNOSTICS v_rows = ROW_COUNT;

    IF p_rotate_cache THEN
        v_rotated := public.admin_rotate_cache_key() IS NOT NULL;
    END IF;

    PERFORM public.admin_write_audit(
        'tag_group', 'UPDATE', p_group_id::text,
        jsonb_build_object('tag_ids', v_old), jsonb_build_object('tag_ids', to_jsonb(p_tag_ids)),
        p_actor, p_ip, p_reason,
        jsonb_build_object('action', 'reorder_tags', 'rows_affected', v_rows, 'reordered', v_rows,
                           'cache_key_rotated', v_rotated));

    RETURN jsonb_build_object('tag_ids', to_jsonb(p_tag_ids));
END;
$$;

------------------------------------------------------------------------------
-- Privileges: service_role only
------------------------------------------------------------------------------
REVOKE ALL ON FUNCTION public.admin_group_snapshot(bigint)                           FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_lock_group(bigint)                               FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_assert_group_args(uuid, text, integer, text)     FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_insert_group_with_tags(text, integer, text, jsonb, uuid, text, text, boolean)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_edit_tag_group(bigint, text, integer, text, jsonb, jsonb, uuid[], uuid, text, text, boolean)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_delete_group(bigint, uuid, text, text, boolean)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_reorder_tags(bigint, uuid[], uuid, text, text, boolean)
    FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.admin_insert_group_with_tags(text, integer, text, jsonb, uuid, text, text, boolean)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.admin_edit_tag_group(bigint, text, integer, text, jsonb, jsonb, uuid[], uuid, text, text, boolean)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.admin_delete_group(bigint, uuid, text, text, boolean)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.admin_reorder_tags(bigint, uuid[], uuid, text, text, boolean)
    TO service_role;
