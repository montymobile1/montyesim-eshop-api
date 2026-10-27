-- 001_rls_user_tables (down)
-- Back to the state before TEOS-268: no policies, RLS off on the per-user tables.

BEGIN;

DROP POLICY IF EXISTS user_profile_select_own ON public.user_profile;
DROP POLICY IF EXISTS user_profile_bundle_select_own ON public.user_profile_bundle;
DROP POLICY IF EXISTS user_order_select_own ON public.user_order;
DROP POLICY IF EXISTS user_wallet_select_own ON public.user_wallet;
DROP POLICY IF EXISTS user_wallet_transaction_select_own ON public.user_wallet_transaction;
DROP POLICY IF EXISTS promotion_usage_select_own ON public.promotion_usage;
DROP POLICY IF EXISTS device_select_own ON public.device;
DROP POLICY IF EXISTS notification_select_own ON public.notification;
DROP POLICY IF EXISTS user_order_select_admin ON public.user_order;
DROP POLICY IF EXISTS user_wallet_select_admin ON public.user_wallet;
DROP POLICY IF EXISTS user_wallet_transaction_select_admin ON public.user_wallet_transaction;
DROP POLICY IF EXISTS promotion_usage_select_admin ON public.promotion_usage;
DROP POLICY IF EXISTS device_select_admin ON public.device;
DROP POLICY IF EXISTS users_copy_select_admin ON public.users_copy;
DROP POLICY IF EXISTS contact_us_select_admin ON public.contact_us;

ALTER TABLE public.user_profile            NO FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_profile_bundle     NO FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_order              NO FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_wallet             NO FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_wallet_transaction NO FORCE ROW LEVEL SECURITY;
ALTER TABLE public.promotion_usage         NO FORCE ROW LEVEL SECURITY;
ALTER TABLE public.device                  NO FORCE ROW LEVEL SECURITY;
ALTER TABLE public.notification            NO FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_otp                NO FORCE ROW LEVEL SECURITY;
ALTER TABLE public.users_copy              NO FORCE ROW LEVEL SECURITY;
ALTER TABLE public.contact_us              NO FORCE ROW LEVEL SECURITY;

ALTER TABLE public.user_profile            DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_profile_bundle     DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_order              DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_wallet             DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_wallet_transaction DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.promotion_usage         DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.device                  DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.notification            DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_otp                DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.users_copy              DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.contact_us              DISABLE ROW LEVEL SECURITY;

COMMIT;

NOTIFY pgrst, 'reload schema';
