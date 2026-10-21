/*
  060_admin_search_bundles.sql  (MS-5083)

  Bundle search for GET /api/v1/admin/bundles when a text search or a tag filter is given
  (without them the API uses a plain PostgREST count + range). Rewrite of the admin portal's
  search_bundles(p_search_term, p_page, p_page_size, p_tag_ids, p_is_active) under a new name;
  the legacy function is untouched until the clean-up story.

  Public function (EXECUTE: service_role only):

    admin_search_bundles(p_search text DEFAULT NULL, p_tag_ids uuid[] DEFAULT NULL,
                         p_is_active boolean DEFAULT NULL,
                         p_page integer DEFAULT 1, p_page_size integer DEFAULT 20)
      -> jsonb {"items": [bundle rows], "total_count": n,
                "facets": {"all": n, "active": n, "inactive": n}}

  Differences from the legacy search_bundles
  - p_page is 1-based (the API contract), p_page_size is clamped to 1..100;
  - the search text is matched literally: "\", "%" and "_" are escaped (admin_like_escape),
    so "50%" finds "50%" and nothing else (MS-5068);
  - the tag filter is an EXISTS on bundle_tag instead of a LEFT JOIN + DISTINCT, so a bundle
    with several matching tags is returned once and the count is exact;
  - stable order: created_at DESC, id DESC (the order of the plain list), so pages never
    repeat or skip a row (MS-5067);
  - facets: the status breakdown of the rows matching the search and the tags, whatever
    p_is_active, for the status tabs. is_active IS NOT TRUE counts as inactive (NULL included),
    so all = active + inactive;
  - items carry the list columns only: id, bundle_name, is_active, created_at, updated_at, data.

  Searched fields (as today): id::text, bundle_name, data->>'bundle_name', data->>'bundle_info_code'.

  Idempotent (CREATE OR REPLACE, REVOKE/GRANT).
*/

------------------------------------------------------------------------------
-- Internal: escape a LIKE pattern so user text is matched literally
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_like_escape(p_text text)
RETURNS text
LANGUAGE sql
IMMUTABLE
SET search_path = public, pg_temp
AS $$
    SELECT replace(replace(replace(coalesce(p_text, ''), '\', '\\'), '%', '\%'), '_', '\_');
$$;

------------------------------------------------------------------------------
-- admin_search_bundles
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_search_bundles(
    p_search    text    DEFAULT NULL,
    p_tag_ids   uuid[]  DEFAULT NULL,
    p_is_active boolean DEFAULT NULL,
    p_page      integer DEFAULT 1,
    p_page_size integer DEFAULT 20
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_search    text    := nullif(btrim(coalesce(p_search, '')), '');
    v_pattern   text;
    v_tag_ids   uuid[]  := CASE WHEN p_tag_ids IS NULL OR cardinality(p_tag_ids) = 0 THEN NULL ELSE p_tag_ids END;
    v_page      integer := greatest(coalesce(p_page, 1), 1);
    v_page_size integer := least(greatest(coalesce(p_page_size, 20), 1), 100);
    v_offset    integer;
    v_items     jsonb;
    v_all       integer;
    v_active    integer;
    v_total     integer;
BEGIN
    v_offset  := (v_page - 1) * v_page_size;
    v_pattern := CASE WHEN v_search IS NULL THEN NULL ELSE '%' || public.admin_like_escape(v_search) || '%' END;

    -- Status breakdown of the searched set (the status filter itself is not applied).
    SELECT count(*)::int,
           count(*) FILTER (WHERE b.is_active IS TRUE)::int
      INTO v_all, v_active
      FROM public.bundle b
     WHERE (v_pattern IS NULL
            OR b.id::text ILIKE v_pattern ESCAPE '\'
            OR b.bundle_name ILIKE v_pattern ESCAPE '\'
            OR (b.data ->> 'bundle_name') ILIKE v_pattern ESCAPE '\'
            OR (b.data ->> 'bundle_info_code') ILIKE v_pattern ESCAPE '\')
       AND (v_tag_ids IS NULL
            OR EXISTS (SELECT 1 FROM public.bundle_tag bt
                        WHERE bt.bundle_id = b.id AND bt.tag_id = ANY (v_tag_ids)));

    v_total := CASE
                   WHEN p_is_active IS TRUE  THEN v_active
                   WHEN p_is_active IS FALSE THEN v_all - v_active
                   ELSE v_all
               END;

    SELECT coalesce(jsonb_agg(to_jsonb(t)), '[]'::jsonb)
      INTO v_items
      FROM (
          SELECT b.id, b.bundle_name, b.is_active, b.created_at, b.updated_at, b.data
            FROM public.bundle b
           WHERE (v_pattern IS NULL
                  OR b.id::text ILIKE v_pattern ESCAPE '\'
                  OR b.bundle_name ILIKE v_pattern ESCAPE '\'
                  OR (b.data ->> 'bundle_name') ILIKE v_pattern ESCAPE '\'
                  OR (b.data ->> 'bundle_info_code') ILIKE v_pattern ESCAPE '\')
             AND (v_tag_ids IS NULL
                  OR EXISTS (SELECT 1 FROM public.bundle_tag bt
                              WHERE bt.bundle_id = b.id AND bt.tag_id = ANY (v_tag_ids)))
             AND (p_is_active IS NULL
                  OR (p_is_active IS TRUE  AND b.is_active IS TRUE)
                  OR (p_is_active IS FALSE AND b.is_active IS NOT TRUE))
           ORDER BY b.created_at DESC NULLS LAST, b.id DESC
           LIMIT v_page_size
          OFFSET v_offset
      ) t;

    RETURN jsonb_build_object(
        'items',       v_items,
        'total_count', v_total,
        'facets',      jsonb_build_object('all', v_all, 'active', v_active, 'inactive', v_all - v_active));
END;
$$;

------------------------------------------------------------------------------
-- Privileges
------------------------------------------------------------------------------
REVOKE ALL ON FUNCTION public.admin_like_escape(text) FROM PUBLIC, anon, authenticated;

REVOKE ALL ON FUNCTION public.admin_search_bundles(text, uuid[], boolean, integer, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_search_bundles(text, uuid[], boolean, integer, integer)
    TO service_role;
