-- =====================================================================================
-- MCP purchase idempotency (Phase 4A)
--
-- Adds durable, database-backed idempotency for the MCP wallet purchase endpoint
-- POST /api/v1/mcp/user/bundle/assign.
--
-- This project has no migration framework: apply this file manually, once per
-- environment, BEFORE setting MCP_PURCHASE_ENABLED=true. It is intentionally
-- additive - it creates one new table and one new function and touches no
-- existing table, so the legacy purchase flow cannot be affected.
--
--   psql "$DATABASE_URL" -f migrations/20260731_0001_mcp_purchase_idempotency.sql
--   (or paste it into the Supabase SQL editor)
--
-- -------------------------------------------------------------------------------------
-- RE-RUN BEHAVIOUR - READ BEFORE RUNNING THIS A SECOND TIME
-- -------------------------------------------------------------------------------------
-- Re-running this file is safe in the sense that it will not error and will not
-- destroy data. It is NOT a substitute for an upgrade:
--
--   * CREATE TABLE IF NOT EXISTS is a NO-OP when the table already exists. It does
--     NOT add columns, does NOT alter types, does NOT add or change constraints and
--     does NOT reconcile an older table definition with this file. If an earlier
--     version of this migration was already applied, the table keeps its OLD shape
--     and this file will silently appear to have succeeded.
--   * CREATE INDEX IF NOT EXISTS is likewise a no-op per index name; it does not
--     alter an existing index that has the same name but different columns.
--   * CREATE OR REPLACE FUNCTION *does* upgrade the function body in place.
--
-- Net effect on an environment that already ran an earlier version: the claim
-- FUNCTION is upgraded but the TABLE is not, which is exactly the combination that
-- breaks (the new function reads columns the old table lacks).
--
-- => Fresh environment: run this file, nothing else.
-- => Environment that already ran an earlier version: run the explicit upgrade
--    block in section "UPGRADE FROM AN EARLIER APPLICATION" at the foot of this
--    file FIRST, then re-run this file. Verify with the queries in section
--    "VERIFICATION".
--
-- At the time of writing this migration has not been applied to any environment,
-- so the fresh path is the expected one.
-- =====================================================================================

create table if not exists public.mcp_purchase_idempotency
(
    id                   uuid                     default gen_random_uuid() not null
        constraint mcp_purchase_idempotency_pkey primary key,
    user_id              uuid                                               not null
        constraint mcp_purchase_idempotency_user_id_fkey references auth.users (id),
    operation            varchar(64)                                        not null,
    -- Keyed digest of the caller's Idempotency-Key. The raw key is never stored.
    idempotency_key_hash varchar(128)                                       not null,
    -- SHA-256 of the canonical request (user, operation, bundle, payment type, search).
    request_hash         varchar(128)                                       not null,
    status               varchar(32)              default 'PROCESSING'      not null,
    order_id             uuid
        constraint mcp_purchase_idempotency_order_id_fkey references public.user_order (id),
    response_code        integer,
    response_body        jsonb,
    error_code           varchar(128),
    -- True as soon as anything irreversible happened for this key: an order row was
    -- created, the wallet was debited, or provisioning was called. Only a record with
    -- NO side effect may ever be re-executed under the same key.
    has_side_effects     boolean                  default false             not null,
    created_at           timestamp with time zone default now()             not null,
    updated_at           timestamp with time zone default now()             not null,
    -- RETENTION HINT ONLY. This does NOT expire, recycle or unlock a key. A terminal
    -- record stays terminal forever; see the claim function. It exists so an offline
    -- archival policy can find old rows.
    expires_at           timestamp with time zone default now() + interval '24 hours' not null,
    constraint mcp_purchase_idempotency_status_check
        check (status in ('PROCESSING', 'SUCCEEDED', 'FAILED_RETRYABLE', 'FAILED_FINAL', 'AMBIGUOUS')),
    -- The atomic identity: ownership is part of the key, so two different users may
    -- safely present the same external Idempotency-Key.
    constraint mcp_purchase_idempotency_identity_uk
        unique (user_id, operation, idempotency_key_hash)
);

comment on table public.mcp_purchase_idempotency is
    'Durable idempotency records for MCP purchases. Stores digests only, never raw idempotency keys.';

alter table public.mcp_purchase_idempotency
    owner to postgres;

create index if not exists mcp_purchase_idempotency_order_id_idx
    on public.mcp_purchase_idempotency (order_id);

create index if not exists mcp_purchase_idempotency_status_idx
    on public.mcp_purchase_idempotency (status, updated_at);

create index if not exists mcp_purchase_idempotency_expires_at_idx
    on public.mcp_purchase_idempotency (expires_at);

-- The backend reaches Supabase with the service role key. Anonymous/authenticated
-- clients must never read these rows, so no grants are issued to them and RLS is
-- enabled with no policy (service_role bypasses RLS).
alter table public.mcp_purchase_idempotency
    enable row level security;

revoke all on public.mcp_purchase_idempotency from anon, authenticated;

grant delete, insert, references, select, trigger, truncate, update
    on public.mcp_purchase_idempotency to service_role;

-- =====================================================================================
-- Atomic claim
--
-- One round trip that either claims the key for execution or explains why the
-- caller must not execute. Concurrency is resolved by PostgreSQL:
--   * INSERT ... ON CONFLICT DO NOTHING against the unique identity, and
--   * SELECT ... FOR UPDATE for the loser of the race.
-- There is no check-then-insert sequence anywhere.
--
-- TERMINAL KEYS ARE NEVER RECYCLED. There is deliberately no TTL-based re-claim: a
-- key that already bought something (or may have) stays spent forever, because the
-- alternative is charging a customer twice for one key after a clock elapsed.
--
--   SUCCEEDED        always replays the stored result, never re-executes
--   AMBIGUOUS        always stays blocked for manual intervention, never re-executes
--   FAILED_FINAL     stays terminal for that key; the caller must use a NEW key
--   FAILED_RETRYABLE re-claimable under the SAME key ONLY when the record proves no
--                    side effect occurred (order_id is null AND has_side_effects
--                    is false). Otherwise it replays like any other terminal state.
--   PROCESSING       never blindly executed; the service reconciles it against the
--                    authoritative order/profile rows (crash recovery)
--
-- Outcomes:
--   CLAIMED           caller owns the execution and must run the purchase exactly once
--   CONFLICT          same user + key, different canonical request
--   PROCESSING        another execution owns this key right now (or crashed)
--   RETRY             the row vanished mid-claim; caller retries, executes nothing
--   SUCCEEDED |
--   FAILED_FINAL |
--   FAILED_RETRYABLE |
--   AMBIGUOUS         terminal record: replay the stored response, do not execute
-- =====================================================================================
create or replace function public.mcp_claim_purchase_idempotency(
    p_user_id uuid,
    p_operation varchar,
    p_idempotency_key_hash varchar,
    p_request_hash varchar,
    p_ttl_seconds integer default 86400
)
    returns table
            (
                outcome       varchar,
                record_id     uuid,
                status        varchar,
                order_id      uuid,
                response_code integer,
                response_body jsonb,
                error_code    varchar,
                request_hash  varchar,
                created_at    timestamp with time zone,
                updated_at    timestamp with time zone,
                expires_at    timestamp with time zone
            )
    language plpgsql
    security definer
    set search_path = public
as
$$
declare
    v_row     public.mcp_purchase_idempotency%rowtype;
    v_outcome varchar;
begin
    insert into public.mcp_purchase_idempotency (user_id, operation, idempotency_key_hash, request_hash,
                                                 status, expires_at)
    values (p_user_id, p_operation, p_idempotency_key_hash, p_request_hash, 'PROCESSING',
            now() + make_interval(secs => p_ttl_seconds))
    on conflict (user_id, operation, idempotency_key_hash) do nothing
    returning * into v_row;

    if found then
        v_outcome := 'CLAIMED';
    else
        select *
          into v_row
          from public.mcp_purchase_idempotency
         where user_id = p_user_id
           and operation = p_operation
           and idempotency_key_hash = p_idempotency_key_hash
           for update;

        if not found then
            -- The row disappeared between the insert and the select. Do not execute:
            -- the caller retries and will claim it cleanly.
            v_outcome := 'RETRY';
        elsif v_row.request_hash <> p_request_hash then
            -- Checked before every other branch: a key presented for a different
            -- purchase is a conflict whatever state the original is in.
            v_outcome := 'CONFLICT';
        elsif v_row.status = 'FAILED_RETRYABLE'
              and v_row.order_id is null
              and v_row.has_side_effects = false then
            -- Provably nothing happened: no order row, no wallet debit, no
            -- provisioning call. Only here may the same key run again.
            update public.mcp_purchase_idempotency
               set status        = 'PROCESSING',
                   response_code = null,
                   response_body = null,
                   error_code    = null,
                   updated_at    = now()
             where id = v_row.id
            returning * into v_row;
            v_outcome := 'CLAIMED';
        else
            -- Everything else - SUCCEEDED, AMBIGUOUS, FAILED_FINAL, PROCESSING, and
            -- FAILED_RETRYABLE that did touch something - replays or blocks. Note
            -- there is no expires_at branch here: elapsed time never unlocks a key.
            v_outcome := v_row.status;
        end if;
    end if;

    return query
        select v_outcome::varchar,
               v_row.id,
               v_row.status,
               v_row.order_id,
               v_row.response_code,
               v_row.response_body,
               v_row.error_code,
               v_row.request_hash,
               v_row.created_at,
               v_row.updated_at,
               v_row.expires_at;
end;
$$;

alter function public.mcp_claim_purchase_idempotency(uuid, varchar, varchar, varchar, integer)
    owner to postgres;

revoke all on function public.mcp_claim_purchase_idempotency(uuid, varchar, varchar, varchar, integer)
    from anon, authenticated;

grant execute on function public.mcp_claim_purchase_idempotency(uuid, varchar, varchar, varchar, integer)
    to service_role;

-- =====================================================================================
-- RETENTION / CLEANUP POLICY (documentation - NO automated job is installed here)
-- =====================================================================================
-- These rows are financial evidence. The request path never deletes or recycles them,
-- and this migration deliberately installs no deletion job, no cron and no trigger.
--
-- If retention pressure ever requires cleanup, it must obey one rule:
--
--   Cleanup must NEVER make an already-used key executable again.
--
-- That means a purged row cannot simply disappear, because the very next request
-- carrying that key would then be CLAIMED and would buy a second bundle. Prune the
-- heavy columns and keep a permanent tombstone instead:
--
--   update public.mcp_purchase_idempotency
--      set response_body = null            -- the bulky part
--    where status in ('SUCCEEDED', 'FAILED_FINAL', 'AMBIGUOUS')
--      and updated_at < now() - interval '180 days';
--
-- The tombstone that MUST survive forever is: user_id, operation,
-- idempotency_key_hash, request_hash, status, order_id. With the body pruned a
-- replay can no longer return the original payload, so decide deliberately which
-- you prefer before running anything:
--   (a) keep bodies forever  -> replay stays perfect, storage grows; or
--   (b) prune bodies         -> the key stays blocked and the service answers from
--                               the AMBIGUOUS path (424, contact support) instead of
--                               replaying a stale success.
-- Option (b) is safe but user-visible, so it needs a support runbook first.
--
-- Hard-deleting a row is acceptable ONLY for records that never had a side effect:
--   delete from public.mcp_purchase_idempotency
--    where status = 'FAILED_RETRYABLE' and order_id is null and has_side_effects = false
--      and updated_at < now() - interval '30 days';
--
-- =====================================================================================
-- UPGRADE FROM AN EARLIER APPLICATION (only if this file was already run before)
-- =====================================================================================
-- Not needed for a fresh environment. CREATE TABLE IF NOT EXISTS will not do any of
-- this for you - run it explicitly, then re-run this file:
--
--   alter table public.mcp_purchase_idempotency
--       add column if not exists has_side_effects boolean default false not null;
--
--   -- Backfill: any record that already carries an order id certainly had a side effect.
--   update public.mcp_purchase_idempotency
--      set has_side_effects = true
--    where order_id is not null and has_side_effects = false;
--
--   -- Conservative backfill: a terminal record whose outcome is unknown must never be
--   -- treated as side-effect-free.
--   update public.mcp_purchase_idempotency
--      set has_side_effects = true
--    where status in ('SUCCEEDED', 'AMBIGUOUS', 'FAILED_FINAL') and has_side_effects = false;
--
-- The claim function is upgraded by the CREATE OR REPLACE above with no extra step.
--
-- =====================================================================================
-- VERIFICATION (run after applying; all of these must hold)
-- =====================================================================================
-- 1. Table exists with the expected columns, including has_side_effects:
--   select column_name, data_type, is_nullable, column_default
--     from information_schema.columns
--    where table_schema = 'public' and table_name = 'mcp_purchase_idempotency'
--    order by ordinal_position;
--   -- expect 14 rows; has_side_effects boolean NOT NULL default false
--
-- 2. The unique identity constraint exists:
--   select conname, pg_get_constraintdef(oid)
--     from pg_constraint
--    where conrelid = 'public.mcp_purchase_idempotency'::regclass and contype = 'u';
--   -- expect UNIQUE (user_id, operation, idempotency_key_hash)
--
-- 3. The status CHECK covers exactly the five lifecycle states:
--   select pg_get_constraintdef(oid) from pg_constraint
--    where conrelid = 'public.mcp_purchase_idempotency'::regclass and contype = 'c';
--
-- 4. RLS is on and no policy grants access:
--   select relrowsecurity from pg_class where relname = 'mcp_purchase_idempotency';  -- t
--   select count(*) from pg_policies where tablename = 'mcp_purchase_idempotency';   -- 0
--
-- 5. anon/authenticated hold no privileges on the table:
--   select grantee, privilege_type from information_schema.role_table_grants
--    where table_name = 'mcp_purchase_idempotency' and grantee in ('anon','authenticated');
--   -- expect 0 rows
--
-- 6. The claim function is the CURRENT version - it must contain no expires_at
--    recycling branch and must reference has_side_effects:
--   select prosrc like '%has_side_effects%'          as has_side_effect_guard,   -- t
--          prosrc like '%expires_at <= now()%'       as still_recycles           -- f
--     from pg_proc where proname = 'mcp_claim_purchase_idempotency';
--
-- 7. Function privileges: service_role only.
--   select grantee, privilege_type from information_schema.role_routine_grants
--    where routine_name = 'mcp_claim_purchase_idempotency';
--
-- =====================================================================================
-- Rollback (manual, only if the feature is abandoned):
--   drop function if exists public.mcp_claim_purchase_idempotency(uuid, varchar, varchar, varchar, integer);
--   drop table if exists public.mcp_purchase_idempotency;
-- Rolling back the FEATURE does not require this: set MCP_PURCHASE_ENABLED=false.
-- =====================================================================================
