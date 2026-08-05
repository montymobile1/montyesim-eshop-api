# QA — MCP Card Checkout (Phase 5A)

Manual test plan for the MCP-only Stripe **hosted** checkout.
**Stripe TEST MODE ONLY.** Never run this against a live key.

- Endpoints under test (MCP-only):
  - `POST /api/v1/mcp/user/bundle/card/checkout`
  - `GET  /api/v1/mcp/user/bundle/card/status/{payment_reference}`
- Webhook (existing, shared): `POST /api/v1/callback/payment-webhook`

Card details are entered **on Stripe's hosted page only**. They never pass through the
chatbot, the MCP server, or this backend. There is no endpoint in this phase that
accepts a card number, CVC, expiry, PaymentMethod id or Stripe token — the request body
rejects unknown fields outright.

---

## 0. Pre-flight

### 0.1 Apply the migration

Validate first inside a transaction you roll back, so nothing is persisted:

```bash
psql "$DATABASE_URL" -c "BEGIN;" \
  -f migrations/20260803_0001_mcp_card_checkout.sql -c "ROLLBACK;"
```

Then apply for real:

```bash
psql "$DATABASE_URL" -f migrations/20260803_0001_mcp_card_checkout.sql
```

Run the seven verification queries in the file's `VERIFICATION` section. All must hold.

Rollback (only if abandoning the feature):
`migrations/20260803_0001_mcp_card_checkout_rollback.sql`

### 0.2 Environment

```env
MCP_CARD_PURCHASE_ENABLED=true
MCP_CARD_SUCCESS_URL=https://<qa-host>/mcp/checkout/success
MCP_CARD_CANCEL_URL=https://<qa-host>/mcp/checkout/cancel
MCP_CARD_SESSION_EXPIRY_MINUTES=30

# Reused from the existing Stripe integration - no new secret is introduced:
STRIPE_SECRET_KEY=sk_test_...      # MUST be a TEST key
STRIPE_WEBHOOK_SECRET=whsec_...
MCP_IDEMPOTENCY_HASH_SECRET=<openssl rand -hex 32>   # stable across replicas
SYSTEM_CURRENCY=USD
```

Confirm the key is a test key before anything else:

```bash
curl -s https://api.stripe.com/v1/balance -u "$STRIPE_SECRET_KEY:" | head -c 200
# a test key returns livemode:false; STOP if you see livemode:true
```

### 0.3 Stripe dashboard

**No new subscription is required.** This feature consumes the SAME events the legacy
Card flow already uses on the SAME endpoint (`/api/v1/callback/payment-webhook`):

- `payment_intent.succeeded`      (already subscribed — legacy depends on it)
- `payment_intent.payment_failed` (subscribe if not already)
- `payment_intent.canceled`       (optional — reflects checkout expiry sooner)

Routing between the two flows is by the `mcp_source` marker inside the PaymentIntent
metadata, not by event type. A legacy PaymentIntent has no marker and continues down
exactly the path it always took.

If `payment_intent.canceled` is not subscribed, expiry is still reported correctly:
the status endpoint derives it from the stored `expires_at`.

### 0.4 Shell helpers

```bash
QA=https://<qa-host>
TOKEN=<eSIM bearer token for a verified, non-anonymous user>
TOKEN_B=<bearer token for a DIFFERENT user>
BUNDLE=<an active bundle_code>
newkey() { openssl rand -hex 20; }   # 40 chars, satisfies the 32..128 rule

checkout() {  # $1 = idempotency key, $2 = quote ref, $3 = bundle (optional)
  curl -s -i -X POST "$QA/api/v1/mcp/user/bundle/card/checkout" \
    -H "Authorization: Bearer $TOKEN" -H "X-Device-Id: qa-device-1" \
    -H "Idempotency-Key: $1" -H "Content-Type: application/json" \
    -d "{\"bundle_code\":\"${3:-$BUNDLE}\",\"quote_reference\":\"$2\",
         \"related_search\":{\"region\":null,\"countries\":[]}}"
}

status() { curl -s "$QA/api/v1/mcp/user/bundle/card/status/$1" \
  -H "Authorization: Bearer $TOKEN" -H "X-Device-Id: qa-device-1"; }
```

### 0.5 Stripe test cards (hosted page)

| Card | Behaviour |
|---|---|
| `4242 4242 4242 4242` | succeeds immediately |
| `4000 0000 0000 0002` | declined |
| `4000 0025 0000 3155` | requires 3DS authentication |
| `4000 0000 0000 9995` | insufficient funds |

Any future expiry, any CVC, any postcode.

---

## 1. Feature flag

| # | Step | Expected |
|---|---|---|
| 1.1 | Set `MCP_CARD_PURCHASE_ENABLED=false`, restart, call `checkout` | **503**, `MCP_CARD_PURCHASE_DISABLED`; `select count(*) from mcp_card_checkout` = 0 |
| 1.2 | Same, call `status/<any>` | **503** |
| 1.3 | Legacy card purchase via the app (`POST /api/v1/user/bundle/assign`, `payment_type=Card`) | Works exactly as before, both with the flag on and off |
| 1.4 | MCP wallet purchase (`POST /api/v1/mcp/user/bundle/assign`) | Unaffected by this flag |
| 1.5 | Re-enable the flag, restart | Proceed |

---

## 2. Happy path — the core test

| # | Step | Expected |
|---|---|---|
| 2.1 | `K=$(newkey); checkout $K quote-001` | **200**. Body has `payment_reference`, `order_id`, `checkout_url`, `status:"PENDING"`, `amount`, `currency:"USD"`, `expires_at`. Header `X-Idempotent-Replay: false` |
| 2.2 | Inspect the response | **No** `client_secret`, no `sk_...`, no `whsec_`, no session id field |
| 2.3 | Check the amount | Equals the bundle's current price from the catalogue — not anything you sent |
| 2.4 | `status <payment_reference>` | `PENDING`, `provisioned:false`, `next_action:"OPEN_CHECKOUT_URL"` |
| 2.5 | DB: `select status, amount_minor, currency, order_id from mcp_card_checkout` | one row, `PENDING` |
| 2.6 | DB: `select payment_status, order_status, payment_type from user_order where id=...` | `pending`, `pending`, `Card` |
| 2.7 | **Before paying**, visit `MCP_CARD_SUCCESS_URL` directly in a browser | Status stays `PENDING`. **A redirect must never mark anything paid** |
| 2.8 | Open `checkout_url`, pay with `4242...` | Stripe confirms |
| 2.9 | Wait for the webhook, then `status <payment_reference>` | `COMPLETED`, `provisioned:true`, `next_action:"GET_ESIM_BY_ORDER"` |
| 2.10 | DB: `user_order` | `payment_status=success`, `order_status=success` |
| 2.11 | DB: `select count(*) from user_profile where user_order_id=...` | exactly **1** |
| 2.12 | Fetch the eSIM by order id through the normal API | Returns the provisioned eSIM |

---

## 3. Idempotency

| # | Step | Expected |
|---|---|---|
| 3.1 | `K=$(newkey); checkout $K quote-010` then repeat the identical call | Both **200**; identical `payment_reference` and `checkout_url`; second has `X-Idempotent-Replay: true` |
| 3.2 | Repeat 5 more times | Still one row in `mcp_card_checkout`, one in `user_order`, **one** Session in the Stripe dashboard |
| 3.3 | Same `K`, different bundle | **409** `IDEMPOTENCY_KEY_CONFLICT`, no new Session |
| 3.4 | Same `K`, different `quote_reference` | **409** — the quote is part of the identity |
| 3.5 | Same `K`, different `related_search` | **409** |
| 3.6 | New key, same bundle+quote | **200**, a second independent checkout |
| 3.7 | Two simultaneous calls with one key (`seq 2 \| xargs -P2`) | Exactly one `mcp_card_checkout` row, one `user_order`, one Stripe Session; the loser gets 200-replay or 409 |
| 3.8 | Use the same key string on the **wallet** endpoint | Independent purchase — operations do not collide |

Concurrency helper:

```bash
K=$(newkey)
seq 2 | xargs -P2 -I{} curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST "$QA/api/v1/mcp/user/bundle/card/checkout" \
  -H "Authorization: Bearer $TOKEN" -H "X-Device-Id: qa-device-1" \
  -H "Idempotency-Key: $K" -H "Content-Type: application/json" \
  -d "{\"bundle_code\":\"$BUNDLE\",\"quote_reference\":\"q-conc\",\"related_search\":{\"region\":null,\"countries\":[]}}"
```

---

## 4. Input rejection — nothing about money is caller-supplied

Each of these must return **400/422**, create nothing, and reach no Stripe call:

```bash
for FIELD in '"amount":1' '"price":0.01' '"tax":0' '"final_price":5' '"currency":"EUR"' \
             '"payment_type":"Card"' '"payment_method_id":"pm_card_visa"' \
             '"card_number":"4242424242424242"' '"cvc":"123"' '"stripe_token":"tok_visa"' \
             '"user_id":"..."' '"order_id":"..."' '"access_token":"x"' '"promo_code":"FREE"'; do
  curl -s -o /dev/null -w "$FIELD -> %{http_code}\n" \
    -X POST "$QA/api/v1/mcp/user/bundle/card/checkout" \
    -H "Authorization: Bearer $TOKEN" -H "X-Device-Id: qa-device-1" \
    -H "Idempotency-Key: $(newkey)" -H "Content-Type: application/json" \
    -d "{\"bundle_code\":\"$BUNDLE\",\"quote_reference\":\"q\",$FIELD}"
done
```

| # | Step | Expected |
|---|---|---|
| 4.1 | All of the above | 400 or 422 every time |
| 4.2 | Missing `Idempotency-Key` | **400** |
| 4.3 | Key of 31 chars / 129 chars / containing `/` | **400** |
| 4.4 | Missing `X-Device-Id` | 400/422 |
| 4.5 | No bearer token | 401/403 |
| 4.6 | Anonymous session token | **401** |
| 4.7 | `X-Currency: EUR` | **400** `MCP_UNSUPPORTED_CURRENCY`, nothing created |
| 4.8 | `X-Currency: usd` (lower case) | **200** — normalized |
| 4.9 | Unknown / inactive `bundle_code` | **400**, no Session created |
| 4.10 | `quote_reference` of 129 chars, or containing `<script>` | 400/422 |

---

## 5. Ownership and information leakage

| # | Step | Expected |
|---|---|---|
| 5.1 | `status <ref>` with `TOKEN_B` | **404** |
| 5.2 | `status 11111111-2222-3333-4444-555555555555` with `TOKEN_B` | **404**, **byte-identical body** to 5.1 |
| 5.3 | `status <ref>` with no token | 401/403 |
| 5.4 | `status ../../etc/passwd`, `status "a"x65` | 400/404/422, never 500 |
| 5.5 | Inspect any status response | No `cs_...`, no `pi_...`, no `client_secret`, no `checkout_url` |

---

## 6. Webhook integrity

| # | Step | Expected |
|---|---|---|
| 6.1 | `curl -X POST $QA/api/v1/callback/payment-webhook -H "stripe-signature: t=1,v1=forged" -d '{}'` | **400**, nothing changes, no row in `mcp_stripe_webhook_event` |
| 6.2 | Send a valid event body with **no** signature header | **400** |
| 6.3 | In Stripe dashboard → the `checkout.session.completed` event → **Resend** | Status stays `COMPLETED`; `user_profile` count still **1**; `create_reseller_order` not called again |
| 6.4 | Resend twice more | Still exactly one eSIM |
| 6.5 | `select count(*) from mcp_stripe_webhook_event where event_id='<evt_...>'` | **1** |
| 6.6 | Replay a `payment_intent.canceled` for an already-completed intent | Status remains `COMPLETED` (never demoted) |
| 6.7 | Confirm a LEGACY card payment's `payment_intent.succeeded` still provisions via the legacy path | Unchanged; no row appears in `mcp_stripe_webhook_event` |

Stripe CLI (optional, test mode):

```bash
stripe listen --forward-to "$QA/api/v1/callback/payment-webhook"
stripe events resend <evt_id>
```

---

## 7. Failure and edge paths

| # | Step | Expected |
|---|---|---|
| 7.1 | Checkout, pay with declined card `4000 0000 0000 0002` | Stripe blocks it; checkout stays `PENDING`; no eSIM |
| 7.2 | Checkout, then abandon the page until the session expires (set `MCP_CARD_SESSION_EXPIRY_MINUTES=30` and wait, or expire it from the dashboard) | `status` → `EXPIRED`, `next_action:"START_NEW_CHECKOUT"`, no eSIM. Works whether or not `payment_intent.canceled` is subscribed — the status endpoint also derives expiry from `expires_at` |
| 7.3 | Retry the **same** Idempotency-Key after expiry | Replays the terminal result. **A new checkout requires a NEW Idempotency-Key** (and a fresh quote — the price may have moved) |
| 7.4 | New key after expiry | **200**, a new checkout |
| 7.5 | Click "cancel" on the hosted page | Returns to `MCP_CARD_CANCEL_URL`; checkout stays `PENDING` until it expires; no eSIM |
| 7.6 | 3DS card `4000 0025 0000 3155`, complete authentication | Provisions exactly once |
| 7.7 | 3DS card, **abandon** authentication | No provisioning |

### 7.8 Paid-but-not-provisioned (the important one)

Temporarily point the eSIM Hub at an unreachable host, then pay with `4242...`:

- `status` → **`AMBIGUOUS`**, `next_action:"CONTACT_SUPPORT"`, `provisioned:false`
- `mcp_card_checkout.failure_code = 'PROVISIONING_FAILED'`, `paid_at` is set
- **No automatic refund, no automatic retry, no second charge**
- Resending the webhook does **not** re-provision — this needs human reconciliation
- Restore the hub afterwards

---

## 8. Post-run audit

```sql
-- one row per checkout, states as expected
select status, count(*) from public.mcp_card_checkout group by status;

-- no duplicate provisioning anywhere
select order_id, count(*) from public.user_profile
 where user_order_id is not null group by order_id having count(*) > 1;   -- expect 0 rows

-- webhook ledger: one row per Stripe event id
select event_type, status, count(*) from public.mcp_stripe_webhook_event
 group by event_type, status;

-- no secrets were persisted
select count(*) from public.mcp_card_checkout
 where checkout_url like '%secret%' or stripe_session_id like '%secret%';  -- expect 0
```

Logs must show `mcp.card` / `mcp.card.webhook` lines with:
- fingerprints only (12 hex chars) for users and keys,
- **no** `sk_`, `rk_`, `whsec_`, `_secret_`, no raw Idempotency-Key, no full Stripe payload.

```bash
grep -E "sk_(test|live)_|rk_(test|live)_|whsec_|_secret_" <logfile> && echo "LEAK - investigate" \
  || echo "no credential-shaped strings in logs"
```

---

## 9. Legacy regression gate (run with the card flag ON)

| # | Step | Expected |
|---|---|---|
| 9.1 | Legacy card purchase in the mobile/web app | Unchanged: PaymentIntent sheet, same response fields, provisioning on `payment_intent.succeeded` |
| 9.2 | Legacy wallet purchase | Unchanged |
| 9.3 | Wallet top-up by card | Unchanged |
| 9.4 | MCP wallet purchase | Unchanged |
| 9.5 | Catalogue / auth / order history | Unchanged |
| 9.6 | Diff `/openapi.json` against the pre-deploy copy | Only the two new MCP card paths and the `McpCard*` schemas are added; **no existing path or schema changes** |

```bash
curl -s "$QA/openapi.json" | python -m json.tool > after.json
diff <(python -c "import json;print('\n'.join(sorted(json.load(open('before.json'))['paths'])))") \
     <(python -c "import json;print('\n'.join(sorted(json.load(open('after.json'))['paths'])))")
```

---

## 10. Sign-off checklist

- [ ] Migration applied; all seven verification queries pass
- [ ] `STRIPE_SECRET_KEY` confirmed to be a **test** key (`livemode:false`)
- [ ] Happy path provisions exactly one eSIM
- [ ] Redirect to `success_url` alone never marks anything paid
- [ ] Duplicate/resent webhooks never provision twice
- [ ] Same key + same request replays; changed request conflicts
- [ ] Concurrent calls create one Session and one order
- [ ] Foreign `payment_reference` is indistinguishable from a missing one
- [ ] Forged webhook signature rejected with 400
- [ ] Paid-but-unprovisioned lands in `AMBIGUOUS` with no auto-refund/retry
- [ ] No Stripe secret in any response, log or database row
- [ ] Legacy card, wallet, top-up, MCP wallet, catalogue and auth all unchanged
- [ ] OpenAPI diff shows additions only
