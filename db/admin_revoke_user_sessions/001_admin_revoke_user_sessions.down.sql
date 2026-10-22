-- 001_admin_revoke_user_sessions (down)
-- Removes the session-revoke function of the admin ban. Deploy the application version that does
-- not call it FIRST, or every ban answers 502 session_revoke_failed while it is missing.
-- Idempotent: IF EXISTS.

BEGIN;

DROP FUNCTION IF EXISTS public.admin_revoke_user_sessions(uuid);

COMMIT;

NOTIFY pgrst, 'reload schema';
