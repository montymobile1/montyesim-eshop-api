-- =====================================================================================
-- MCP card checkout - Stripe hosted Checkout Sessions (Phase 5A)
--
-- Adds the persistence the MCP-only endpoints need:
--   POST /api/v1/mcp/user/bundle/card/checkout
--   GET  /api/v1/mcp/user/bundle/card/status/{payment_reference}
--
-- This project has no migration framework: apply this file MANUALLY, once per
-- environment, BEFORE setting MCP_CARD_PURCHASE_ENABLED=true.
--
-- It is strictly ADDITIVE. It creates two new tables and touches NO existing table,
-- column, constraint, index or function. The legacy Card flow, the legacy webhook, the
-- Wallet flow and the MCP Wallet flow are all unaffected by running it.
--
--   psql "$DATABASE_URL" -f migrations/20260803_0001_mcp_card_checkout.sql
--   (or paste it into the Supabase SQL editor)
--
-- Rollback: migrations/20260803_0001_mcp_card_checkout_rollback.sql
--
-- -------------------------------------------------------------------------------------
-- RE-RUN BEHAVIOUR - READ BEFORE RUNNING THIS A SECOND TIME
-- -------------------------------------------------------------------------------------
-- Re-running will not error and will not destroy data, but it is NOT an upgrade:
--   * CREATE TABLE IF NOT EXISTS is a NO-OP when the table already exists. It does NOT
--     add columns, alter types, or reconcile an older definition. If an earlier version
--     of this file was applied, the table keeps its OLD shape and this file will
--     silently appear to have succeeded.
--   * CREATE INDEX IF NOT EXISTS is likewise a no-op per index name.
-- If an earlier version was already applied, diff the columns against section
-- VERIFICATION below and ALTER explicitly before relying on the new shape.
--
-- At the time of writing this migration has not been applied to any environment.
-- =====================================================================================

-- =====================================================================================
-- 1. Checkout records. The primary key IS the payment_reference handed to the caller.
-- =====================================================================================
create table if not exists public.mcp_card_checkout
(
    id                       uuid                     default gen_random_uuid() not null
        constraint mcp_card_checkout_pkey primary key,
    user_id                  uuid                                               not null
        constraint mcp_card_checkout_user_id_fkey references auth.users (id),
    order_id                 uuid
        constraint mcp_card_checkout_order_id_fkey references public.user_order (id),
    bundle_code              varchar(200)                                       not null,
    quote_reference          varchar(128),
    -- Correlates back to the row in mcp_purchase_idempotency that owns this attempt.
    idempotency_record_id    uuid
        constraint mcp_card_checkout_idem_fkey references public.mcp_purchase_idempotency (id),
    request_hash             varchar(128),
    -- Authoritative, server-computed amount in the currency's smallest unit. Never
    -- supplied by the caller; re-verified against Stripe before any fulfilment.
    amount_minor             integer                                            not null,
    currency                 varchar(8)                                         not null,
    status                   varchar(32)              default 'PENDING'         not null,
    -- Non-secret Stripe identifiers, kept for reconciliation only. No client secret,
    -- no session secret and no API key is ever stored in this table.
    stripe_session_id        varchar(255),
    stripe_payment_intent_id varchar(255),
    checkout_url             text,
    expires_at               timestamp with time zone,
    paid_at                  timestamp with time zone,
    provisioned_at           timestamp with time zone,
    failure_code             varchar(64),
    created_at               timestamp with time zone default now()             not null,
    updated_at               timestamp with time zone default now()             not null,
    constraint mcp_card_checkout_status_check
        check (status in ('PENDING', 'PAID', 'PROVISIONING', 'COMPLETED',
                          'FAILED', 'EXPIRED', 'CANCELLED', 'AMBIGUOUS')),
    constraint mcp_card_checkout_amount_check check (amount_minor >= 0),
    -- One Stripe Session maps to exactly one checkout record, and likewise one
    -- PaymentIntent. The intent constraint is what makes the webhook lookup
    -- unambiguous: this feature consumes payment_intent.* events, so the intent id is
    -- the identifier an incoming event is resolved by.
    constraint mcp_card_checkout_session_uk unique (stripe_session_id),
    constraint mcp_card_checkout_intent_uk unique (stripe_payment_intent_id)
);

comment on table public.mcp_card_checkout is
    'MCP-only Stripe hosted checkout attempts. Stores no Stripe secrets; the id is the caller-facing payment_reference.';

alter table public.mcp_card_checkout
    owner to postgres;

create index if not exists mcp_card_checkout_user_id_idx
    on public.mcp_card_checkout (user_id);

create index if not exists mcp_card_checkout_order_id_idx
    on public.mcp_card_checkout (order_id);

create index if not exists mcp_card_checkout_status_idx
    on public.mcp_card_checkout (status, updated_at);

create index if not exists mcp_card_checkout_idem_idx
    on public.mcp_card_checkout (idempotency_record_id);

-- Webhook hot path: every payment_intent.* event resolves the checkout by intent id.
create index if not exists mcp_card_checkout_intent_idx
    on public.mcp_card_checkout (stripe_payment_intent_id);

-- The backend reaches Supabase with the service role key. Anonymous/authenticated
-- clients must never read these rows, so no grants are issued to them and RLS is
-- enabled with no policy (service_role bypasses RLS).
alter table public.mcp_card_checkout
    enable row level security;

revoke all on public.mcp_card_checkout from anon, authenticated;

grant delete, insert, references, select, trigger, truncate, update
    on public.mcp_card_checkout to service_role;


-- =====================================================================================
-- 2. Webhook dedupe ledger.
--
-- Stripe guarantees neither once-only delivery nor ordering. The unique event_id is the
-- entire duplicate-suppression mechanism: the second delivery of an event collides on
-- insert and is discarded before it can touch any state.
--
-- Scoped to MCP card events only. The legacy webhook has never de-duplicated events and
-- its behaviour is deliberately left exactly as it is.
-- =====================================================================================
create table if not exists public.mcp_stripe_webhook_event
(
    id          uuid                     default gen_random_uuid() not null
        constraint mcp_stripe_webhook_event_pkey primary key,
    event_id    varchar(255)                                       not null
        constraint mcp_stripe_webhook_event_uk unique,
    event_type  varchar(128)                                       not null,
    checkout_id uuid
        constraint mcp_stripe_webhook_event_checkout_fkey references public.mcp_card_checkout (id),
    status      varchar(64),
    received_at timestamp with time zone default now()             not null
);

comment on table public.mcp_stripe_webhook_event is
    'Idempotency ledger for Stripe events consumed by the MCP card flow. One row per Stripe event id.';

alter table public.mcp_stripe_webhook_event
    owner to postgres;

create index if not exists mcp_stripe_webhook_event_checkout_idx
    on public.mcp_stripe_webhook_event (checkout_id);

create index if not exists mcp_stripe_webhook_event_received_idx
    on public.mcp_stripe_webhook_event (received_at);

alter table public.mcp_stripe_webhook_event
    enable row level security;

revoke all on public.mcp_stripe_webhook_event from anon, authenticated;

grant delete, insert, references, select, trigger, truncate, update
    on public.mcp_stripe_webhook_event to service_role;


-- =====================================================================================
-- RETENTION (documentation - NO automated job is installed here)
-- =====================================================================================
-- These rows are financial evidence and the request path never deletes them.
--
-- Cleanup must NEVER delete a mcp_stripe_webhook_event row whose checkout still exists:
-- removing the ledger entry makes an already-processed Stripe redelivery look new, and
-- a redelivered checkout.session.completed would then be processed a second time.
-- The compare-and-set on mcp_card_checkout.status still prevents a double eSIM, but the
-- ledger is the first line of defence and should be kept for at least as long as Stripe
-- retries (72 hours) plus a wide margin. Prefer keeping both tables indefinitely.
--
-- =====================================================================================
-- VERIFICATION (run after applying; all of these must hold)
-- =====================================================================================
-- 1. Both tables exist with the expected columns:
--   select table_name, column_name, data_type, is_nullable
--     from information_schema.columns
--    where table_schema = 'public'
--      and table_name in ('mcp_card_checkout','mcp_stripe_webhook_event')
--    order by table_name, ordinal_position;
--   -- expect 20 rows for mcp_card_checkout, 6 for mcp_stripe_webhook_event
--
-- 2. Status CHECK covers exactly the eight normalized states:
--   select pg_get_constraintdef(oid) from pg_constraint
--    where conrelid = 'public.mcp_card_checkout'::regclass and contype = 'c';
--
-- 3. Uniqueness that the correctness argument depends on:
--   select conname, pg_get_constraintdef(oid) from pg_constraint
--    where conrelid in ('public.mcp_card_checkout'::regclass,
--                       'public.mcp_stripe_webhook_event'::regclass)
--      and contype = 'u';
--   -- expect UNIQUE (stripe_session_id), UNIQUE (stripe_payment_intent_id), UNIQUE (event_id)
--
-- 4. RLS on, no policies:
--   select relname, relrowsecurity from pg_class
--    where relname in ('mcp_card_checkout','mcp_stripe_webhook_event');           -- t, t
--   select count(*) from pg_policies
--    where tablename in ('mcp_card_checkout','mcp_stripe_webhook_event');         -- 0
--
-- 5. anon/authenticated hold no privileges (expect 0 rows):
--   select grantee, table_name, privilege_type from information_schema.role_table_grants
--    where table_name in ('mcp_card_checkout','mcp_stripe_webhook_event')
--      and grantee in ('anon','authenticated');
--
-- 6. Nothing pre-existing was altered - the Phase 4A table is untouched:
--   select count(*) from information_schema.columns
--    where table_schema='public' and table_name='mcp_purchase_idempotency';       -- 14
-- =====================================================================================
