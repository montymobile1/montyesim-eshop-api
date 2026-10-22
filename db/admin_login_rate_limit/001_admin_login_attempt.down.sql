-- 001_admin_login_attempt (down)
-- Removes the shared admin login rate-limit store. Deploy the application version that does not
-- call these functions FIRST, or every admin login fails while the functions are missing.
-- Idempotent: IF EXISTS.

BEGIN;

DROP FUNCTION IF EXISTS public.admin_login_attempt_finish(bigint, text);
DROP FUNCTION IF EXISTS public.admin_login_attempt_begin(text, text, integer, integer);
DROP TABLE IF EXISTS public.admin_login_attempt;

COMMIT;

NOTIFY pgrst, 'reload schema';
