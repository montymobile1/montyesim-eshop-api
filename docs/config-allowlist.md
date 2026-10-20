# /api/v1/app/configurations: allowlist (as built)

`GET /api/v1/app/configurations` returns only the `app_config` keys listed in
`CLIENT_CONFIG_KEYS` (`app/services/app_service.py`), followed by the computed entries. Every
other key is private by default; a key added to `app_config` is never returned unless it is
added to the allowlist in code.

## Response
For each allowlisted row, the response contains `{"key": <key uppercased>, "value": <value>}`, as before.
- Rows come in the order the database returns them, as before. Rows not on the allowlist are skipped.
- The computed entries follow, in this order:
  1. `CATALOG.BUNDLES_CACHE_VERSION` (from the `APP_CACHE_KEY` row, when present)
  2. `WHATSAPP_NUMBER` (env `WHATSAPP_NUMBER`)
  3. `SUPABASE_BASE_URL` (env `SUPABASE_URL`)
  4. `SUPABASE_BASE_ANON_KEY` (env `SUPABASE_ANON_KEY`)
  5. `default_currency` (env `DEFAULT_CURRENCY`)
- `value` may be null: a computed entry whose env var is unset. `app_config.value` itself is NOT NULL.
- The guard against secret-shaped names applies even if the allowlist says otherwise: a key that is a secret by
  `is_secret_key` is never returned. The one approved exception is `SUPABASE_BASE_ANON_KEY`, the public anon key.
- If `app_config` can't be read, the endpoint returns 503. It never falls back to env values.
- There is no cache: a changed value is returned on the next call.

## Allowlist and client inventory
Checked on 2026-10-20 against these repos, cloned read-only from github.com/montymobile1:
- `montyesim-eshop-app` (`main` 300d24c, including history back to the first commit)
- `montyesim-eshop-web` (`main` ef744e0, including history)
- `montyesim-eshop-admin` (`main` fadeaf0)
- `montyesim-eshop-SDK-iOS`: tags 1.0.0–1.0.5, release binaries inspected
- `montyesim-eshop-SDK-Android`: tags 1.0.0–1.0.1, AAR inspected

In the table:
- Mobile lines are in `lib/data/services/app_configuration_service_impl.dart`.
- Web lines are in `src/redux/reducers/currencyReducer.jsx`.
- The SDKs are compiled binaries, so only the class or symbol is given.

| Key | Read by |
|---|---|
| `ALLOWED_PAYMENT_TYPES` | mobile :134; web :59; Android `getPaymentTypeList`; iOS 1.0.3+ |
| `LOGIN_TYPE` | mobile :123; web :60; Android; iOS 1.0.4 |
| `OTP_EXPIRATION_TIME` | web :63 |
| `TRANSACTION_EXPIRY_TIME` | web :64 |
| `REFERRAL_CODE_PERCENTAGE` | mobile :157 |
| `REFERRAL_CODE_AMOUNT` | web :58 (read but never used); mobile builds de83d9f–ed765b5; iOS 1.0.3–1.0.5 |
| `CASHBACK_DISCOUNT` | Android `AppConfigurationServiceImpl.getCashbackDiscount` |
| `COMPANY_SUPPORT_PHONE` | web :65 (footer) |
| `COMPANY_SUPPORT_EMAIL` | web :66 (footer `mailto:`) |
| `TOP_COUNTRIES_COUNT` | web :67 |
| `DEFAULT_CURRENCY` (row) / `default_currency` (computed) | mobile :84; web :57; Android; iOS |
| `WHATSAPP_NUMBER` (row and computed) | mobile :113; web :62; Android; iOS |
| `CATALOG.BUNDLES_CACHE_VERSION` | mobile :77; web :61; Android; iOS |
| `SUPABASE_BASE_URL` | mobile `startup_view_model.dart:138`; Android `OnSupabaseConfigReady`; iOS `ESimSupabaseConfigDelegate` |
| `SUPABASE_BASE_ANON_KEY` | mobile `startup_view_model.dart:139`; Android; iOS |

- **Why `DEFAULT_CURRENCY` and `WHATSAPP_NUMBER` are allowlisted as rows:** a row can sit next to the computed
  entry of the same name. Mobile takes the first match (the row) and web takes the last (the computed entry),
  so dropping the row would change what mobile shows.
- **Why the computed names are also allowlisted:** for the same reason, a row with one of those names keeps
  being returned.

## Constraints from old builds
- **Original computed keys:** the oldest mobile builds (efd2c8c–de83d9f) match the original five computed keys
  exactly and crash if one is missing. They are kept.
- **Lowercase keys already unmatched:** mobile builds from 2025-08/09 look up lowercase `login_type` and
  `allowed_payment_types` with exact matching. The backend already sent these uppercased before this change, and
  still does.
- **Values must stay strings or null:** mobile fails to parse the whole response on any other type.
- **Admin:** it doesn't call this endpoint. It reads and writes `app_config` directly.

## Secrets: `get_secret`, environment only
`app/config/helper.py`:
- **Secret rule (`is_secret_key`):** names ending `_KEY`, `_SECRET`, `_PASSWORD`, `_TOKEN` or `_USERNAME`, plus
  `RESELLER_ID`. `APP_CACHE_KEY` and `CACHE_KEY` are not secrets.
- **`get_secret(key)`:** reads the environment only. It never writes to `app_config`, never logs, and raises
  `MissingSecretError` (naming the key) when the variable is unset or empty.
- **`get_setting(key, default)`:** a non-secret setting from `app_config`, auto-seeded from the environment as
  before. Concurrent first reads store one row. It raises `ValueError` on a secret-shaped key. `get_config` is an
  alias of it.

Secrets read through `get_secret`:

| Key | Read at |
|---|---|
| `ESIM_HUB_API_KEY` | `app/config/config.py` `esim_hub_service_instance` |
| `DCB_HUB_API_KEY` | `app/services/integration/hub_dcb_service.py` `HubDcbService.__init__` |
| `RESELLER_ID` | `app/services/integration/esim_hub_service.py` `get_exchange_rates`; `app/services/callback_service.py` (2 places) |

These are DB-backed (`get_setting`) and not returned: `WALLET_TOP_UP_ALERT_RECIPIENTS`, `SUPPORT_EMAIL`,
`SMTP_SENDER`, `SMTP_SENDER_NAME`, and every other setting not on the allowlist.

`tests/config/test_secret_source_guard.py` fails if any `get_config` or `get_setting` call in `app/` names a
secret-shaped key.
