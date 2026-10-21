# MS-5079: users page and user detail admin API

Story: MS-5079 (users list with ban status, ban and unban audited server-side, devices, wallet and referral sub-lists). Bugs fixed: MS-5067, and the users part of MS-5071. Portal counterpart: `docs/adminDocs/MS-5080-users.md` in the admin portal repo. Branch: `TEOS-139-be-planning-for-changes`. Date: 2026-10-21.

Status: done and tested with mocks. Nothing is committed. The SQL file was written and not run.

## No current flow changed

No existing flow or behavior of the backend changed.

- All code is in new admin files, plus the router `app/api/v1/admin/users.py`, which already existed and held only the lookup route (kept as it was, above the `/{id}` routes).
- No existing service, router, repository, schema, model, `main.py` or shared admin file was edited. The wave-1 device repository and service (`app/repo/admin/device_repo.py`, `app/services/admin/device_service.py`) are reused read-only.
- The mobile app's sign-in path is untouched. Bans are written in Supabase Auth exactly as the portal wrote them before (`ban_duration`), so GoTrue keeps refusing a banned user's sign-in and token refresh as before.

## Endpoints

All under `/api/v1/admin/users`. Lists use `page` (1-based), `page_size` (at most 100) and the standard list envelope. Any undeclared query parameter returns 422. `{id}` must be a UUID (422 otherwise).

| Method and path | Permission | Request | Success | Errors (`developerMessage`) |
|---|---|---|---|---|
| GET `` | `admin.users.read` | `search` (email, literal, ≤ 100 chars), `ban_status=all\|banned\|active` | 200 `{data:[{id,email,first_name,last_name,msisdn,referral_code,email_verified,phone_verified,should_notify,role,banned_until,is_banned,created_at,last_sign_in_at}], total_count, facets:{all,banned,active}}` | 401, 403, 422 |
| GET `/lookup` | `admin.users.read` | MS-5077, unchanged | | |
| GET `/{id}` | `admin.users.read` | none | 200 the row above + `wallet:{amount,currency}` or `wallet:null` | 404 `user_not_found`, 422 |
| POST `/{id}/ban` | `admin.users.ban` | `{hours 1..87600 (default 8760), reason 1..500 (not blank)}` | 200 `{user_id,email,banned_until,hours_banned}` | 404, 409 `{"code":"already_banned","banned_until":…}`, 422, 502 `AUTH_SERVICE_UNAVAILABLE` / `AUDIT_FAILED` |
| POST `/{id}/unban` | `admin.users.ban` | `{reason}` | 200 `{user_id,email}` | 404, 409 `not_banned`, 422, 502 |
| GET `/{id}/devices` | `admin.devices.read` | paging | 200 device rows (no `fcm_token`), `total_count` | 401, 403, 422 |
| GET `/{id}/wallet-transactions` | `admin.users.read` | paging | 200 `{data:[{id,wallet_id,amount,status,source,currency,created_at}], total_count}` | 401, 403, 422 |
| GET `/{id}/referrals` | `admin.referrals.read` | `search` (referral code), `from_email`, `date` (YYYY-MM-DD), paging | 200 rows of `admin_referral_transactions` (shape owned by MS-5056), `total_count` | 401, 403, 422 |

Notes:

- The list and the detail go through one SQL function, `admin_search_users`, which joins `users_copy` with `auth.users`. The ban state, the role and the dates come with the rows; there is no GoTrue call per row and no list of ids in a request (MS-5067). `is_banned` is true only while `banned_until` is in the future: a lapsed ban counts as active in the rows, the filter and the facets, so `facets.banned + facets.active == facets.all`.
- Facets count the rows matching the search, whatever the `ban_status` filter says.
- The rows are ordered `auth.users.created_at desc, id desc`, so paging never repeats or skips a user.
- `first_name`, `last_name`, `msisdn`, `referral_code`, `email_verified`, `phone_verified` and `should_notify` are read from `users_copy.metadata`, which is what the portal spread into the rows before.
- Devices reuse the wave-1 `DeviceAdminService.list(page, user_id=…)`; the response is the same as `GET /devices?user_id=`.
- `GET /{id}/referrals` calls `admin_referral_transactions` by name with `p_user_id, p_search, p_from_email, p_date, p_limit, p_offset`. The function itself belongs to the Referrals story (`090_wallet_referrals.sql`). The repository accepts either `{data,total}` or `{items,total_count}` from it.

### Ban and unban

| Step | Ban | Unban |
|---|---|---|
| 1 | read the user through `admin_search_users` (404 when unknown) | same |
| 2 | 409 `already_banned` when `is_banned` (with the current `banned_until`) | 409 `not_banned` when not banned (a lapsed ban is not a ban) |
| 3 | GoTrue `auth.admin.update_user_by_id(id, {"ban_duration": "<hours>h"})` | same with `"none"` |
| 4 | `AuditService.log` → `admin_write_audit`: table `auth.users`, operation `BAN`, record the user id, details `{user_id, user_email, hours_banned, ban_duration, banned_until, banned_at, rows_affected:1}` plus `ip_address`, `reason` and `client_reported:false` added by the SQL function | operation `UNBAN`, details `{…, hours_banned:null, ban_duration:"none", previous_banned_until, unbanned_at}` |

- GoTrue failure in step 3: 502 `AUTH_SERVICE_UNAVAILABLE`, nothing audited. A GoTrue 404 becomes 404.
- Audit failure in step 4: the ban state is put back (ban: `ban_duration: "none"`; unban: `"<remaining seconds>s"` until the previous `banned_until`, skipped when it already passed) and 502 `AUDIT_FAILED` is returned. A failure of that compensation is logged; the response is still 502.
- The actor comes from the token and the IP from the request headers; the reason is the body's `reason`. The portal no longer writes audit rows or reports its own IP (MS-5071).
- The check in step 2 and the write in step 3 are not atomic. Two admins banning the same user at the same moment can both get 200 (GoTrue has no conditional update); the second one simply re-applies the ban and both actions are audited. MS-5079 edge case "two admins ban the same user → one 200, one 409" holds whenever the first ban is visible before the second request reads the user.
- Legacy rows written by the browser (`Ban User`, `Unban User`) stay readable; the audit log maps them to `BAN` / `UNBAN` (MS-5048).

## Files

### New

| File | Purpose |
|---|---|
| `app/schemas/admin/users.py` | `AdminUserBan` (hours 1..87600 default 8760, reason trimmed, not blank, ≤ 500, extra forbidden), `AdminUserUnban`, `AdminUserRow`, `AdminUserDetail`, `AdminUserWallet`, `AdminUserBanResult`, `AdminUserUnbanResult`, `AdminWalletTransaction`, `AdminUserReferral`, `UserBanStatus` |
| `app/repo/admin/user_repo.py` | `UserAdminRepo` (`search`, `get`, `wallet_transactions`, `referrals`, all RPCs), `UserWalletAdminRepo.for_user` |
| `app/services/admin/user_service.py` | List, detail with wallet, devices, wallet transactions, referrals, ban and unban with audit and compensation |
| `migrations/admin/080_users.sql` | `admin_search_users`, `admin_wallet_transactions` |
| `tests/admin/api/test_users_api.py` | HTTP contract (37 tests) |
| `tests/admin/services/test_user_service.py` | Service, schemas and repositories (39 tests) |
| `adminDocs/MS-5079-users.md` | This record |

### Filled (already existed, mounted, held the lookup only)

| File | Change |
|---|---|
| `app/api/v1/admin/users.py` | The seven routes above, under the untouched lookup line |

## SQL

`migrations/admin/080_users.sql`, idempotent, service_role-only EXECUTE, never run:

- `admin_search_users(p_search text, p_ban_status text, p_limit int, p_offset int, p_id uuid)` → `{data, total, facets:{all,banned,active}}`. SECURITY DEFINER because it reads `auth.users`. Literal `ILIKE` on email with `\`, `%` and `_` escaped. AD422 for a bad `p_ban_status`, `p_limit` outside 1..100 or a negative offset.
- `admin_wallet_transactions(p_user_id uuid, p_limit int, p_offset int)` → `{data, total}`. Same rows as the legacy `get_wallet_transactions_with_count` for one user, with the true total (the legacy `count` was the page length) and a stable order. `p_user_id` is required.

**Until it is applied, `GET /users`, `GET /users/{id}`, ban, unban and the wallet tab return 502.** `GET /users/{id}/referrals` also needs `admin_referral_transactions` from `090_wallet_referrals.sql` (Referrals story). The audit rows of ban and unban use `admin_write_audit` from `050_admin_audit_helpers.sql`.

Performance note for the review: the list filters and orders on `auth.users` columns. With many users, an index on `auth.users (created_at desc)` is used by the ordering; Supabase manages `auth.users`, so no index is added here. MS-5079 test case 18 (page 1 under 300 ms over 100k users) can only be checked on a database.

## Tests

```powershell
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m pytest tests/admin/api/test_users_api.py tests/admin/services/test_user_service.py -q -p no:cacheprovider
```

76 passed. They cover:

- the list: rows, `total_count`, facets, the default `ban_status=all` and page 1; 422 for `page_size=101`, `ban_status=foo`, an unknown parameter, `page=0`, a 101-character search; 401 without a session; the lookup still answered before the `/{id}` route;
- the detail with and without a wallet, 404, 422 for a non-UUID id;
- ban: body and audit context (actor, IP from `X-Forwarded-For`, trimmed reason), 1 and 87600 hours accepted, 409 `already_banned` with `banned_until`, 404, 502, 422 for `hours=0`, `87601`, `1.5`, a missing, empty, blank or 501-character reason and an extra field; 403 for a regular admin; 401;
- unban: reason passed, 409 `not_banned`, 422 for a missing or empty reason and an extra field, 403;
- sub-lists: devices (403 without `admin.devices.read`), wallet transactions (422 on an unknown parameter), referrals with `search`, `from_email` and `date` (422 on a bad date, 403 without `admin.referrals.read`);
- the service: GoTrue called with `"<hours>h"` / `"none"`, the audit row's table, operation and details, `previous_banned_until`, nothing touched on 409, GoTrue 404 and 5xx mapping, audit failure → compensation (`"none"` after a ban, `"<seconds>s"` after an unban) and 502, a failed compensation still 502;
- the repositories: RPC names and parameters, blank search sent as NULL, a wrapped scalar result, both result spellings, the wallet query.

Full admin suite result: see the end of this file.

## Bugs fixed

| Bug | Fix |
|---|---|
| MS-5067: users pages unstable (no ORDER BY) and the Banned filter missed users beyond 20 000 | One SQL function with a stable order and the ban state joined from `auth.users`; no id list in any request |
| MS-5071 (users): ban and unban audited from the browser with a self-reported IP | Audited server-side with the actor from the token and the IP from the request; a ban whose audit row cannot be written is lifted again |

## Deviations and review notes

- MS-5079 says `/users/{id}` is "RPC + `UserWalletRepo`". The wallet is read by a new admin repository (`UserWalletAdminRepo`) on `user_wallet`; the existing consumer `UserWalletRepo` is not touched.
- The row carries `email_verified`, `phone_verified` and `should_notify` in addition to the plan's fields, because the Users page shows those three columns today.
- `admin_wallet_transactions` lives in `080_users.sql` (this story owns the file), not in `090_wallet_referrals.sql` as the plan's migration table says; `admin_referral_transactions` stays in `090` with the Referrals story.
- 409 `already_banned` carries the current `banned_until` as an extra field, so the portal can show it.
- MS-5079's test cases 6, 18, 19 and 20 (20 000+ auth users, timing, legacy rows, GoTrue refusing a banned session) need a database and are not covered by mocks.

## Full admin suite

`tests/admin`: see the hand-over report (run on 2026-10-21 with the other wave-2 tracks' tests included).
