-- 001_admin_login_attempt (up)
-- Shared counter store for the admin login rate limit (TEOS-109, AUTH-026 / AUTH-027).
-- The limit was counted in memory per uvicorn worker; these objects count in the database, so
-- every worker and instance sees the same attempts.
--
-- admin_login_attempt: one row per admin login attempt that counts towards the limit. A row is
-- inserted before Supabase Auth is asked (so a burst of parallel requests is counted too) and is
-- deleted again when the attempt did not fail on the caller's side (success, or Supabase Auth
-- down). What is left are the failed attempts.
--
-- admin_login_attempt_begin: atomically (advisory locks on the IP and the email) prunes rows older
-- than the window, counts the attempts in the window for the IP and for the email, and either
-- refuses (limit reached) or inserts the attempt row and returns its id.
-- admin_login_attempt_finish: 'success' deletes the attempts of that IP + email pair (a successful
-- login resets the count, MS-5075), 'aborted' deletes only the attempt itself.
--
-- Times are the database clock, so the workers' clocks do not matter. Service role only.
-- Idempotent: IF NOT EXISTS / CREATE OR REPLACE.

BEGIN;

CREATE TABLE IF NOT EXISTS public.admin_login_attempt
(
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ip           text        NOT NULL,
    email        text        NOT NULL,
    attempted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS admin_login_attempt_ip_idx    ON public.admin_login_attempt (ip, attempted_at);
CREATE INDEX IF NOT EXISTS admin_login_attempt_email_idx ON public.admin_login_attempt (email, attempted_at);
CREATE INDEX IF NOT EXISTS admin_login_attempt_time_idx  ON public.admin_login_attempt (attempted_at);

ALTER TABLE public.admin_login_attempt ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.admin_login_attempt FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, DELETE ON TABLE public.admin_login_attempt TO service_role;

CREATE OR REPLACE FUNCTION public.admin_login_attempt_begin(
    p_ip             text,
    p_email          text,
    p_limit          integer,
    p_window_seconds integer
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_since    timestamptz;
    v_ip_count integer;
    v_em_count integer;
    v_id       bigint;
BEGIN
    IF p_ip IS NULL OR p_email IS NULL OR p_limit IS NULL OR p_limit < 1
       OR p_window_seconds IS NULL OR p_window_seconds < 1 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'admin_login_attempt_begin: invalid arguments';
    END IF;

    -- Serialise concurrent attempts on the same IP or email (fixed order: no deadlock between
    -- the two locks). Held until the end of this transaction (one PostgREST RPC = one transaction).
    PERFORM pg_advisory_xact_lock(hashtextextended('admin_login_ip:' || p_ip, 0));
    PERFORM pg_advisory_xact_lock(hashtextextended('admin_login_email:' || p_email, 0));

    v_since := clock_timestamp() - make_interval(secs => p_window_seconds);

    -- cleanup on write: rows outside the window never count again
    DELETE FROM public.admin_login_attempt WHERE attempted_at < v_since;

    SELECT count(*) INTO v_ip_count FROM public.admin_login_attempt WHERE ip = p_ip AND attempted_at >= v_since;
    SELECT count(*) INTO v_em_count FROM public.admin_login_attempt WHERE email = p_email AND attempted_at >= v_since;

    IF v_ip_count >= p_limit OR v_em_count >= p_limit THEN
        RETURN jsonb_build_object('allowed', false, 'attempt_id', NULL);
    END IF;

    INSERT INTO public.admin_login_attempt (ip, email) VALUES (p_ip, p_email) RETURNING id INTO v_id;
    RETURN jsonb_build_object('allowed', true, 'attempt_id', v_id);
END;
$$;

CREATE OR REPLACE FUNCTION public.admin_login_attempt_finish(
    p_attempt_id bigint,
    p_outcome    text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_row public.admin_login_attempt%ROWTYPE;
BEGIN
    IF p_outcome NOT IN ('success', 'aborted') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'admin_login_attempt_finish: outcome must be success or aborted';
    END IF;
    SELECT * INTO v_row FROM public.admin_login_attempt WHERE id = p_attempt_id;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    IF p_outcome = 'success' THEN
        DELETE FROM public.admin_login_attempt WHERE ip = v_row.ip AND email = v_row.email;
    ELSE
        DELETE FROM public.admin_login_attempt WHERE id = p_attempt_id;
    END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.admin_login_attempt_begin(text, text, integer, integer) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_login_attempt_finish(bigint, text)                FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_login_attempt_begin(text, text, integer, integer) TO service_role;
GRANT EXECUTE ON FUNCTION public.admin_login_attempt_finish(bigint, text)                TO service_role;

COMMIT;

-- PostgREST caches the schema; make the new functions callable right away.
NOTIFY pgrst, 'reload schema';
