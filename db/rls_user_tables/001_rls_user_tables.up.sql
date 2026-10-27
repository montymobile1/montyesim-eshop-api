-- 001_rls_user_tables (up)
-- Row level security on the per-user tables (TEOS-268): a request made as the authenticated role
-- (a user JWT against PostgREST / supabase-js) sees only the rows of that user; anon sees none.
--
-- - ENABLE + FORCE ROW LEVEL SECURITY on every table below, so the table owner is subject too.
-- - One policy per owned table, FOR SELECT TO authenticated, on its owner column. There is no
--   INSERT, UPDATE or DELETE policy: those stay API-only (the API uses the service_role key, which
--   bypasses RLS).
-- - Portal admins: the old admin portal reads user_order, user_wallet, user_wallet_transaction,
--   promotion_usage, device, users_copy and contact_us with the signed-in admin's JWT (directly and
--   through the SECURITY INVOKER wallet / referral functions). Those tables get a second SELECT
--   policy for a JWT whose app_metadata.role is admin or super_admin, the role the admin API checks.
--   app_metadata can only be written with the service key (user_metadata does not count).
--   scripts/check_admin_roles.py lists the portal accounts without that role.
-- - user_otp (keyed by email) gets no policy: authenticated and anon read nothing from it.
-- - auth.uid() is wrapped in a sub-select so it is evaluated once per statement, not per row.
--
-- The SECURITY DEFINER functions that read these tables (admin_*, the users_copy sync trigger on
-- auth.users) run as their owner. With FORCE, an owner that does not bypass RLS would see no rows:
-- the admin lists would come back empty and every sign-up would fail in the users_copy trigger.
-- So the migration first checks that the owner of every table and of every SECURITY DEFINER
-- function in public is a superuser or has BYPASSRLS, and fails otherwise.
-- It also fails when these tables carry any other policy (for example the admin portal's
-- "select policy" USING (true)): permissive policies are OR-ed, so one of them would undo the
-- lock-down. Drop or review the listed policies first, then run this again.
-- Idempotent: policies are dropped and created again.

BEGIN;

DO $$
DECLARE
    v_role     text;
    v_policies text;
BEGIN
    FOR v_role IN
        SELECT DISTINCT pg_get_userbyid(c.relowner)
          FROM pg_catalog.pg_class c
         WHERE c.relnamespace = 'public'::regnamespace
           AND c.relname IN ('user_profile', 'user_profile_bundle', 'user_order', 'user_wallet',
                             'user_wallet_transaction', 'promotion_usage', 'device', 'notification',
                             'user_otp', 'users_copy', 'contact_us')
        UNION
        SELECT DISTINCT pg_get_userbyid(p.proowner)
          FROM pg_catalog.pg_proc p
         WHERE p.pronamespace = 'public'::regnamespace AND p.prosecdef
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles
                        WHERE rolname = v_role AND (rolsuper OR rolbypassrls)) THEN
            RAISE EXCEPTION '% owns user tables or SECURITY DEFINER functions but does not bypass row level security; FORCE ROW LEVEL SECURITY would hide every row from it', v_role;
        END IF;
    END LOOP;

    SELECT string_agg(format('%I on %I (%s)', policyname, tablename, cmd), ', ' ORDER BY tablename, policyname)
      INTO v_policies
      FROM pg_catalog.pg_policies
     WHERE schemaname = 'public'
       AND tablename IN ('user_profile', 'user_profile_bundle', 'user_order', 'user_wallet',
                         'user_wallet_transaction', 'promotion_usage', 'device', 'notification',
                         'user_otp', 'users_copy', 'contact_us')
       AND policyname NOT IN ('user_profile_select_own', 'user_profile_bundle_select_own',
                              'user_order_select_own', 'user_wallet_select_own',
                              'user_wallet_transaction_select_own', 'promotion_usage_select_own',
                              'device_select_own', 'notification_select_own',
                              'user_order_select_admin', 'user_wallet_select_admin',
                              'user_wallet_transaction_select_admin', 'promotion_usage_select_admin',
                              'device_select_admin', 'users_copy_select_admin', 'contact_us_select_admin');
    IF v_policies IS NOT NULL THEN
        RAISE EXCEPTION 'other policies exist on the per-user tables and would be OR-ed with these: %. Drop or review them first.', v_policies;
    END IF;
END
$$;

ALTER TABLE public.user_profile            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_profile_bundle     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_order              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_wallet             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_wallet_transaction ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.promotion_usage         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.device                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.notification            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_otp                ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.users_copy              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.contact_us              ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.user_profile            FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_profile_bundle     FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_order              FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_wallet             FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_wallet_transaction FORCE ROW LEVEL SECURITY;
ALTER TABLE public.promotion_usage         FORCE ROW LEVEL SECURITY;
ALTER TABLE public.device                  FORCE ROW LEVEL SECURITY;
ALTER TABLE public.notification            FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_otp                FORCE ROW LEVEL SECURITY;
ALTER TABLE public.users_copy              FORCE ROW LEVEL SECURITY;
ALTER TABLE public.contact_us              FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS user_profile_select_own ON public.user_profile;
CREATE POLICY user_profile_select_own ON public.user_profile
    FOR SELECT TO authenticated
    USING (user_id = (SELECT auth.uid()));

DROP POLICY IF EXISTS user_profile_bundle_select_own ON public.user_profile_bundle;
CREATE POLICY user_profile_bundle_select_own ON public.user_profile_bundle
    FOR SELECT TO authenticated
    USING (user_id = (SELECT auth.uid()));

DROP POLICY IF EXISTS user_order_select_own ON public.user_order;
CREATE POLICY user_order_select_own ON public.user_order
    FOR SELECT TO authenticated
    USING (user_id = (SELECT auth.uid()) OR anonymous_user_id = (SELECT auth.uid()));

DROP POLICY IF EXISTS user_wallet_select_own ON public.user_wallet;
CREATE POLICY user_wallet_select_own ON public.user_wallet
    FOR SELECT TO authenticated
    USING (user_id = (SELECT auth.uid()));

DROP POLICY IF EXISTS user_wallet_transaction_select_own ON public.user_wallet_transaction;
CREATE POLICY user_wallet_transaction_select_own ON public.user_wallet_transaction
    FOR SELECT TO authenticated
    USING (wallet_id IN (SELECT w.id FROM public.user_wallet w WHERE w.user_id = (SELECT auth.uid())));

DROP POLICY IF EXISTS promotion_usage_select_own ON public.promotion_usage;
CREATE POLICY promotion_usage_select_own ON public.promotion_usage
    FOR SELECT TO authenticated
    USING (user_id = (SELECT auth.uid()));

DROP POLICY IF EXISTS device_select_own ON public.device;
CREATE POLICY device_select_own ON public.device
    FOR SELECT TO authenticated
    USING (user_id = (SELECT auth.uid()));

DROP POLICY IF EXISTS notification_select_own ON public.notification;
CREATE POLICY notification_select_own ON public.notification
    FOR SELECT TO authenticated
    USING (user_id = (SELECT auth.uid()));

-- Portal admins (app_metadata.role, written only with the service key) read every user's rows.

DROP POLICY IF EXISTS user_order_select_admin ON public.user_order;
CREATE POLICY user_order_select_admin ON public.user_order
    FOR SELECT TO authenticated
    USING (((SELECT auth.jwt()) -> 'app_metadata' ->> 'role') IN ('admin', 'super_admin'));

DROP POLICY IF EXISTS user_wallet_select_admin ON public.user_wallet;
CREATE POLICY user_wallet_select_admin ON public.user_wallet
    FOR SELECT TO authenticated
    USING (((SELECT auth.jwt()) -> 'app_metadata' ->> 'role') IN ('admin', 'super_admin'));

DROP POLICY IF EXISTS user_wallet_transaction_select_admin ON public.user_wallet_transaction;
CREATE POLICY user_wallet_transaction_select_admin ON public.user_wallet_transaction
    FOR SELECT TO authenticated
    USING (((SELECT auth.jwt()) -> 'app_metadata' ->> 'role') IN ('admin', 'super_admin'));

DROP POLICY IF EXISTS promotion_usage_select_admin ON public.promotion_usage;
CREATE POLICY promotion_usage_select_admin ON public.promotion_usage
    FOR SELECT TO authenticated
    USING (((SELECT auth.jwt()) -> 'app_metadata' ->> 'role') IN ('admin', 'super_admin'));

DROP POLICY IF EXISTS device_select_admin ON public.device;
CREATE POLICY device_select_admin ON public.device
    FOR SELECT TO authenticated
    USING (((SELECT auth.jwt()) -> 'app_metadata' ->> 'role') IN ('admin', 'super_admin'));

DROP POLICY IF EXISTS users_copy_select_admin ON public.users_copy;
CREATE POLICY users_copy_select_admin ON public.users_copy
    FOR SELECT TO authenticated
    USING (((SELECT auth.jwt()) -> 'app_metadata' ->> 'role') IN ('admin', 'super_admin'));

DROP POLICY IF EXISTS contact_us_select_admin ON public.contact_us;
CREATE POLICY contact_us_select_admin ON public.contact_us
    FOR SELECT TO authenticated
    USING (((SELECT auth.jwt()) -> 'app_metadata' ->> 'role') IN ('admin', 'super_admin'));

COMMIT;

-- PostgREST caches the schema; make the change visible right away.
NOTIFY pgrst, 'reload schema';
