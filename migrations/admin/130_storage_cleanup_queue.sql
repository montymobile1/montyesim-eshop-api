/*
  130_storage_cleanup_queue.sql  (MS-5075, MS-5058)

  Storage objects that MediaStorageService.remove_after_commit() could not
  delete (replaced/deleted banner images, group/tag icons). Failures are
  queued here instead of being swallowed; a retry job or an operator drains
  the queue (attempts is incremented on each retry).

  Server-only: RLS enabled with no policies, no grants to anon/authenticated
  (service_role bypasses RLS).

  Idempotent.
*/

CREATE TABLE IF NOT EXISTS public.storage_cleanup_queue
(
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bucket     text        NOT NULL,
    path       text        NOT NULL,
    reason     text,
    attempts   integer     NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.storage_cleanup_queue ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.storage_cleanup_queue FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.storage_cleanup_queue TO service_role;

DO $$
DECLARE
    v_nsp text;
    v_rel text;
BEGIN
    SELECT n.nspname, c.relname
      INTO v_nsp, v_rel
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.oid = pg_get_serial_sequence('public.storage_cleanup_queue', 'id')::regclass;

    IF v_rel IS NOT NULL THEN
        EXECUTE format('REVOKE ALL ON SEQUENCE %I.%I FROM anon, authenticated', v_nsp, v_rel);
        EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %I.%I TO service_role', v_nsp, v_rel);
    END IF;
END
$$;
