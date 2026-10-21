# MS-5056: referrals list and export admin API

Story: MS-5056 (referrals page: paged referral transactions and a server-generated export). Portal counterpart: `docs/adminDocs/MS-5057-referrals.md` in the admin portal repo. Branch: `TEOS-139-be-planning-for-changes`. Date: 2026-10-21.

Status: done and tested with mocks. Nothing is committed. The SQL file was written and not run.

## No current flow changed

No existing flow or behavior of the backend changed.

- All code is in new admin files, plus the router file that was already mounted (`referrals.py`, which was empty).
- No existing service, router, repository, schema, model, `main.py`, `requirements.txt`, `.env.example` or shared admin file was edited.
- The legacy SQL functions `get_referral_transactions_with_count` and `export_referrals_data` (`supabase_ddl.sql`) are untouched; the unmigrated portal pages keep calling them until the clean-up story drops them. The new function has a new name.
- `openpyxl==3.1.5` was already in `requirements.txt` (added by the foundation for this story) and is installed in `.venv`. Nothing was installed.

## Endpoints

All under `/api/v1/admin`. The list uses `page` (1-based), `page_size` (at most 100) and the standard list envelope. Any undeclared query parameter returns 422.

| Method and path | Permission | Request | Success | Errors (`developerMessage`) |
|---|---|---|---|---|
| GET `/referrals` | `admin.referrals.read` | `user_id` (uuid), `search` (part of the referral code, literal, ≤ 100), `from_email` (part of the referred user's email, literal, ≤ 255), `date` (`YYYY-MM-DD`) | 200 `{data:[row], total_count}` | 401, 403, 422 (bad uuid or date, `page_size=101`, unknown parameter), 502 |
| POST `/referrals/export` | `admin.referrals.export` (super admin only) | JSON `{user_id?, search?, from_email?, date?}` (same rules, `extra` forbidden) | 200, body = the `.xlsx` file, `Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, `Content-Disposition: attachment; filename="referrals-<UTC yyyymmdd-hhmmss>.xlsx"`, `X-Export-Row-Count` | 401, 403 (a regular admin), 422 `{"code":"too_many_rows","total":n,"cap":c}` or invalid filters, 502 (database or audit failure; no file is sent) |

Row of the list (`AdminReferral`): `{id, created_at, payment_time, referral_code, amount, currency, modified_amount, order_status, status, referrer:{id,email}|null, referred:{id,email}|null}`.

- `id`, `created_at`, `payment_time`, `referral_code`, `currency`, `modified_amount`, `order_status` come from `user_order` (the order paid with the code; `order_status` is always `success`).
- `amount` (the reward) and `status` (`pending | completed | failed`) come from `promotion_usage` (joined on `promotion_usage.order_id = user_order.id`).
- `referrer` is the owner of the code who receives the reward (`promotion_usage.user_id`, email from `users_copy`): the legacy "To User Email" column.
- `referred` is the user who bought with the code (`user_order.user_id`; email = `promotion_usage.referred_to`, else `users_copy`): the legacy "From User Email" column.

Filters keep the meaning of the legacy functions (the latest versions, with `p_from_email` and `p_referral_date`): `user_id` matches `user_order.user_id` (what the user detail tab shows today), `search` the referral code, `from_email` the referred user's email (`promotion_usage.referred_to`), `date` the calendar day of `user_order.created_at`. All are combined with AND; blank texts are ignored; `%`, `_` and `\` are matched literally. Order: `created_at desc, id desc`.

The Users story calls the same function from `GET /users/{id}/referrals` with the same parameter names.

### Export (v1, synchronous)

As decided in the migration plan and recorded in the Jira comment, v1 builds the file in the request:

1. The first chunk (1000 rows) is read through `admin_referral_transactions`; its `total` is compared with `ADMIN_EXPORT_ROW_CAP` (default 50000). Above the cap: 422 `too_many_rows` with `total` and `cap`, nothing else is read and nothing is audited.
2. The remaining chunks are read 1000 rows at a time, at the same stable order.
3. The workbook is written with `openpyxl` in write-only mode (one sheet, "Referral Transactions", a header row then one row per transaction) into memory.
4. Every text cell starting with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading apostrophe, so spreadsheet programs show it as text instead of running it as a formula. Numbers (amounts) are written as numbers and are not touched.
5. One audit row is written through `AuditService.log` (`admin_write_audit`): `table_name = user_order`, `operation = EXPORT`, actor from the token, IP from the request, `operation_details = {rows_affected, filters: {the filters that were set}, file_name}`. If that write fails the request answers 502 and no file is sent.
6. The bytes are returned as a `StreamingResponse` attachment.

Columns: Refer Code, Reward Amount, Currency, Modified Amount, From Email (referred), To Email (referrer), Order Number, Order Status, Usage Status, Referral Date, Purchased Date. Compared with the browser-built export: the amount and its currency are two cells (numbers stay numbers) instead of one `"EUR 2.5"` text.

`ADMIN_EXPORT_ROW_CAP` is read with `os.getenv` in `referral_service.py` at each export (`export_row_cap()`), so it can be changed without a restart of the code. It is already documented in `.env.example` (line `ADMIN_EXPORT_ROW_CAP=50000`, added by the foundation); no edit to `.env.example` or `app/config` was needed.

`AuditService` already covers an EXPORT row for a non-table action (`log(ctx, table, operation, rows_affected=..., **details)`): no addition to the audit service was needed.

## Files

### New

| File | Purpose |
|---|---|
| `app/schemas/admin/referrals.py` | `AdminReferralFilters` (+ `referral_filters` query dependency, `rpc_params`, `audit_filters`), `AdminReferralExportRequest` (extra forbidden), `AdminReferralUser`, `AdminReferral` |
| `app/repo/admin/referral_repo.py` | `ReferralAdminRepo.search(filters, limit, offset)` on `admin_referral_transactions` |
| `app/services/admin/referral_service.py` | `ReferralAdminService.list` / `.export`, `export_row_cap`, `neutralise`, `export_file_name`, `ReferralExport` |
| `migrations/admin/090_wallet_referrals.sql` | `admin_referral_transactions` and two indexes (see SQL) |
| `tests/admin/api/test_referrals_api.py` | HTTP contract (23 tests) |
| `tests/admin/services/test_referral_service.py` | Service, workbook content, cap, chunks, audit, repository (31 tests) |
| `adminDocs/MS-5056-referrals.md` | This record |

### Filled (already existed, was empty and mounted)

| File | Change |
|---|---|
| `app/api/v1/admin/referrals.py` | `GET ""` and `POST "/export"` |

## SQL

`migrations/admin/090_wallet_referrals.sql` (idempotent, service_role-only EXECUTE):

- `admin_referral_transactions(p_user_id uuid, p_search text, p_from_email text, p_date date, p_limit integer, p_offset integer) -> jsonb {data, total}`. Rewrite of the two legacy functions under a new name: same row set (successful orders with a referral code joined to their `promotion_usage` row), escaped `ILIKE` patterns, stable order, `p_limit` 1..1000 (`AD422` otherwise). Difference from the legacy functions: `users_copy` is LEFT JOINed, so an order whose referrer no longer exists in `users_copy` is still listed (email null) instead of disappearing.
- Indexes `admin_promotion_usage_order_id_idx` on `promotion_usage(order_id)` and the partial `admin_user_order_referral_idx` on `user_order(created_at desc, id desc) where referral_code is not null and order_status = 'success'`.
- Needs `000_schema_drift.sql` first (`promotion_usage.order_id` and `referred_to`), like the legacy functions do.
- **Not in this file**: the plan's migrations table lists `admin_wallet_transactions` in `090_wallet_referrals.sql` too. It is written by the Users story (MS-5079) in `080_users.sql`, where the rest of the user detail SQL lives. The file header says so.

Until the file is applied, `GET /referrals` and the export return 502 against a real database.

## Tests

```powershell
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m pytest tests/admin/api/test_referrals_api.py tests/admin/services/test_referral_service.py -q -p no:cacheprovider
```

54 tests, all pass. They cover:

- the list: filters and page passed to the service, the envelope (`totalCount`, `data.total_count`, the joined `referrer` / `referred`), defaults, 422 for `page_size=101`, `page=0`, an unknown parameter, an invalid or impossible date, a bad uuid, an overlong search, 401, 403 without `admin.referrals.read`, 502;
- the export: 200 with the xlsx content type, the attachment header, the row-count header and the bytes, the audit context (actor, IP), an empty body, 422 `too_many_rows` with `cap`, 422 for an invalid date, uuid, an unknown or extra field, 403 for a regular admin, 401, 502 without any attachment header;
- the service: the workbook read back with openpyxl (headers, one row per transaction, numbers kept as numbers), chunks of 1000 with increasing offsets, the cap (above → 422 and no audit, at the cap → allowed), the formula neutralisation on every text column, an empty export (headers only, `rows_affected` 0), an audit failure that propagates, `neutralise` and `export_row_cap` cases, the file name;
- the repository: the exact RPC name and parameter names, a list-wrapped or empty result, `AD422` mapped to 422.

The service file is skipped as a whole when `openpyxl` is missing (`pytest.importorskip`), with the reason in the output.

Full admin suite: see the end of this file.

## Deviations from the Jira text and open points

- **Synchronous export** (`StreamingResponse`, `Content-Disposition`, row cap, 422 `too_many_rows`) instead of the background job, the `admin_export` table, the private bucket, the polling route, the 429 "one export per admin" and the 24 h cleanup: decided in the migration plan and in the Jira comment on MS-5056. The job stays a follow-up story once the cap is hit in practice.
- Jira says `search` matches the referred email and `from_email` is the exact referrer email; the plan's catalog says `search` is the referral code. The plan was followed and the legacy meaning kept: `search` = part of the code, `from_email` = part of the referred user's email (`promotion_usage.referred_to`, a "contains" match, as `get_referral_transactions_with_count` does), `user_id` = `user_order.user_id`. Note for the team leader: in the legacy portal the "To User" dropdown fills `user_id`, which the SQL compares with the order's user, i.e. the buyer shown in the "From User Email" column, not the referrer shown in "To User Email". The API keeps that behavior so the data stays the same (and so `GET /users/{id}/referrals` shows what the user detail tab shows today). If the team prefers `user_id` to mean the referrer (`promotion_usage.user_id`), it is a one-line change in the SQL `WHERE` clause.
- `from_email` is not validated as an email address (Jira case 15): the portal fills it from the user dropdown and the legacy function accepted any text; a partial text is a valid filter.
- The row shape adds `payment_time`, `referral_code`, `modified_amount`, `order_status` to the Jira shape, because the portal page shows them (Order Number, Order Status, Usage Status, Purchased Date columns).
- Exporting the "amount" as a number plus a "Currency" column instead of the legacy `"EUR 2.5"` text; the "Modified Amount" column is a number too.
- The export response adds `X-Export-Row-Count`. It is not exposed through CORS (`main.py` is not editable), so the portal does not read it; `curl` does.

## Full admin suite

`tests/admin`: 900 passed (run on 2026-10-21, other wave-2 tracks' tests included). The OpenAPI guard `tests/mcp/test_legacy_unchanged.py -k "openapi or schemas or endpoint"`: 5 passed.
