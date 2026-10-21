# MCP purchase adapter

Three routes exist for the external `mcp-service`. They add **no database objects** — no
tables, columns, migrations, SQL scripts, RPCs, functions, triggers, sequences, indexes,
constraints, grants or RLS policies — and they own no persistence. They validate the
request, translate the payload, and call the same services the website and the mobile
application call.

| Route | Delegates to |
| --- | --- |
| `POST /api/v1/mcp/user/bundle/assign` | `UserBundleService.assign(payment_type=Wallet)` |
| `POST /api/v1/mcp/user/bundle/card/checkout` | `create_hosted_checkout_session` + `UserOrderRepo` |
| `GET /api/v1/mcp/user/bundle/card/status/{payment_reference}` | `UserOrderRepo` / `UserProfileRepo` |

## Feature flags

| Variable | Default | Effect |
| --- | --- | --- |
| `MCP_PURCHASE_ENABLED` | off | Master switch for all three routes |
| `MCP_CARD_PURCHASE_ENABLED` | off | Card routes only; also requires the master switch |
| `MCP_CARD_SUCCESS_URL` | — | Absolute `https` redirect target after payment |
| `MCP_CARD_CANCEL_URL` | — | Absolute `https` redirect target after abandonment |
| `MCP_CARD_SESSION_EXPIRY_MINUTES` | `31` | Page lifetime, clamped to Stripe's 31..1439 window |

Both flags fail closed: unset, blank or unparseable means off. Neither is read anywhere in
the legacy request path or in the webhook handler — `grep` for either name across `app/` and
the only hits are the MCP modules themselves — so legacy traffic is unaffected in either
position. A test asserts this.

## Authentication and trust

All three routes use the existing `bearer_token` dependency, deliberately **not**
`bearer_token_anonymous` (which the legacy assign route uses to support guest checkout).
The caller is resolved from the verified token and from nowhere else.

Every request model declares `extra="forbid"`, so a `user_id`, `amount`, `currency`,
`balance`, `order_id`, `checkout_url`, `paid` flag, `payment_status`, card number, CVV,
expiry, cardholder or payment token is a `422` for the whole request rather than a field
the endpoint politely ignores. There is no field through which any of them could arrive.

## Identifiers

There is no MCP-specific identifier. `payment_reference` **is** the `user_order.id`
returned by the shared purchase flow, and status polling reads that row back. This is why
no new persistence is needed: the order record already *is* the record of the purchase.

## Wallet

`POST /api/v1/mcp/user/bundle/assign` translates the MCP body into the same `AssignRequest`
a mobile or web client sends and calls `UserBundleService.assign` with
`payment_type=Wallet`. Bundle lookup, availability, server-side pricing, the wallet balance
check, the wallet debit, order creation and eSIM provisioning all happen inside that call.
An insufficient balance raises the platform's existing `INSUFFICIENT_WALLET_BALANCE` and
propagates unchanged.

The one thing the adapter adds is honesty about the outcome. The shared wallet branch
answers `COMPLETED` as soon as it finishes, but `BundleService.buy_bundle` reports a failed
eSIM hub order by *returning* an exception object rather than raising one — so a wallet
purchase whose provisioning failed still answers `COMPLETED` today. That is long-standing
behaviour the website depends on and it is **left exactly as it is**. For MCP, the order row
the flow just wrote is read back through the existing repository, so:

| Order row | MCP answer | HTTP |
| --- | --- | --- |
| payment success + order success + profile exists | `COMPLETED` | 200 |
| payment success + (order failed or no profile) | `MANUAL_INTERVENTION_REQUIRED` | 424 |
| payment failed / order failed / cancelled | `FAILED` | 200 |
| anything else | `PENDING` | 200 |

## Card checkout and the webhook

The legacy card flow builds a Stripe `PaymentIntent` for the native SDK payment sheet. That
yields a `client_secret`, which a chat client cannot use — it needs a URL. So the MCP card
route creates a hosted Checkout Session instead (`create_hosted_checkout_session` in
`app/config/utils.py`, added alongside the untouched `create_payment_intent`). This is the
only new Stripe surface, and it exists because the codebase had no hosted-checkout call to
reuse. `create_payment_intent` is never called from the MCP path, so no abandoned
PaymentIntent is left behind.

Everything downstream is unchanged. The Session's `payment_intent_data.metadata` carries the
same keys `__handle_card_payment` writes (`order_id`, `user_id`, `device_id`, `bundle_code`,
`order_type`, `env`, `rule_id`, `amount`), so the `payment_intent.succeeded` event it emits
is fulfilled by the **existing** `CallbackService.handle_payment_webhook` through the path it
already uses:

* no MCP webhook handler,
* no second provisioning implementation,
* no new Stripe webhook subscription,
* no MCP branch or feature flag in `callback_service.py` — the file is byte-identical to its
  committed state, which a test asserts with `git diff`.

An `mcp_source` metadata key is added for support triage. The existing handler never reads
it; it reads the keys it has always read and ignores the rest.

Nothing marks an order paid at checkout-creation time. A browser landing on
`MCP_CARD_SUCCESS_URL` proves nothing and is never consulted, and neither is a chat client
saying "I paid". The signature-verified webhook is the only payment authority.

The returned link is validated rather than trusted: only a plain `https` URL with a host, no
embedded credentials, no whitespace or control characters. Anything else is refused with
`MCP_CARD_CHECKOUT_UNAVAILABLE` rather than shown to a user.

## Card status

`GET /api/v1/mcp/user/bundle/card/status/{payment_reference}` is a pure read of the existing
`user_order` and `user_profile` rows, scoped to the authenticated caller in the query itself
— so an order belonging to somebody else is indistinguishable from one that never existed.
It mutates nothing, triggers no provisioning, makes no provider call and accepts no status
from the client.

| Order row | Reported status |
| --- | --- |
| payment or order cancelled | `CANCELLED` |
| payment success + order failed | `AMBIGUOUS` |
| payment success + order success + profile exists | `COMPLETED` |
| payment success, otherwise | `PROVISIONING` |
| payment failed or order failed | `FAILED` |
| anything else | `PENDING` |

`EXPIRED` is never emitted: the platform stores no page-expiry timestamp, and inventing one
would require a new column. A lapsed page reads as `PENDING`; the MCP service's own checkout
TTL is what tells the user the link went stale.

## Idempotency

**Card: at the provider.** The Stripe call is keyed `mcp-card-<order_id>`. `order_id` is a
database-generated primary key that is already persisted, so a repeated call with the same
order returns the Session Stripe already created rather than opening a second payment page.
Note that a *new* MCP request creates a new order and therefore a new key — this protects
against a retried provider call, not against a repeated MCP request.

**Wallet: none, and none is claimed.** The existing schema offers no idempotency surface for
a wallet purchase:

* `user_order` has no unique constraint a caller could key on, and no idempotency column
  (see `supabase_ddl.sql`);
* `payment_intent_code` is nullable, not unique, and is not populated for a wallet purchase;
* the wallet transaction records carry no caller-supplied key either;
* there is no Stripe request to attach an idempotency key to, because a wallet purchase never
  contacts Stripe.

Providing the guarantee would require a new table, which this branch deliberately does not
add. Process-local or in-memory de-duplication was rejected: it is unsafe across replicas and
across restarts, and would read as a guarantee that does not hold.

The `Idempotency-Key` header is still accepted so the external service does not have to
change, but it is **not honoured**, and both responses always report
`idempotent_replay: false`. A repeated wallet call is a second purchase.

To close this properly, a follow-up MR needs either a durable idempotency record or a unique
constraint on an existing table — both of which are schema changes.

## Error codes

Stable, safe keys translated through `locales/`. Nothing carries a token, a Stripe secret, a
webhook secret, an OTP, a full personal identifier, a stack trace or an internal exception
message.

| Condition | Code | HTTP |
| --- | --- | --- |
| unauthenticated / expired session | `BEARER_TOKEN_REQUIRED` (existing) | 401 |
| anonymous session | `MCP_ANONYMOUS_NOT_ALLOWED` | 401 |
| invalid or inactive bundle | `BUNDLE_NOT_AVAILABLE` (existing) | 400 |
| insufficient wallet balance | `INSUFFICIENT_WALLET_BALANCE` (existing) | 400 |
| currency mismatch | `MCP_UNSUPPORTED_CURRENCY` | 400 |
| MCP disabled | `MCP_PURCHASE_DISABLED` | 503 |
| card checkout disabled | `MCP_CARD_PURCHASE_DISABLED` | 503 |
| card redirect misconfigured | `MCP_CARD_CONFIG_INVALID` | 503 |
| provider could not open a page | `MCP_CARD_CHECKOUT_UNAVAILABLE` | 503 |
| payment not found / not the caller's | `MCP_PAYMENT_NOT_FOUND` | 404 |
| charged without an eSIM | `MCP_MANUAL_INTERVENTION_REQUIRED` | 424 |
| dependency unavailable | `MCP_DEPENDENCY_UNAVAILABLE` | 502/503 |

A configuration or authentication failure is never hidden behind a generic response: a
missing redirect URL answers `MCP_CARD_CONFIG_INVALID`, not "bundle unavailable".
