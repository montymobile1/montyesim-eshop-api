/*
  090_wallet_referrals.sql  (MS-5056 referrals list and export)

  Read side of the referral transactions for the admin API. Never writes.

  NOTE: the migration plan lists admin_wallet_transactions in this file too. It is written by
  the Users story (MS-5079) in 080_users.sql, next to admin_search_users, because the wallet
  tab belongs to the user detail page. This file holds the referral function only.

  Public function (EXECUTE: service_role only):

    admin_referral_transactions(p_user_id uuid DEFAULT NULL, p_search text DEFAULT NULL,
                                p_from_email text DEFAULT NULL, p_date date DEFAULT NULL,
                                p_limit integer DEFAULT 20, p_offset integer DEFAULT 0)
      -> jsonb {"data": [row], "total": n}

    Rewrite of get_referral_transactions_with_count / export_referrals_data (supabase_ddl.sql,
    the versions with p_from_email and p_referral_date), under a new name. Same row set:
    successful orders paid with a referral code, joined to their promotion_usage row
    (promotion_usage.order_id = user_order.id, column added by 000_schema_drift.sql).

    Row:
      id              user_order.id (the order number shown in the portal)
      created_at      user_order.created_at (the referral date)
      payment_time    user_order.payment_time (the purchase date)
      referral_code   user_order.referral_code
      currency        user_order.currency
      modified_amount user_order.modified_amount
      order_status    user_order.order_status (always 'success' here)
      amount          promotion_usage.amount (the reward)
      status          promotion_usage.status (pending | completed | failed)
      referrer        {id, email}: promotion_usage.user_id, the owner of the code who receives
                      the reward (legacy column "To User Email"); email from users_copy
      referred        {id, email}: user_order.user_id, the user who bought with the code (legacy
                      column "From User Email"); email = promotion_usage.referred_to, else
                      users_copy

    Filters (all optional, combined with AND), same meaning as the legacy functions:
      p_user_id     user_order.user_id (the referred user; what the user detail tab shows today)
      p_search      user_order.referral_code contains the text (case-insensitive, literal:
                    %, _ and \ are escaped)
      p_from_email  promotion_usage.referred_to contains the text (case-insensitive, literal)
      p_date        user_order.created_at on that calendar day
    Order: created_at DESC, id DESC (stable paging).
    p_limit 1..1000 (the API lists at most 100 and exports in chunks of 1000), p_offset >= 0,
    else AD422.

    Difference from the legacy functions: users_copy is LEFT JOINed (a referrer deleted from
    users_copy no longer hides the order; its email is null). Blank search texts are ignored.

  Indexes: the join and the default order (newest referral orders first).

  Idempotent (CREATE OR REPLACE, CREATE INDEX IF NOT EXISTS, REVOKE/GRANT).
*/

------------------------------------------------------------------------------
-- Indexes
------------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS admin_promotion_usage_order_id_idx
    ON public.promotion_usage (order_id);

CREATE INDEX IF NOT EXISTS admin_user_order_referral_idx
    ON public.user_order (created_at DESC, id DESC)
    WHERE referral_code IS NOT NULL AND order_status = 'success';

------------------------------------------------------------------------------
-- admin_referral_transactions
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.admin_referral_transactions(
    p_user_id    uuid    DEFAULT NULL,
    p_search     text    DEFAULT NULL,
    p_from_email text    DEFAULT NULL,
    p_date       date    DEFAULT NULL,
    p_limit      integer DEFAULT 20,
    p_offset     integer DEFAULT 0
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_search text;
    v_from   text;
    v_result jsonb;
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 OR p_offset IS NULL OR p_offset < 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'AD422',
            MESSAGE = 'p_limit must be 1..1000 and p_offset >= 0',
            DETAIL  = jsonb_build_object('code', 'invalid_input', 'param', 'p_limit')::text;
    END IF;

    -- literal "contains" patterns: \, % and _ in the text are escaped (ESCAPE '\' below)
    IF nullif(btrim(p_search), '') IS NOT NULL THEN
        v_search := '%' || replace(replace(replace(btrim(p_search), '\', '\\'), '%', '\%'), '_', '\_') || '%';
    END IF;
    IF nullif(btrim(p_from_email), '') IS NOT NULL THEN
        v_from := '%' || replace(replace(replace(btrim(p_from_email), '\', '\\'), '%', '\%'), '_', '\_') || '%';
    END IF;

    WITH filtered AS (
        SELECT o.id,
               o.created_at,
               o.payment_time,
               o.user_id          AS referred_id,
               o.referral_code,
               o.currency,
               o.modified_amount,
               o.order_status,
               pu.user_id         AS referrer_id,
               pu.amount,
               pu.status,
               pu.referred_to
          FROM public.user_order o
          JOIN public.promotion_usage pu ON pu.order_id = o.id
         WHERE o.referral_code IS NOT NULL
           AND o.order_status = 'success'
           AND (p_user_id IS NULL OR o.user_id = p_user_id)
           AND (v_search IS NULL OR o.referral_code ILIKE v_search ESCAPE '\')
           AND (v_from IS NULL OR pu.referred_to ILIKE v_from ESCAPE '\')
           AND (p_date IS NULL OR o.created_at::date = p_date)
    ),
    page AS (
        SELECT f.*,
               ur.email AS referrer_email,
               ud.email AS referred_email
          FROM filtered f
          LEFT JOIN public.users_copy ur ON ur.id = f.referrer_id
          LEFT JOIN public.users_copy ud ON ud.id = f.referred_id
         ORDER BY f.created_at DESC, f.id DESC
         LIMIT p_limit OFFSET p_offset
    )
    SELECT jsonb_build_object(
               'data', coalesce((SELECT jsonb_agg(jsonb_build_object(
                                            'id',              p.id,
                                            'created_at',      p.created_at,
                                            'payment_time',    p.payment_time,
                                            'referral_code',   p.referral_code,
                                            'currency',        p.currency,
                                            'modified_amount', p.modified_amount,
                                            'order_status',    p.order_status,
                                            'amount',          p.amount,
                                            'status',          p.status,
                                            'referrer',        CASE WHEN p.referrer_id IS NULL THEN NULL
                                                                    ELSE jsonb_build_object('id', p.referrer_id,
                                                                                            'email', p.referrer_email) END,
                                            'referred',        CASE WHEN p.referred_id IS NULL THEN NULL
                                                                    ELSE jsonb_build_object('id', p.referred_id,
                                                                                            'email', coalesce(p.referred_to, p.referred_email)) END
                                        ) ORDER BY p.created_at DESC, p.id DESC)
                                   FROM page p), '[]'::jsonb),
               'total', (SELECT count(*) FROM filtered))
      INTO v_result;

    RETURN v_result;
END;
$$;

------------------------------------------------------------------------------
-- Privileges
------------------------------------------------------------------------------
REVOKE ALL ON FUNCTION public.admin_referral_transactions(uuid, text, text, date, integer, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_referral_transactions(uuid, text, text, date, integer, integer)
    TO service_role;
