-- 001_harden_user_otp (down)
-- Restores user_otp.otp to varchar(6). Only run AFTER the previous application version (plain
-- codes) is deployed again; otherwise new sign-ins fail to insert their OTP.
--
-- 64-char hashed rows cannot fit in varchar(6), and the old code cannot verify them anyway, so
-- they are deleted first. They are short-lived sign-in codes (minutes); affected users simply
-- request a new code. Plain 6-digit rows are kept.

BEGIN;

SET LOCAL lock_timeout = '5s';

DELETE FROM public.user_otp WHERE length(otp) > 6;

ALTER TABLE public.user_otp ALTER COLUMN otp TYPE varchar(6);

COMMIT;
