# Phase 5A — Quick Reference
## MCP-Only Stripe Hosted Checkout

Verified against the source of truth on 2026-08-03. Contains **no secret values**.

---

## 1. Forward SQL migration

```
migrations/20260803_0001_mcp_card_checkout.sql
```

Absolute path:
`/home/mohammadtokko/projects/e-sim/montyesim-eshop-api/migrations/20260803_0001_mcp_card_checkout.sql`

- Size: 10,665 bytes
- Creates two new tables: `mcp_card_checkout`, `mcp_stripe_webhook_event`
- Strictly additive — touches no existing table, column, index or function
- **Has NOT been executed against any database**

Apply manually (validate in a rolled-back transaction first):

```bash
psql "$DATABASE_URL" -c "BEGIN;" \
  -f migrations/20260803_0001_mcp_card_checkout.sql -c "ROLLBACK;"

psql "$DATABASE_URL" -f migrations/20260803_0001_mcp_card_checkout.sql
```

---

## 2. Rollback SQL

```
migrations/20260803_0001_mcp_card_checkout_rollback.sql
```

Absolute path:
`/home/mohammadtokko/projects/e-sim/montyesim-eshop-api/migrations/20260803_0001_mcp_card_checkout_rollback.sql`

- Size: 2,996 bytes
- Drops the child table (`mcp_stripe_webhook_event`) first, then the parent (`mcp_card_checkout`)
- Deliberately does **not** delete `user_order` rows
- **Has NOT been executed**

> To disable the feature you do **not** need this file. Set
> `MCP_CARD_PURCHASE_ENABLED=false` and restart. Use the rollback only when abandoning
> the feature entirely.

---

## 3. QA test guide

```
QA_CARD_CHECKOUT_TEST.md
```

Absolute path:
`/home/mohammadtokko/projects/e-sim/montyesim-eshop-api/QA_CARD_CHECKOUT_TEST.md`

- Size: 13,910 bytes
- Stripe **test mode only**; 10 sections plus a sign-off checklist

---

## 4. Environment variables (names + placeholders only)

### New in Phase 5A

```env
MCP_CARD_PURCHASE_ENABLED=false
MCP_CARD_SUCCESS_URL=https://example.test/mcp/checkout/success
MCP_CARD_CANCEL_URL=https://example.test/mcp/checkout/cancel
MCP_CARD_SESSION_EXPIRY_MINUTES=30
```

| Variable | Required | Default | Notes |
|---|---|---|---|
| `MCP_CARD_PURCHASE_ENABLED` | no | `false` | Gates only the two new routes and the webhook branch |
| `MCP_CARD_SUCCESS_URL` | **yes when enabled** | — | Absolute http(s); HTTPS enforced outside localhost |
| `MCP_CARD_CANCEL_URL` | **yes when enabled** | — | Same validation |
| `MCP_CARD_SESSION_EXPIRY_MINUTES` | no | `30` | Clamped to Stripe's limits (30–1440) |

### Pre-existing and reused — Phase 5A introduces no new secret

| Variable | Required | Notes |
|---|---|---|
| `STRIPE_SECRET_KEY` | yes | Must be a **test** key (`sk_test_…` / `rk_test_…`) for QA |
| `STRIPE_WEBHOOK_SECRET` | yes | Verifies `Stripe-Signature` on the shared webhook |
| `MCP_IDEMPOTENCY_HASH_SECRET` | yes | ≥ 32 chars; identical on every replica and stable forever |
| `SYSTEM_CURRENCY` | no (default `USD`) | The only currency card checkout settles in |

`DEFAULT_CURRENCY` is **not** used by these endpoints.

---

## 5. Stripe webhook endpoint

```
POST /api/v1/callback/payment-webhook
```

This is the **existing, unchanged** endpoint. Phase 5A adds no webhook route — it only
adds a guarded branch inside the existing handler.

Also pre-existing (not used by Phase 5A): `POST /api/v1/callback/payment-webhook-fake`.

---

## 6. Required Stripe events

**No new subscription is required.** This feature consumes the SAME events the legacy
Card flow already uses, on the SAME endpoint.

```
payment_intent.succeeded        # already subscribed - legacy depends on it
payment_intent.payment_failed   # subscribe if not already
payment_intent.canceled         # optional - reflects checkout expiry sooner
```

Routing between the two flows is by the `mcp_source` marker inside the PaymentIntent
metadata, never by event type. A legacy PaymentIntent carries no marker and continues
down exactly the path it always took.

Without `payment_intent.canceled`, expiry is still reported correctly: the status
endpoint derives it from the stored `expires_at`.

---

## 7. Endpoint request / response examples

Registered routes (read back from the running app):

```
POST  /api/v1/mcp/user/bundle/card/checkout
GET   /api/v1/mcp/user/bundle/card/status/{payment_reference}
```

### 7.1 Create checkout — request

```http
POST /api/v1/mcp/user/bundle/card/checkout HTTP/1.1
Host: <qa-host>
Authorization: Bearer <esim-access-token>
X-Device-Id: qa-device-1
Idempotency-Key: 3f9c1a7e5b2d84061c9df7e3b5a2c84d6019fbe7
X-Currency: USD                       # optional; omit to use SYSTEM_CURRENCY
Content-Type: application/json

{
  "bundle_code": "bundle-0000-0000-0000-000000000001",
  "quote_reference": "mcp-quote-card-1",
  "related_search": {
    "region": null,
    "countries": [{ "iso3_code": "FRA", "country_name": "France" }]
  }
}
```

Only these three fields are accepted. `payment_type` is fixed internally to Card/Stripe.
`extra="forbid"` rejects `amount`, `price`, `tax`, `final_price`, `currency`,
`card_number`, `cvc`, `exp_month`, `payment_method_id`, `stripe_token`, `user_id`,
`order_id`, `access_token`, `promo_code`.

### 7.2 Create checkout — 200 OK

Response header: `X-Idempotent-Replay: false`

```json
{
  "status": "success",
  "totalCount": 0,
  "data": {
    "payment_reference": "a0fa641e-b456-4aaa-89af-c99dd9b3d8ce",
    "order_id": "fd4944f5-f8e5-496c-8457-f230ea1af7dd",
    "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_a1B2c3D4...",
    "status": "PENDING",
    "amount": "10.00",
    "currency": "USD",
    "expires_at": "2026-08-03T12:30:00+00:00",
    "idempotent_replay": false,
    "correlation_id": "42ebb6ff15334285998186f98ca31c5a",
    "message": null
  },
  "title": null,
  "message": null,
  "developerMessage": null,
  "responseCode": 200
}
```

### 7.3 Create checkout — replay

Same Idempotency-Key + same body. Header `X-Idempotent-Replay: true`, identical
`payment_reference` and `checkout_url`, `"idempotent_replay": true`. No second Stripe
Session is created.

### 7.4 Create checkout — 409 conflict

Same key with a changed bundle, quote_reference or related_search:

```json
{
  "status": "failed",
  "totalCount": 0,
  "data": null,
  "title": "This Idempotency-Key was already used for a different request.",
  "message": "This Idempotency-Key was already used for a different request.",
  "developerMessage": "This Idempotency-Key was already used for a different checkout request. Use a new Idempotency-Key for a different quote or bundle.",
  "responseCode": 409
}
```

### 7.5 Create checkout — 503 feature disabled

```json
{
  "status": "failed",
  "totalCount": 0,
  "data": null,
  "title": "Card checkout is not available right now.",
  "message": "Card checkout is not available right now.",
  "developerMessage": "MCP card purchase is not enabled in this environment",
  "responseCode": 503
}
```

### 7.6 Create checkout — 400 unsupported currency

Sent with `X-Currency: EUR`:

```
developerMessage: "MCP card checkout is settled in USD only. Send X-Currency: USD, or omit the header."
```

### 7.7 Create checkout — 503 ambiguous (Stripe timeout)

`data.status` is `"AMBIGUOUS"`, `checkout_url` is `null`, and the message instructs the
caller to retry **the very same** Idempotency-Key. The retry reuses the same Stripe
idempotency key, so Stripe returns the original Session — it can never create a second
one.

### 7.8 Status poll — request

```http
GET /api/v1/mcp/user/bundle/card/status/a0fa641e-b456-4aaa-89af-c99dd9b3d8ce HTTP/1.1
Authorization: Bearer <esim-access-token>
X-Device-Id: qa-device-1
```

### 7.9 Status poll — 200 OK (before payment)

```json
{
  "status": "success",
  "totalCount": 0,
  "data": {
    "payment_reference": "a0fa641e-b456-4aaa-89af-c99dd9b3d8ce",
    "order_id": "fd4944f5-f8e5-496c-8457-f230ea1af7dd",
    "status": "PENDING",
    "amount": "10.00",
    "currency": "USD",
    "bundle_code": "bundle-0000-0000-0000-000000000001",
    "quote_reference": "mcp-quote-card-1",
    "expires_at": "2026-08-03T12:30:00+00:00",
    "provisioned": false,
    "next_action": "OPEN_CHECKOUT_URL",
    "message": null
  },
  "title": null,
  "message": null,
  "developerMessage": null,
  "responseCode": 200
}
```

### 7.10 Status poll — other states

| State | `provisioned` | `next_action` |
|---|---|---|
| `PENDING` | false | `OPEN_CHECKOUT_URL` |
| `PAID` | false | `WAIT_FOR_CONFIRMATION` |
| `PROVISIONING` | false | `WAIT_FOR_CONFIRMATION` |
| `COMPLETED` | **true** | `GET_ESIM_BY_ORDER` |
| `FAILED` | false | `START_NEW_CHECKOUT` |
| `EXPIRED` | false | `START_NEW_CHECKOUT` |
| `CANCELLED` | false | `START_NEW_CHECKOUT` |
| `AMBIGUOUS` | false | `CONTACT_SUPPORT` |

`AMBIGUOUS` message: *"Payment may have been taken without a completed eSIM. Contact
support."*

### 7.11 Status poll — 404

Returned identically for a reference owned by another user and one that does not exist,
so the endpoint cannot be used to probe for the existence of someone else's payment:

```json
{
  "status": "failed",
  "totalCount": 0,
  "data": null,
  "title": "Payment reference not found.",
  "message": "Payment reference not found.",
  "developerMessage": "Payment reference not found",
  "responseCode": 404
}
```

---

## Security notes

- No response on either endpoint carries a Stripe session id, payment-intent id, client
  secret or API key.
- Card details are entered on Stripe's hosted page only. No endpoint in this phase
  accepts a card number, CVC, expiry, PaymentMethod id or Stripe token.
- A browser redirect to `MCP_CARD_SUCCESS_URL` never marks anything paid. Only a
  signature-verified webhook can do that.
- eSIM provisioning happens exactly once, gated by a unique Stripe event-id ledger plus
  a compare-and-set on the checkout status.
