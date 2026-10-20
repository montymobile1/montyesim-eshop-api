/*
  020_constraints.sql  (MS-5075)

  Uniqueness the admin API relies on for its 409 codes (23505 -> duplicate_*):

  table          | key                                     | name if created here                       | 409 code
  ---------------+-----------------------------------------+--------------------------------------------+-------------------
  bundle_tag     | (bundle_id, tag_id)                     | unique_bundle_tag                          | (upsert target)
  tag            | (name, tag_group_id)                    | unique_tag_name_per_group                  | duplicate_tag_name
  app_config     | (key)                                   | app_config_key_key                         | -
  banner         | lower(title)                            | banner_title_lower_key (unique index)      | duplicate_title
  voucher        | lower(code)                             | voucher_code_lower_key (unique index)      | duplicate_code
  promotion      | (name)                                  | promotion_name_key                         | duplicate_name
  promotion_rule | (action_id, event_id, max_usage)        | promotion_rule_action_event_max_usage_key  | duplicate_rule

  Each block:
    1. reports existing duplicates and RAISES, so the whole run aborts and
       nothing is half-applied - clean the data, then re-run;
    2. creates the constraint / index only when no equivalent unique index
       exists already, checked on the column set or expression whatever its
       name (supabase_ddl.sql already has app_config_key_key,
       promotion_name_key, unique_bundle_tag, unique_tag_name_per_group,
       so on that schema the API must map 23505 by table, not only by the
       names above).

  Replaces admin repo procedures/unique_bundle_tag.sql and
  unique_tag_name_per_group.sql. Idempotent.
*/

-- Session-local helpers, dropped at the end of this file.
CREATE OR REPLACE FUNCTION pg_temp.admin_mig_has_unique_cols(p_table regclass, p_cols text[])
RETURNS boolean
LANGUAGE sql
STABLE
AS $fn$
    SELECT EXISTS (
        SELECT 1
          FROM pg_index i
         WHERE i.indrelid = p_table
           AND i.indisunique
           AND i.indpred IS NULL
           AND i.indexprs IS NULL
           AND i.indnatts = i.indnkeyatts
           AND (SELECT array_agg(a.attname::text ORDER BY a.attname::text)
                  FROM unnest(i.indkey::int2[]) AS k(attnum)
                  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum)
             = (SELECT array_agg(c ORDER BY c) FROM unnest(p_cols) AS c)
    );
$fn$;

CREATE OR REPLACE FUNCTION pg_temp.admin_mig_has_unique_expr(p_table regclass, p_index_name text, p_exprs text[])
RETURNS boolean
LANGUAGE sql
STABLE
AS $fn$
    SELECT EXISTS (
        SELECT 1
          FROM pg_index i
          JOIN pg_class ic ON ic.oid = i.indexrelid
         WHERE i.indrelid = p_table
           AND i.indisunique
           AND i.indpred IS NULL
           AND (ic.relname = p_index_name
                OR pg_get_expr(i.indexprs, i.indrelid) = ANY (p_exprs))
    );
$fn$;

------------------------------------------------------------------------------
-- bundle_tag (bundle_id, tag_id)
------------------------------------------------------------------------------
DO $$
DECLARE
    v_dups text;
BEGIN
    SELECT string_agg(format('(bundle_id=%s, tag_id=%s) x%s', d.bundle_id, d.tag_id, d.n), '; ')
      INTO v_dups
      FROM (SELECT bundle_id, tag_id, count(*) AS n
              FROM public.bundle_tag
             GROUP BY bundle_id, tag_id
            HAVING count(*) > 1
             ORDER BY count(*) DESC
             LIMIT 50) d;

    IF v_dups IS NOT NULL THEN
        RAISE EXCEPTION 'bundle_tag has duplicate (bundle_id, tag_id) rows, clean them before re-running: %', v_dups;
    END IF;

    IF NOT pg_temp.admin_mig_has_unique_cols('public.bundle_tag'::regclass, ARRAY['bundle_id', 'tag_id']) THEN
        ALTER TABLE public.bundle_tag
            ADD CONSTRAINT unique_bundle_tag UNIQUE (bundle_id, tag_id);
    END IF;
END
$$;

------------------------------------------------------------------------------
-- tag (name, tag_group_id)
------------------------------------------------------------------------------
DO $$
DECLARE
    v_dups text;
BEGIN
    SELECT string_agg(format('(name=%L, tag_group_id=%s) x%s', d.name, d.tag_group_id, d.n), '; ')
      INTO v_dups
      FROM (SELECT name, tag_group_id, count(*) AS n
              FROM public.tag
             GROUP BY name, tag_group_id
            HAVING count(*) > 1
             ORDER BY count(*) DESC
             LIMIT 50) d;

    IF v_dups IS NOT NULL THEN
        RAISE EXCEPTION 'tag has duplicate (name, tag_group_id) rows, clean them before re-running: %', v_dups;
    END IF;

    IF NOT pg_temp.admin_mig_has_unique_cols('public.tag'::regclass, ARRAY['name', 'tag_group_id']) THEN
        ALTER TABLE public.tag
            ADD CONSTRAINT unique_tag_name_per_group UNIQUE (name, tag_group_id);
    END IF;
END
$$;

------------------------------------------------------------------------------
-- app_config (key)
------------------------------------------------------------------------------
DO $$
DECLARE
    v_dups text;
BEGIN
    SELECT string_agg(format('(key=%L) x%s', d.key, d.n), '; ')
      INTO v_dups
      FROM (SELECT key, count(*) AS n
              FROM public.app_config
             GROUP BY key
            HAVING count(*) > 1
             ORDER BY count(*) DESC
             LIMIT 50) d;

    IF v_dups IS NOT NULL THEN
        RAISE EXCEPTION 'app_config has duplicate key rows, clean them before re-running: %', v_dups;
    END IF;

    IF NOT pg_temp.admin_mig_has_unique_cols('public.app_config'::regclass, ARRAY['key']) THEN
        ALTER TABLE public.app_config
            ADD CONSTRAINT app_config_key_key UNIQUE (key);
    END IF;
END
$$;

------------------------------------------------------------------------------
-- banner lower(title)   (NULL titles are allowed and never conflict)
------------------------------------------------------------------------------
DO $$
DECLARE
    v_dups text;
BEGIN
    SELECT string_agg(format('(lower(title)=%L, ids=%s)', d.t, d.ids), '; ')
      INTO v_dups
      FROM (SELECT lower(title) AS t, array_agg(id ORDER BY id)::text AS ids
              FROM public.banner
             WHERE title IS NOT NULL
             GROUP BY lower(title)
            HAVING count(*) > 1
             ORDER BY count(*) DESC
             LIMIT 50) d;

    IF v_dups IS NOT NULL THEN
        RAISE EXCEPTION 'banner has duplicate titles (case-insensitive), rename them before re-running: %', v_dups;
    END IF;

    IF NOT pg_temp.admin_mig_has_unique_expr('public.banner'::regclass, 'banner_title_lower_key',
                                             ARRAY['lower((title)::text)', 'lower(title)']) THEN
        CREATE UNIQUE INDEX banner_title_lower_key ON public.banner (lower(title));
    END IF;
END
$$;

------------------------------------------------------------------------------
-- voucher lower(code)   (the existing case-sensitive voucher_code_key stays)
------------------------------------------------------------------------------
DO $$
DECLARE
    v_dups text;
BEGIN
    SELECT string_agg(format('(lower(code)=%L, ids=%s)', d.c, d.ids), '; ')
      INTO v_dups
      FROM (SELECT lower(code) AS c, array_agg(id ORDER BY id)::text AS ids
              FROM public.voucher
             WHERE code IS NOT NULL
             GROUP BY lower(code)
            HAVING count(*) > 1
             ORDER BY count(*) DESC
             LIMIT 50) d;

    IF v_dups IS NOT NULL THEN
        RAISE EXCEPTION 'voucher has duplicate codes (case-insensitive), clean them before re-running: %', v_dups;
    END IF;

    IF NOT pg_temp.admin_mig_has_unique_expr('public.voucher'::regclass, 'voucher_code_lower_key',
                                             ARRAY['lower((code)::text)', 'lower(code)']) THEN
        CREATE UNIQUE INDEX voucher_code_lower_key ON public.voucher (lower(code));
    END IF;
END
$$;

------------------------------------------------------------------------------
-- promotion (name)
------------------------------------------------------------------------------
DO $$
DECLARE
    v_dups text;
BEGIN
    SELECT string_agg(format('(name=%L, ids=%s)', d.name, d.ids), '; ')
      INTO v_dups
      FROM (SELECT name, array_agg(id ORDER BY id)::text AS ids
              FROM public.promotion
             WHERE name IS NOT NULL
             GROUP BY name
            HAVING count(*) > 1
             ORDER BY count(*) DESC
             LIMIT 50) d;

    IF v_dups IS NOT NULL THEN
        RAISE EXCEPTION 'promotion has duplicate names, rename them before re-running: %', v_dups;
    END IF;

    IF NOT pg_temp.admin_mig_has_unique_cols('public.promotion'::regclass, ARRAY['name']) THEN
        ALTER TABLE public.promotion
            ADD CONSTRAINT promotion_name_key UNIQUE (name);
    END IF;
END
$$;

------------------------------------------------------------------------------
-- promotion_rule (promotion_rule_action_id, promotion_rule_event_id, max_usage)
------------------------------------------------------------------------------
DO $$
DECLARE
    v_dups text;
BEGIN
    SELECT string_agg(format('(action=%s, event=%s, max_usage=%s, ids=%s)',
                             d.promotion_rule_action_id, d.promotion_rule_event_id, d.max_usage, d.ids), '; ')
      INTO v_dups
      FROM (SELECT promotion_rule_action_id, promotion_rule_event_id, max_usage,
                   array_agg(id ORDER BY id)::text AS ids
              FROM public.promotion_rule
             GROUP BY promotion_rule_action_id, promotion_rule_event_id, max_usage
            HAVING count(*) > 1
             ORDER BY count(*) DESC
             LIMIT 50) d;

    IF v_dups IS NOT NULL THEN
        RAISE EXCEPTION 'promotion_rule has duplicate (action, event, max_usage) rows, merge them before re-running: %', v_dups;
    END IF;

    IF NOT pg_temp.admin_mig_has_unique_cols('public.promotion_rule'::regclass,
                                             ARRAY['promotion_rule_action_id', 'promotion_rule_event_id', 'max_usage']) THEN
        ALTER TABLE public.promotion_rule
            ADD CONSTRAINT promotion_rule_action_event_max_usage_key
            UNIQUE (promotion_rule_action_id, promotion_rule_event_id, max_usage);
    END IF;
END
$$;

DROP FUNCTION IF EXISTS pg_temp.admin_mig_has_unique_cols(regclass, text[]);
DROP FUNCTION IF EXISTS pg_temp.admin_mig_has_unique_expr(regclass, text, text[]);
