# MS-5091, MS-5046, MS-5048: settings, settings history and audit log

Stories: MS-5091 (settings list and bulk save), MS-5046 (settings history), MS-5048 (audit log). Portal counterparts: MS-5092, MS-5047, MS-5049. Bugs fixed: the settings and audit parts of MS-5071 and MS-5070. Branch: `TEOS-139-be-planning-for-changes`. Date: 2026-10-21.

Status: done and tested. Nothing is committed and no migration has been applied.

## No current flow changed

No existing flow or behavior of the backend changed. Every new line is in new admin files, except one agreed exception:

- `app/repo/admin/audit_log_repo.py` (foundation file) gained two read methods, `search` and `get`, and the `AuditSearch` result class. `write()` is unchanged. This exception was allowed by the brief for this track.

`app/config/helper.py` is not changed. The settings save calls its `is_secret_key()` to refuse secret-shaped keys, so the rule "secrets live in the environment, never in app_config" (tests/config/test_config_allowlist_no_secrets.py) also holds for the admin portal.

## Endpoints

All under `/api/v1/admin`, all super admin only. `admin.settings.read` and `admin.audit.read` are super admin only in this project.

| Method and path | Permission | Request | Success | Errors (`developerMessage`) |
|---|---|---|---|---|
| GET `/settings` | settings.read | `key?` (exact), `page`, `page_size` | 200 `{data:[{id,key,value,created_at,is_secret}], total_count}`, newest first | 401, 403, 422 (unknown param, `page_size=101`), 502 |
| PUT `/settings` | settings.write | `{items:[{key,value}], deleted_keys:[], reason?}` | 200 `{updated:[keys], deleted:[keys], cache_key_rotated}` | 409 `{"code":"no_change"}`; 422 `items.<i>.key: cache_key_forbidden`, `secret_key`, `duplicate_key`, `invalid_key` (new key not `^[A-Z0-9_]{1,100}$`), value over 10 000, both lists empty, key in both lists; 401, 403, 502 |
| GET `/settings/history` | settings.read | `key`, `changed_by` (uuid), `from`, `to` (dates, inclusive, UTC), `page`, `page_size` | 200 `{data:[{id,changed_at,changed_by:{id,email}\|null,ip_address,client_reported,reason,operation,changes:[{key,old_value,new_value,redacted}],parse_error}], total_count}` | 422 (`from` after `to`, bad uuid or date, unknown param, `page_size=101`), 401, 403, 502 |
| GET `/settings/history/{id}` | settings.read | | 200 one row of the same shape | 404 (unknown id or not an app_config row), 422 (id not a positive integer) |
| GET `/audit-log` | audit.read | `table` (enum: bundle, app_config, auth.users, tag_group, promotion, voucher, banner, promotion_rule, bundle_tag, tag), `operation` (INSERT, UPDATE, DELETE, BAN, UNBAN, BULK), `changed_by`, `from`, `to`, `page`, `page_size` | 200 `{data:[{id,changed_at,table_name,operation,legacy_operation,record_id,changed_by,ip_address,reason,rows_affected,client_reported,summary}], total_count, facets:{by_table:{...}}}` | 422 (unknown table or operation, `from` after `to`, ...), 401, 403, 502 |
| GET `/audit-log/{id}` | audit.read | | 200 the row plus `hours_banned`, `old_data`, `new_data`, `operation_details`, `parse_error` | 404, 422 |

POST, PUT, PATCH and DELETE on `/audit-log` return 405: no route writes or deletes audit rows.

### Rules

- **One audited save.** `PUT /settings` makes one call to `admin_upsert_settings`. It upserts, deletes, writes one audit row and rotates the cache key in one transaction. Before, the portal made four separate calls from the browser (MS-5071).
- **Only changes are written.** The service reads the touched keys first. It sends only the items whose value differs or whose key is new, and only the deleted keys that exist. If nothing would change, it returns 409 `no_change` and writes and audits nothing. The SQL function checks again under row locks.
- **Cache key.** `APP_CACHE_KEY` cannot be written or deleted (422). The server rotates it when `FORBIDDEN_COUNTRIES` is created, changed or deleted. That is exactly when the portal rotated it before (`settingsAPI.jsx` `updateSettings`).
- **Secrets.** A key that `is_secret_key()` treats as a secret (`*_KEY`, `*_SECRET`, `*_PASSWORD`, `*_TOKEN`, `*_USERNAME`, `RESELLER_ID`; `APP_CACHE_KEY` and `CACHE_KEY` excepted) cannot be created or updated (422 `secret_key`). Deleting a leftover secret row is allowed. In the audit row its old value is stored as `***`.
- **Existing lower-case keys.** Some existing keys are lower case, for example `login_type`. They can still be edited. The `^[A-Z0-9_]{1,100}$` rule applies to new keys only.
- **Redaction on read.** In the settings list, the history and the audit detail, values of keys matching `SECRET|PASSWORD|TOKEN|PRIVATE|API_KEY`, or `is_secret_key()`, are returned as `"***"` (`redacted: true` in `changes[]`, `is_secret: true` in the list). This also applies to rows written before MS-5013 and to super admins. The OpenAPI description of `/settings/history` states the rule.
- **`changes[]`** is computed on the server and lists only the keys whose value differs. A created key has `old_value: null` and a deleted key has `new_value: null`. Three stored shapes are read:
  - legacy browser rows: JSON strings of the whole `[{key,value}]` list;
  - `{KEY: value}` objects written by `admin_upsert_settings`;
  - `{id,key,value,created_at}` row snapshots.

  A row that cannot be read returns `changes: []` and `parse_error: true` instead of a 500.
- **Legacy operations.** These browser operation texts are mapped:
  - `Ban User` becomes `BAN` and `Unban User` becomes `UNBAN`;
  - `Activate Bundle` and `Deactivate Bundle` become `BULK` when `rows_affected > 1`, otherwise `UPDATE`.

  The original text is returned in `legacy_operation`. The same mapping is used in SQL for the `operation` filter.
- **`client_reported`** is `false` on rows written by the API (`admin_write_audit` sets it). Rows without the flag are legacy browser rows and are shown as `true`: their IP, when present, was reported by the browser.
- **Actor.** The actor email is joined with `UsersCopyAdminRepo.by_ids`, in one query per page. An actor who no longer exists is returned as `changed_by: null`.
- **Facets.** `facets.by_table` counts rows per table over every filter except `table`, so the portal can show a count next to each table option.
- **Dates.** `from` and `to` are calendar days, inclusive, compared in UTC with `changed_at` (a `timestamp` without time zone). The API sends `[from 00:00, to + 1 day 00:00)` to SQL.

## Files

### New

| File | Purpose |
|---|---|
| `app/api/v1/admin/settings.py` | GET/PUT `/settings`, GET `/settings/history`, GET `/settings/history/{id}` (the file existed empty and was already mounted) |
| `app/api/v1/admin/audit_log.py` | GET `/audit-log`, GET `/audit-log/{id}` (existed empty, already mounted) |
| `app/schemas/admin/settings.py` | `AdminSettingItem`, `AdminSettingsUpdate` (422 checks), `AdminSettingsUpdateResult`, `AdminSettingRow`, `AdminActor`, `AdminSettingChange`, `AdminSettingsHistoryRow` |
| `app/schemas/admin/audit_log.py` | `AdminAuditTable`, `AdminAuditOperation` enums, `AdminAuditLogRow`, `AdminAuditLogDetail`, `AdminAuditLogPage` (facets `{by_table:{}}`) |
| `app/services/admin/settings_service.py` | `compute_changes`, `redact_changes`, `SettingsAdminService` (list, update, history, history_one) |
| `app/services/admin/audit_query_service.py` | `is_secret_name`, `redact`, `parse_audit_json`, `normalize_operation`, `project_row`, `AuditQueryService` |
| `app/repo/admin/app_config_repo.py` | `AppConfigAdminRepo`: `search`, `by_keys`, `upsert_settings` (RPC) |
| `migrations/admin/110_audit_search.sql` | `admin_audit_log_search` and its helpers |
| `migrations/admin/120_settings.sql` | `admin_upsert_settings`, `admin_setting_is_secret` |
| `tests/admin/api/test_settings_api.py`, `tests/admin/api/test_audit_log_api.py`, `tests/admin/services/test_settings_audit_service.py` | Tests |

### Changed

| File | Change |
|---|---|
| `app/repo/admin/audit_log_repo.py` | Added `AuditSearch`, `search()` (RPC `admin_audit_log_search`) and `get()`. `write()` untouched |

## SQL written, not applied

| File | Content |
|---|---|
| `110_audit_search.sql` | `admin_audit_log_search(p_table, p_operation, p_changed_by, p_from, p_to, p_setting_key, p_limit, p_offset)` returns `{data, total, facets_by_table}`. Internal helpers: `admin_audit_jsonb` (parses legacy JSON strings, NULL when not JSON), `admin_settings_map` (reads all three settings shapes), `admin_audit_operation` (legacy mapping) |
| `120_settings.sql` | `admin_upsert_settings(p_items jsonb, p_deleted text[], p_actor uuid, p_ip text, p_reason text)`. In one transaction: validation (AD422), `FOR UPDATE` locks in key order, upsert only when the value differs, deletes, AD409 `no_change`, `admin_rotate_cache_key()` when `FORBIDDEN_COUNTRIES` changed, one `admin_write_audit` row (`UPDATE`, old and new values of the touched keys, details `{updated, deleted, rows_affected, cache_key_rotated}`) |

Both files follow `050_admin_audit_helpers.sql`: `CREATE OR REPLACE`, `SECURITY DEFINER` on the public functions, `SET search_path = public, pg_temp`, EXECUTE revoked from PUBLIC, anon and authenticated, and granted to service_role on the public functions only. They are idempotent. `120` depends on `050`.

### For the team leader's review

- `admin_setting_is_secret` copies `is_secret_key()` of `app/config/helper.py`. If the Python rule changes, change both.
- The history `key` filter has to read the old and new data of every `app_config` row that passes the other filters. Legacy rows hold JSON strings, so no jsonb index can serve them. The filter runs only on `app_config` rows, which are few. If this becomes slow, rewrite the legacy rows into objects once and add an index on the jsonb keys.
- The `operation` filter compares the normalised operation, so the index cannot serve it. Table, date and actor filters use the indexes from `010`.
- An insert of a new key races with the same insert from another save through `ON CONFLICT (key)`, and the last write wins.
- `p_from` and `p_to` are `timestamp` without time zone, like `audit_log.changed_at`.

## Tests

```powershell
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m pytest tests/admin -q -p no:cacheprovider
```

- 432 passed in all of `tests/admin`, including the other tracks' tests at the time of the run. This story's three files have 115 tests. They cover:
  - 200 for each route;
  - 401 without a session;
  - 403 for `admin` on every route;
  - 404, 405 on writes to `/audit-log`, 409 `no_change` (from the service and from SQL), 422 cases and 502;
  - redaction, legacy shapes, `parse_error`, deleted actors, facets, filter passing and paging;
  - the RPC parameters of both repositories.
- In `tests/mcp/test_legacy_unchanged.py`, `test_openapi_gained_only_mcp_schemas` passes: this story's schemas are all `Admin*`.
- `test_only_expected_files_were_modified` lists `app/repo/admin/audit_log_repo.py`, the allowed exception above. The guard's allowed list needs that path added, and that file is not owned by this track.

## Bugs fixed

- **MS-5071 (settings and audit part).** Settings saves are audited by the server in the same transaction as the change. The audit row records the actor from the token, the IP from the request, `client_reported: false`, and only the touched keys. The browser no longer writes audit rows or the cache key, and no longer looks up its own IP.
- **MS-5070 (settings part).** Duplicate and invalid keys are refused by the API (422), not checked in the browser.
