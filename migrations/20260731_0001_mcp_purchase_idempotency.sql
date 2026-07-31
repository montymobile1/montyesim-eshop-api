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
-- Idempotent by construction: re-running it is safe.
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
    created_at           timestamp with time zone default now()             not null,
    updated_at           timestamp with time zone default now()             not null,
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
-- Outcomes:
--   CLAIMED           caller owns the execution and must run the purchase exactly once
--   CONFLICT          same user + key, different canonical request
--   PROCESSING        another execution owns this key right now (or crashed)
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
            -- The row disappeared between the insert and the select (expiry cleanup).
            -- Do not execute: the caller retries and will claim it cleanly.
            v_outcome := 'RETRY';
        elsif v_row.status <> 'PROCESSING' and v_row.expires_at <= now() then
            -- Terminal record past its replay TTL: the key may be recycled.
            update public.mcp_purchase_idempotency
               set request_hash  = p_request_hash,
                   status        = 'PROCESSING',
                   order_id      = null,
                   response_code = null,
                   response_body = null,
                   error_code    = null,
                   updated_at    = now(),
                   expires_at    = now() + make_interval(secs => p_ttl_seconds)
             where id = v_row.id
            returning * into v_row;
            v_outcome := 'CLAIMED';
        elsif v_row.request_hash <> p_request_hash then
            v_outcome := 'CONFLICT';
        elsif v_row.status = 'FAILED_RETRYABLE' then
            -- Failed before any financial or provisioning side effect: the same key
            -- may be retried and re-claims the record.
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
            v_outcome := v_row.status;
        end if;
    end if;

    return query
        select v_outcome,
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
-- Rollback (manual, only if the feature is abandoned):
--   drop function if exists public.mcp_claim_purchase_idempotency(uuid, varchar, varchar, varchar, integer);
--   drop table if exists public.mcp_purchase_idempotency;
-- Rolling back the FEATURE does not require this: set MCP_PURCHASE_ENABLED=false.
-- =====================================================================================
