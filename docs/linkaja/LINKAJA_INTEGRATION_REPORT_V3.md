# LinkAja integration report v3: alignment with the official spec

**Date:** 2026-10-01 (amount reconciliation revised the same day, see §1a)
**Spec:** "LinkAja Applink SNAP" interface specification v1.1 (2025-05-08), `link-aja/ISD20250508-LinkAja Applink SNAP V1.1.pdf`
**Input:** `docs/linkaja/LINKAJA_EMAIL_FINAL_CHECK.md` §4 (code-vs-PDF differences, 2026-09-29)
**Rule applied:** where the Postman collection and the PDF disagree, the PDF wins.

> No secrets appear in this file. Nothing was committed or pushed.

## 1. What changed

| # | Change | Spec | Files |
|---|---|---|---|
| 1 | Create payment sends `urlParams` (plural) with `type: "PAY_RETURN"`; `isDeeplink` configurable via `LINKAJA_URL_PARAM_IS_DEEPLINK` (default `N`) | p.2, p.11 | `service.py`, `config.py` |
| 2 | Status inquiry `serviceCode` `"54"` → `"55"` | p.17-18 | `service.py` |
| 3 | Access token cached in memory, refreshed 60 s before expiry (`expiresIn`), shared between service instances with the same credentials, safe across coroutines and event loops; a 401 on a service call drops the token and retries once | p.10 | `token_cache.py`, `service.py`, `client.py` |
| 4 | `POST /bi/applink/v1.0/debit/notify` added at the application root as an alias of `POST /api/v1/callback/linkaja`, same handler. No other route changed | p.14 | `app/api/v1/callback.py`, `app/main.py` |
| 5 | Inform Payment body parsed: `originalPartnerReferenceNo`, `originalReferenceNo`, `merchantId`, `latestTransactionStatus`, `amount{value,currency}`, `createdTime`, `finishedTime`, `additionalInfo{approvalCode, terminalId, terminalName, items[]}`. Amounts accepted with or without decimals. `merchantId` must be ours; the amount must equal the IDR amount **stored on the order at creation** (§1a) | p.14-16 | `service.py`, `dto.py`, `order_data.py`, `link_aja_callback_service.py` |
| 6 | Notify reply is exactly `{"responseCode":"2005600","responseMessage":"Successful","approvalCode":"<echoed>"}`; failures use §5.2 codes for service 56 (table below), never our API envelope | p.15-16, §5.2 | `link_aja_callback_service.py`, `exceptions.py` |
| 7 | Status response parsed (p.18); `PROVIDER_STATUS_MAP` = `00`→SUCCESS, `04`→REFUNDED, `06`→FAILED, `07`→NOT_FOUND. HTTP 404 / `4045501` on inquiry is tolerated and mapped to NOT_FOUND. NOT_FOUND and REFUNDED never provision and leave the order pending | p.18, p.25 | `service.py`, `dto.py`, `client.py` |
| 8 | Reserve-then-commit: the notify handler verifies, reconciles, claims the order (atomic pending→success) and replies; eSIM provisioning runs **after** the response on the existing `TaskExecutor` queue | p.7 | `link_aja_callback_service.py` |
| 9 | Notify signature verification per §5.1.3 (SHA256withRSA over `POST:<EndpointUrl>:<lowerhex(sha256(minify(body)))>:<X-TIMESTAMP>`) with LinkAja's public key from `LINKAJA_NOTIFY_PUBLIC_KEY` / `_PATH`. Key unset → every notify rejected. `EndpointUrl` defaults to the path LinkAja called, overridable with `LINKAJA_NOTIFY_ENDPOINT_URL`. `validate_for_callback` now requires the key; `LINKAJA_CALLBACK_SIGNATURE_HEADER` / `LINKAJA_SIGNATURE_ALGORITHM` are removed | p.23 | `signature.py`, `config.py`, `service.py` |
| 10 | `urlParams[0].url` is now our notify URL, `LINKAJA_NOTIFY_URL` (LinkAja's answer); `LINKAJA_SUCCESS_URL` is no longer sent and no longer required | LinkAja email | `config.py`, `dto.py`, `service.py`, `user_service.py` |
| 11 | Reconciliation job: LinkAja does not retry the notify, so pending LinkAja orders aged 60 s - 24 h are polled every 120 s with Inquiry Status (serviceCode 55). A paid order goes through the same atomic pending→success claim as the notify, so it is provisioned exactly once; not found stays pending; failed/canceled/expired is recorded only while still pending. Started only when `LINKAJA_ENABLED=true` | p.17-18 | `link_aja_reconciliation_service.py`, `link_aja_callback_service.py`, `app/main.py` |

Also: new error name `LINKAJA_CALLBACK_INVALID` (`constants.py`, `locales/en.json`, `locales/ar.json`), `.env.example`
and `docs/linkaja/LINKAJA_SPEC_SUMMARY.md` updated. Card, Wallet, DCB and Stripe code untouched.

### 1a. Amount reconciliation against the stored quote (no migration)

The first cut recomputed the expected IDR amount from the *current* `LINKAJA_USD_TO_IDR_RATE`, so a rate
change while a payment was in flight would have rejected its notify. Now:

- `LinkAjaService.quote_amount()` produces the exact IDR string that `create_payment` will send, plus the
  rate, the USD minor units and a timestamp (`LinkAjaOrderData`).
- `UserBundleService.__handle_link_aja_payment` persists that quote on the order **before** calling LinkAja
  (a storage failure costs no provider call), creates the payment, checks that the amount echoed by
  `create_payment` equals the stored quote, then persists the reference.
- `LinkAjaCallbackService.__reconcile_amount` compares the notify amount with the stored quote only. The
  current rate is never consulted; an order without a stored quote is rejected (`4045613`), never
  provisioned.

**Where it is stored.** `user_order` has no spare column. The quote lives inside `bundle_data` (the order's
JSON snapshot of the `BundleDTO`) under the reserved key `linkaja_payment`. This is safe without a migration
because every reader of `user_order.bundle_data` (`user_service.py:603`, `callback_service.py:292`,
`dto_mapper.py:428`, the LinkAja callback) parses it with `BundleDTO.model_validate_json`, and `BundleDTO`
has `extra="ignore"`; nothing in the database parses the column. Tests assert the snapshot is otherwise
unchanged and still parses as a `BundleDTO`.

**Caveat.** `UserBundleService.assign` schedules a delayed rewrite of `bundle_data` on the promo-code path
(`__update_order_with_delay`, `user_service.py:112`). That task never runs today - it calls an `async`
function without awaiting it - but if it is ever fixed it must preserve the `linkaja_payment` key (or re-embed
it), otherwise promo-code LinkAja orders would lose their quote and their notify would be rejected (not
wrongly provisioned). Not changed here because it is shared assign code outside the LinkAja scope.

### Notify error codes (§5.2, service code 56)

| Situation | HTTP | `responseCode` | `responseMessage` |
|---|---|---|---|
| Success, duplicate, or non-success status recorded | 200 | `2005600` | Successful |
| Missing / invalid `X-SIGNATURE` or `X-TIMESTAMP`, bad signature | 401 | `4015600` | Unauthorized. Invalid signature |
| Unknown order, order not paid with LinkAja, LinkAja reference mismatch | 404 | `4045601` | Transaction Not Found |
| `merchantId` is not ours | 404 | `4045608` | Invalid Merchant |
| Amount or currency ≠ the quote stored on the order; order has no stored quote | 404 | `4045613` | Invalid Amount |
| Missing mandatory field | 400 | `4005602` | Invalid Mandatory Field {field} |
| Bad amount / currency format | 400 | `4005601` | Invalid Field Format {field} |
| Body not JSON | 400 | `4005600` | Bad Request |
| `LINKAJA_ENABLED=false` | 400 | `4005600` | Bad Request |
| Public key not configured, key unreadable | 503 | `5005601` | Internal Server Error (retry) |

## 2. How provisioning is triggered: before and after

**Before (v2).** `LinkAjaCallbackService.process_verified_callback` claimed the order and then
`await`ed `BundleService.buy_bundle(...)` inline, inside the HTTP request. The notify response was only sent
after the eSIM Hub round-trips finished. Under the p.7 flow, LinkAja would have been holding the reserved
payment for the whole provisioning time, and a slow Hub call could have made LinkAja time out and treat the
payment as not acknowledged. (In practice nothing was reachable: verification always raised.)

```
notify → verify (raised) → resolve order → claim → await buy_bundle → ack
```

**After (v3).** `handle_callback` → `verify_callback` (signature, body) → `process_verified_callback`:

```
notify → verify signature → parse body → resolve order → idempotency check
       → reconcile amount (vs. the quote stored on the order) → claim (UPDATE … WHERE payment_status='pending')
       → queue provision(order) on TaskExecutor → reply 2005600
                                                     ↓ (worker thread, after the response)
                                               asyncio.run(provision(order)) → BundleService.buy_bundle
```

Concretely, `LinkAjaCallbackService.__schedule_provisioning` builds a zero-argument task that runs
`asyncio.run(self.provision(order))` and hands it to `TaskExecutor.add_task(...)`, which is exactly what the
Stripe webhook does (`CallbackService.handle_payment_webhook`). The executor runs tasks on worker threads with no
event loop, hence `asyncio.run`. If the queue is full (`add_task` returns `False`), the task runs on a dedicated
daemon thread instead, because by then the order is already claimed as paid and must not be stranded.

The atomic claim is unchanged: `user_order_repo.update_by(where={"id": …, "payment_status": "pending"},
data={"payment_status": "success", …})`. Of N deliveries of the same notify exactly one matches the row and
schedules provisioning; the others get the same `2005600`. The claim happens **before** the response, so a retry
arriving while provisioning runs cannot provision again. `provision()` is a public coroutine so tests can run
exactly what was queued; it is not reachable from the network.

## 3. Test results

Command: `FCM_CONFIG_FILE=../local-dev/fcm-dummy.json venv/bin/python -m pytest tests -q`

| Run | Result |
|---|---|
| Baseline, before any change (2026-10-01) | `7 failed, 210 passed, 38 errors` |
| After the changes | `7 failed, 332 passed, 38 errors` |
| `tests/services/test_link_aja_payment.py` alone | `276 passed` (was 154) |

The 7 failures / 38 errors are the same pre-existing set (no network for the Supabase-backed services, stale
patch targets in `test_user_service.py`); none is in the LinkAja test file.

New or updated tests cover: the `urlParams`/`PAY_RETURN` body and configurable `isDeeplink`; `serviceCode 55`;
token reuse across calls and instances, refresh before expiry, no caching without a lifetime, single fetch under
concurrency, use from a second event loop, 401 → refresh-and-retry once; both notify routes through a FastAPI
`TestClient` (signed per §5.1.3, exact `2005600` body and key order, SNAP error bodies, POST-only, signature
bound to the path that was called); notify body parsing with the p.15-16 sample (every field, amounts with and
without decimals, each mandatory field, optional fields, wrong currency, wrong merchant, non-JSON); every status
code mapping for both the notify and the inquiry; 404/`4045501` staying pending; duplicate notify not
provisioning twice (fast path, lost race, and two deliveries through the route); provisioning happening only
after the response (queued task, then run on a thread); the full-queue fallback; signature verification with a
generated RSA pair: valid, tampered body, wrong key, missing header, wrong timestamp, key unset, key from file,
unreadable file, configured `EndpointUrl`, no logged signature; stored-quote reconciliation: the rate
changes between create and notify and the notify still succeeds (unit level, and end to end from `assign`
through the signed notify route), an amount at the new rate is rejected, reconciliation works with the rate
unset, an order without a stored quote is rejected, the quote is persisted before the provider call, a
sent-vs-stored mismatch fails the flow, `embed_order_data`/`read_order_data` round trip and leave the
`BundleDTO` snapshot readable.

## 4. Sandbox result (dev, 2026-10-01)

One create payment through the real service code (`LinkAjaService.create_payment`) with the spec body:

```
request urlParams: [{"url": "…", "type": "PAY_RETURN", "isDeeplink": "N"}]
create_payment -> PENDING  referenceNo=p1942150828NzDTywQvUU  appRedirectUrl=https://cdn-dev.linkaja.com/applink/payment?data=…
status inquiry (serviceCode 55) -> NOT_FOUND 07 "Transaction Not Found"   (HTTP 404 / 4045501, unpaid)
token cached: True   (the inquiry reused the token from the create call)
```

`urlParams` / `PAY_RETURN` is accepted by the dev sandbox (`responseCode 2005400`), as was the old Postman form.
`serviceCode 55` behaves like `54` for an unpaid transaction: both return `4045501`. A paid transaction has not
been observed, so the inquiry's positive response (p.18) and the notify have only been tested against the spec
samples.

## 5. Still waiting on LinkAja

| # | Item | Effect while missing |
|---|---|---|
| 1 | **LinkAja's public key** for the notify signature (`LINKAJA_NOTIFY_PUBLIC_KEY[_PATH]`), or confirmation of which key pair signs the notify. The `public-key.txt` in the credential pack is the pair of OUR private key, not LinkAja's | Every notify is rejected with `503 / 5005601`; no LinkAja payment can provision. Fail closed by design |
| 2 | **Answered:** the notify URL is sent in `urlParams[0].url` (`LINKAJA_NOTIFY_URL`). Both paths are served. Still open: the exact `EndpointUrl` they put in the string-to-sign (set `LINKAJA_NOTIFY_ENDPOINT_URL` if it is the full URL) | Set `LINKAJA_NOTIFY_ENDPOINT_URL` once known |
| 3 | **Test account** (dev wallet number, PIN/OTP, balance) and how to open the 5-minute `appRedirectUrl` in the dev environment, or a joint test session | The end-to-end flow cannot be exercised |
| 4 | **Retry policy, answered:** LinkAja does not retry; the reconciliation job covers lost notifies. Still open: whether the notify carries `latestTransactionStatus` `00` (p.14) or a pending status (p.7 diagram) | The code handles both readings |
| 5 | Confirmation that `urlParams`/`PAY_RETURN` and `serviceCode 55` are the production values (dev accepts both forms) | Low risk |
| 6 | Production base URL and production key exchange | Before go-live |

## 6. Deployment notes

- New settings: `LINKAJA_NOTIFY_PUBLIC_KEY` or `LINKAJA_NOTIFY_PUBLIC_KEY_PATH` (blocking), optional
  `LINKAJA_NOTIFY_ENDPOINT_URL`, optional `LINKAJA_URL_PARAM_IS_DEEPLINK` (default `N`).
- `LINKAJA_NOTIFY_URL` (required when enabled): the public URL of our notify endpoint, sent as `urlParams[0].url`.
  `LINKAJA_SUCCESS_URL` is no longer used.
- Reconciliation job (only with `LINKAJA_ENABLED=true`): `LINKAJA_RECONCILE_INTERVAL_SECONDS` (120),
  `LINKAJA_RECONCILE_MIN_AGE_SECONDS` (60), `LINKAJA_RECONCILE_MAX_AGE_SECONDS` (86400),
  `LINKAJA_RECONCILE_BATCH_SIZE` (100).
- Removed settings: `LINKAJA_CALLBACK_SIGNATURE_HEADER`, `LINKAJA_SIGNATURE_ALGORITHM` (ignored if still
  present in an `.env`).
- The notify amount is reconciled against the quote stored on the order at creation, so
  `LINKAJA_USD_TO_IDR_RATE` can be changed at any time. LinkAja orders created **before** this change (dev only)
  have no stored quote and their notifies will be rejected with `4045613`.
- `LinkAjaCallbackService` is a per-process singleton built on first request; configuration is read at that
  moment, so a settings change needs a restart.
- Refund (§4.5) is still not implemented; `approvalCode` from the notify/inquiry is now parsed and available
  for it.
