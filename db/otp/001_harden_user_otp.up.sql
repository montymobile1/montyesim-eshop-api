-- 001_harden_user_otp (up)
-- user_otp.otp now stores HMAC-SHA256(OTP_HMAC_SECRET, code) as 64 hex chars instead of the
-- plain 6-digit code. Run BEFORE deploying the application that writes hashes.
--
-- Safe on a live database with existing rows: widening a varchar is a catalog-only change in
-- PostgreSQL (no table rewrite, no data change). It still takes a brief ACCESS EXCLUSIVE lock,
-- so lock_timeout makes it give up instead of queueing behind a long transaction; just re-run.
-- Idempotent: running it again on varchar(64) is a no-op.

BEGIN;

SET LOCAL lock_timeout = '5s';

ALTER TABLE public.user_otp ALTER COLUMN otp TYPE varchar(64);

COMMIT;
