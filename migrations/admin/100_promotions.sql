/*
  100_promotions.sql  (MS-5050 promotions list, MS-5054 promo analysis)

  Read side of the promotions pages for the admin API. Never writes (the writes go
  through 050_admin_audit_helpers.sql).

  Public functions (EXECUTE: service_role only):

    admin_search_promotions(p_search text DEFAULT NULL, p_status text DEFAULT 'all',
                            p_rule_ids uuid[] DEFAULT NULL, p_bundle_codes text[] DEFAULT NULL,
                            p_include_global boolean DEFAULT false,
                            p_limit integer DEFAULT 20, p_offset integer DEFAULT 0)
      -> jsonb {"data": [row], "total": n,
                "facets": {"all": n, "active": n, "inactive": n, "expired": n, "scheduled": n}}

      row = {id, name, code, type, amount, valid_from, valid_to, is_active, times_used, created_at,
             rule_id, bundle_code, bundle_codes: [text], status,
             promotion_rule: {id, name, max_usage, beneficiary,
                              promotion_rule_action: {id, name}, promotion_rule_event: {id, name}}}
      callback_url / callback_headers are never returned.

    - p_search: literal, case-insensitive match on name OR code (%, _ and \ escaped).
    - p_status: 'all' | 'active' | 'inactive' | 'expired' | 'scheduled'.
        expired   = valid_to < now (UTC)
        scheduled = not expired and valid_from > now
        active    = neither, and is_active
        inactive  = neither, and not is_active
      so the four tabs partition "all" and the counts add up.
    - p_rule_ids: promotions whose rule is one of them (empty or NULL = no filter).
    - p_bundle_codes: promotions whose stored CSV bundle_code lists any of them, matched as whole
      codes (never as a pattern); with p_include_global also the promotions with no bundle
      restriction (NULL / empty bundle_code).
    - facets count the searched set (search, rules, bundles) whatever p_status.
    - Order: created_at DESC NULLS LAST, id DESC.
    - p_limit 1..100, p_offset >= 0, else AD422.

    admin_promotion_usage(p_code text DEFAULT NULL, p_user_id uuid DEFAULT NULL,
                          p_bundle_ids uuid[] DEFAULT NULL,
                          p_from timestamp DEFAULT NULL, p_to timestamp DEFAULT NULL,
                          p_limit integer DEFAULT 20, p_offset integer DEFAULT 0)
      -> jsonb {"data": [row], "total": n,
                "totals": {"uses": n, "distinct_users": n,
                           "discount_amount": [{"currency": text|null, "amount": numeric}]}}

      row = {id, created_at, promotion_code, user_id, bundle_id, order_id, discount_amount, currency,
             status, promotion: {id, code, name} | null, user: {id, email, msisdn} | null, bundle_code}

    - Only rows with a promotion code (promotion_code IS NOT NULL), as the portal lists today.
    - p_code: literal partial match on promotion_code; p_user_id exact; p_bundle_ids any.
    - p_from inclusive, p_to EXCLUSIVE (the API passes the day after the "to" date).
    - promotion / user / bundle_code are LEFT JOINs: a deleted promotion or account gives null
      and the row is still listed and counted.
    - currency comes from the order (promotion_usage.order_id -> user_order.currency); rows
      without an order have a null currency and are summed under it.
    - totals are computed over EVERY row the filters select, never over the page.
    - Order: created_at DESC NULLS LAST, id DESC. p_limit 1..100, p_offset >= 0, else AD422.

  Internal helper (no grant to anyone but the owner):
    admin_promotions_like(text)  -> '%text%' with \, % and _ escaped (ILIKE, default escape \)

  Indexes (IF NOT EXISTS):
    promotion_usage (promotion_code, created_at DESC)   code filter + order
    promotion_usage (created_at DESC, id DESC)          default order of the usage page
    promotion (valid_from), promotion (valid_to)        expired / scheduled tabs

  Idempotent (CREATE OR REPLACE, IF NOT EXISTS, REVOKE/GRANT).
*/

------------------------------------------------------------------------------
-- Internal: literal ILIKE pattern
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_promotions_like(p_text text)
RETURNS text
LANGUAGE sql
IMMUTABLE
SET search_path = public, pg_temp
AS $$
    SELECT '%' || replace(replace(replace(p_text, '\', '\\'), '%', '\%'), '_', '\_') || '%';
$$;

------------------------------------------------------------------------------
-- admin_search_promotions
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_search_promotions(
    p_search         text    DEFAULT NULL,
    p_status         text    DEFAULT 'all',
    p_rule_ids       uuid[]  DEFAULT NULL,
    p_bundle_codes   text[]  DEFAULT NULL,
    p_include_global boolean DEFAULT false,
    p_limit          integer DEFAULT 20,
    p_offset         integer DEFAULT 0
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_now     timestamp := timezone('utc', now());
    v_pattern text;
    v_status  text := coalesce(nullif(btrim(p_status), ''), 'all');
    v_rules   uuid[] := CASE WHEN p_rule_ids IS NULL OR cardinality(p_rule_ids) = 0 THEN NULL ELSE p_rule_ids END;
    v_codes   text[];
    v_result  jsonb;
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 100 OR p_offset IS NULL OR p_offset < 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_limit must be 1..100 and p_offset >= 0',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_limit')::text;
    END IF;

    IF v_status NOT IN ('all', 'active', 'inactive', 'expired', 'scheduled') THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = format('unknown status: %s', v_status),
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_status')::text;
    END IF;

    IF nullif(btrim(coalesce(p_search, '')), '') IS NOT NULL THEN
        v_pattern := public.admin_promotions_like(btrim(p_search));
    END IF;

    SELECT array_agg(btrim(c)) INTO v_codes
      FROM unnest(coalesce(p_bundle_codes, ARRAY[]::text[])) AS c
     WHERE nullif(btrim(c), '') IS NOT NULL;

    WITH base AS (
        SELECT p.id, p.name, p.code, p.type, p.amount, p.valid_from, p.valid_to, p.is_active,
               p.times_used, p.created_at, p.rule_id, p.bundle_code,
               CASE
                   WHEN p.valid_to IS NOT NULL AND p.valid_to < v_now THEN 'expired'
                   WHEN p.valid_from IS NOT NULL AND p.valid_from > v_now THEN 'scheduled'
                   WHEN coalesce(p.is_active, true) THEN 'active'
                   ELSE 'inactive'
               END AS status
          FROM public.promotion p
         WHERE (v_pattern IS NULL OR p.name ILIKE v_pattern OR p.code ILIKE v_pattern)
           AND (v_rules IS NULL OR p.rule_id = ANY (v_rules))
           AND (v_codes IS NULL
                OR EXISTS (SELECT 1
                             FROM unnest(string_to_array(coalesce(p.bundle_code, ''), ',')) AS bc(code)
                            WHERE btrim(bc.code) = ANY (v_codes))
                OR (p_include_global AND nullif(btrim(coalesce(p.bundle_code, '')), '') IS NULL))
    ),
    page AS (
        SELECT b.*
          FROM base b
         WHERE v_status = 'all' OR b.status = v_status
         ORDER BY b.created_at DESC NULLS LAST, b.id DESC
         LIMIT p_limit OFFSET p_offset
    )
    SELECT jsonb_build_object(
        'data', coalesce((
            SELECT jsonb_agg(
                jsonb_build_object(
                    'id',           pg.id,
                    'name',         pg.name,
                    'code',         pg.code,
                    'type',         pg.type,
                    'amount',       pg.amount,
                    'valid_from',   pg.valid_from,
                    'valid_to',     pg.valid_to,
                    'is_active',    pg.is_active,
                    'times_used',   pg.times_used,
                    'created_at',   pg.created_at,
                    'rule_id',      pg.rule_id,
                    'bundle_code',  pg.bundle_code,
                    'status',       pg.status,
                    'bundle_codes', (
                        SELECT coalesce(jsonb_agg(btrim(bc.code)), '[]'::jsonb)
                          FROM unnest(string_to_array(coalesce(pg.bundle_code, ''), ',')) AS bc(code)
                         WHERE nullif(btrim(bc.code), '') IS NOT NULL
                    ),
                    'promotion_rule', (
                        SELECT jsonb_build_object(
                                   'id',          r.id,
                                   'name',        r.name,
                                   'max_usage',   r.max_usage,
                                   'beneficiary', r.beneficiary,
                                   'promotion_rule_action', (
                                       SELECT jsonb_build_object('id', a.id, 'name', a.name)
                                         FROM public.promotion_rule_action a
                                        WHERE a.id = r.promotion_rule_action_id),
                                   'promotion_rule_event', (
                                       SELECT jsonb_build_object('id', e.id, 'name', e.name)
                                         FROM public.promotion_rule_event e
                                        WHERE e.id = r.promotion_rule_event_id))
                          FROM public.promotion_rule r
                         WHERE r.id = pg.rule_id
                    )
                )
                ORDER BY pg.created_at DESC NULLS LAST, pg.id DESC)
              FROM page pg), '[]'::jsonb),
        'total', (SELECT count(*) FROM base b WHERE v_status = 'all' OR b.status = v_status),
        'facets', jsonb_build_object(
            'all',       (SELECT count(*) FROM base),
            'active',    (SELECT count(*) FROM base WHERE status = 'active'),
            'inactive',  (SELECT count(*) FROM base WHERE status = 'inactive'),
            'expired',   (SELECT count(*) FROM base WHERE status = 'expired'),
            'scheduled', (SELECT count(*) FROM base WHERE status = 'scheduled'))
    )
    INTO v_result;

    RETURN v_result;
END;
$$;

------------------------------------------------------------------------------
-- admin_promotion_usage
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_promotion_usage(
    p_code       text      DEFAULT NULL,
    p_user_id    uuid      DEFAULT NULL,
    p_bundle_ids uuid[]    DEFAULT NULL,
    p_from       timestamp DEFAULT NULL,
    p_to         timestamp DEFAULT NULL,
    p_limit      integer   DEFAULT 20,
    p_offset     integer   DEFAULT 0
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_pattern text;
    v_bundles uuid[] := CASE WHEN p_bundle_ids IS NULL OR cardinality(p_bundle_ids) = 0 THEN NULL ELSE p_bundle_ids END;
    v_result  jsonb;
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 100 OR p_offset IS NULL OR p_offset < 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_limit must be 1..100 and p_offset >= 0',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_limit')::text;
    END IF;

    IF p_from IS NOT NULL AND p_to IS NOT NULL AND p_to < p_from THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_from must be on or before p_to',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_from')::text;
    END IF;

    IF nullif(btrim(coalesce(p_code, '')), '') IS NOT NULL THEN
        v_pattern := public.admin_promotions_like(btrim(p_code));
    END IF;

    WITH base AS (
        SELECT pu.id, pu.created_at, pu.promotion_code, pu.user_id, pu.bundle_id, pu.order_id,
               pu.amount, pu.status
          FROM public.promotion_usage pu
         WHERE pu.promotion_code IS NOT NULL
           AND (v_pattern IS NULL OR pu.promotion_code ILIKE v_pattern)
           AND (p_user_id IS NULL OR pu.user_id = p_user_id)
           AND (v_bundles IS NULL OR pu.bundle_id = ANY (v_bundles))
           AND (p_from IS NULL OR pu.created_at >= p_from)
           AND (p_to IS NULL OR pu.created_at < p_to)
    ),
    page AS (
        SELECT b.*, o.currency
          FROM base b
          LEFT JOIN public.user_order o ON o.id = b.order_id
         ORDER BY b.created_at DESC NULLS LAST, b.id DESC
         LIMIT p_limit OFFSET p_offset
    ),
    by_currency AS (
        SELECT o.currency, round(sum(b.amount)::numeric, 2) AS amount
          FROM base b
          LEFT JOIN public.user_order o ON o.id = b.order_id
         GROUP BY o.currency
    )
    SELECT jsonb_build_object(
        'data', coalesce((
            SELECT jsonb_agg(
                jsonb_build_object(
                    'id',              pg.id,
                    'created_at',      pg.created_at,
                    'promotion_code',  pg.promotion_code,
                    'user_id',         pg.user_id,
                    'bundle_id',       pg.bundle_id,
                    'order_id',        pg.order_id,
                    'discount_amount', pg.amount,
                    'currency',        pg.currency,
                    'status',          pg.status,
                    'promotion', (
                        SELECT jsonb_build_object('id', p.id, 'code', p.code, 'name', p.name)
                          FROM public.promotion p
                         WHERE p.code = pg.promotion_code),
                    'user', (
                        SELECT jsonb_build_object('id', u.id, 'email', u.email,
                                                  'msisdn', u.metadata ->> 'msisdn')
                          FROM public.users_copy u
                         WHERE u.id = pg.user_id),
                    'bundle_code', (
                        SELECT bd.data ->> 'bundle_code'
                          FROM public.bundle bd
                         WHERE bd.id = pg.bundle_id)
                )
                ORDER BY pg.created_at DESC NULLS LAST, pg.id DESC)
              FROM page pg), '[]'::jsonb),
        'total', (SELECT count(*) FROM base),
        'totals', jsonb_build_object(
            'uses',           (SELECT count(*) FROM base),
            'distinct_users', (SELECT count(DISTINCT b.user_id) FROM base b WHERE b.user_id IS NOT NULL),
            'discount_amount', coalesce((
                SELECT jsonb_agg(jsonb_build_object('currency', t.currency, 'amount', t.amount)
                                 ORDER BY t.currency NULLS LAST)
                  FROM by_currency t), '[]'::jsonb))
    )
    INTO v_result;

    RETURN v_result;
END;
$$;

------------------------------------------------------------------------------
-- Indexes
------------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS promotion_usage_code_created_idx
    ON public.promotion_usage (promotion_code, created_at DESC);
CREATE INDEX IF NOT EXISTS promotion_usage_created_at_id_idx
    ON public.promotion_usage (created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS promotion_valid_from_idx
    ON public.promotion (valid_from);
CREATE INDEX IF NOT EXISTS promotion_valid_to_idx
    ON public.promotion (valid_to);

------------------------------------------------------------------------------
-- Privileges: nobody but service_role (and the owner) may execute these.
------------------------------------------------------------------------------
REVOKE ALL ON FUNCTION public.admin_promotions_like(text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_search_promotions(text, text, uuid[], text[], boolean, integer, integer)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_promotion_usage(text, uuid, uuid[], timestamp, timestamp, integer, integer)
    FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.admin_search_promotions(text, text, uuid[], text[], boolean, integer, integer)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.admin_promotion_usage(text, uuid, uuid[], timestamp, timestamp, integer, integer)
    TO service_role;
