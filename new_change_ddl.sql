-- ============================================================================
-- new_change_ddl.sql
-- Only the SQL objects changed by the TEOS-102 QA bug fixes (2026-10-26).
-- Each object here is a full copy of the same object in supabase_ddl.sql
-- (ADMIN API (MS-5017) section): run this file to apply just the changes.
-- Every statement is idempotent (CREATE OR REPLACE / IF NOT EXISTS).
-- ============================================================================


-- TEOS-168: PUT /bundles/{id}/tags refuses any tag whose group is inactive, already-linked tags included (422 inactive_group_tag)
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
    v_inactive uuid[];
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

    -- TEOS-168: a bundle cannot be saved with any tag whose group is inactive, tags it already
    -- has included: the admin removes those tags first. Refused before any write.
    SELECT array_agg(t ORDER BY t)
      INTO v_inactive
      FROM unnest(v_tag_ids) AS t
      JOIN public.tag AS g ON g.id = t
      JOIN public.tag_group AS tg ON tg.id = g.tag_group_id
     WHERE tg.is_active IS FALSE;

    IF v_inactive IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = format('tag(s) of an inactive group: %s', array_to_string(v_inactive, ', ')),
            DETAIL  = jsonb_build_object('code', 'inactive_group_tag', 'tag_ids', to_jsonb(v_inactive))::text;
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

REVOKE ALL ON FUNCTION public.admin_set_bundle_tags(uuid, uuid[], uuid, text, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_set_bundle_tags(uuid, uuid[], uuid, text, text)
    TO service_role;

-- TEOS-196, TEOS-176: group edit 409 linked_bundles counts distinct bundles and names the linked tags (tag_ids, tags)
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
    v_linked_ids   jsonb;
    v_linked       jsonb;
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

    -- a tag assigned to a bundle cannot be deleted (bundle_tag.tag_id has no cascade).
    -- bundle_count = distinct bundles (TEOS-176); the 409 also names the linked tags (TEOS-196).
    SELECT count(DISTINCT bt.bundle_id) INTO v_bundle_count
      FROM public.bundle_tag AS bt
     WHERE bt.tag_id = ANY (v_deleted);

    IF v_bundle_count > 0 THEN
        SELECT jsonb_agg(l.id::text ORDER BY l.name, l.id),
               jsonb_agg(jsonb_build_object('id', l.id::text, 'name', l.name, 'bundle_count', l.bundle_count)
                         ORDER BY l.name, l.id)
          INTO v_linked_ids, v_linked
          FROM (SELECT t.id, t.name, count(DISTINCT bt.bundle_id)::int AS bundle_count
                  FROM public.tag AS t
                  JOIN public.bundle_tag AS bt ON bt.tag_id = t.id
                 WHERE t.id = ANY (v_deleted)
                 GROUP BY t.id, t.name) AS l;

        RAISE EXCEPTION USING
            ERRCODE = 'AD409',
            MESSAGE = 'linked_bundles',
            DETAIL  = jsonb_build_object('code', 'linked_bundles', 'bundle_count', v_bundle_count,
                                         'tag_ids', coalesce(v_linked_ids, '[]'::jsonb),
                                         'tags', coalesce(v_linked, '[]'::jsonb))::text;
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

REVOKE ALL ON FUNCTION public.admin_edit_tag_group(bigint, text, integer, text, jsonb, jsonb, uuid[], uuid, text, text, boolean)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_edit_tag_group(bigint, text, integer, text, jsonb, jsonb, uuid[], uuid, text, text, boolean)
    TO service_role;

-- TEOS-176: group delete 409 linked_bundles bundle_count = count(DISTINCT bundle_id)
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

    -- bundles reached through bundle_tag -> tag -> this group: refuse, change nothing.
    -- A bundle carrying several tags of the group counts once (TEOS-176).
    SELECT count(DISTINCT bt.bundle_id) INTO v_bundle_count
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

REVOKE ALL ON FUNCTION public.admin_delete_group(bigint, uuid, text, text, boolean)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_delete_group(bigint, uuid, text, text, boolean)
    TO service_role;

-- TEOS-197, TEOS-198: tag reorder is a no-op (no audit, no cache rotation) when the order is already stored; an empty list is accepted for a group without tags
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

    -- an empty array is accepted (TEOS-198): fine for a group without tags, incomplete_order otherwise
    IF p_tag_ids IS NULL THEN
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

    -- TEOS-197 / TEOS-198: every tag already has sorting_number = its position in p_tag_ids
    -- (an empty group sent an empty list included): no write, no audit row, no cache rotation.
    -- Comparing the stored numbers, not only the order, still writes a same order stored as 3, 7, 9.
    IF NOT EXISTS (SELECT 1
                     FROM unnest(p_tag_ids) WITH ORDINALITY AS o(id, n)
                     JOIN public.tag AS t ON t.id = o.id AND t.tag_group_id = p_group_id
                    WHERE t.sorting_number IS DISTINCT FROM o.n::integer) THEN
        RETURN jsonb_build_object('tag_ids', to_jsonb(p_tag_ids));
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

REVOKE ALL ON FUNCTION public.admin_reorder_tags(bigint, uuid[], uuid, text, text, boolean)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_reorder_tags(bigint, uuid[], uuid, text, text, boolean)
    TO service_role;

-- TEOS-173: bundle_count (distinct bundles) of each group on GET /groups and GET /groups/{id}; index bundle_tag(tag_id)
CREATE INDEX IF NOT EXISTS bundle_tag_tag_id_idx ON public.bundle_tag (tag_id);

CREATE OR REPLACE FUNCTION public.admin_group_bundle_counts(p_group_ids bigint[])
RETURNS TABLE (group_id bigint, bundle_count integer)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT t.tag_group_id, count(DISTINCT bt.bundle_id)::integer
      FROM public.tag AS t
      JOIN public.bundle_tag AS bt ON bt.tag_id = t.id
     WHERE t.tag_group_id = ANY (coalesce(p_group_ids, ARRAY[]::bigint[]))
     GROUP BY t.tag_group_id;
$$;

REVOKE ALL ON FUNCTION public.admin_group_bundle_counts(bigint[])
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_group_bundle_counts(bigint[])
    TO service_role;
