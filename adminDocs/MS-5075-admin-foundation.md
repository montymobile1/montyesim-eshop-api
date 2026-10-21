# MS-5075: admin API foundation

Story: MS-5075, admin API foundation: login, permissions, base repository, audit and image storage. Branch: `TEOS-139-be-planning-for-changes`. Date: 2026-10-20.

Status: done and tested. Nothing is committed and no migration has been applied.

## No current flow changed

No existing flow or behavior of the backend changed. The mobile app login, its token check, payments, the Stripe webhook, callbacks, the scheduler and every existing endpoint work exactly as before.

- All admin code is in new files.
- The edits to existing files only append lines. The table below lists all of them.
- No existing service, router, repository, schema, model, `main.py`, `security.py` or `base_repo.py` was touched.
- `supabase_ddl.sql` is untouched, and no SQL has been run against any database.

How this was checked: the full non-admin test suite was run on the untouched branch and on the changed code under the same conditions. The results are identical, except for four guard tests that fail on any uncommitted file and pass again once the work is committed. The guard test that checks every existing endpoint's OpenAPI definition is unchanged passes.

## What the story adds

### Endpoints

All under `/api/v1/admin/auth`. Responses use the existing envelope (`status`, `data`, `message`, `developerMessage`, `responseCode`), and the HTTP status always equals `responseCode`.

| Method and path | Who | Request | Success | Errors (`developerMessage`) |
|---|---|---|---|---|
| POST `/login` | anyone | `{email, password}` | 200 `{access_token, refresh_token, expires_in, user:{id, email, role, permissions[]}}` | 401 `ADMIN_LOGIN_FAILED`, 403 `ADMIN_ROLE_REQUIRED`, 429 too many failed attempts, 502 Supabase Auth down |
| POST `/refresh` | anyone with a refresh token | `{refresh_token}` | 200, same data as login | 401 `INVALID_REFRESH_TOKEN`, 403 `ADMIN_ROLE_REQUIRED` |
| POST `/logout` | any admin | bearer token | 204, empty body | 401 |
| GET `/me` | any admin | bearer token | 200 `{id, email, role, permissions[]}` | 401, 403 `ADMIN_ROLE_REQUIRED` |

### Roles and permissions

- The role is stored on the Supabase user in `app_metadata.role`. Only the service-role key can write that field, so a user cannot give themselves a role.
- Two roles are accepted: `admin` and `super_admin`. An account with no role or another value cannot sign in to the portal; its new session is revoked at once.
- `app/config/admin_permissions.py` maps each role to its permissions. Moving a permission between roles is a one-line change there.

| Role | Permissions |
|---|---|
| `admin` | read: users, orders, bundles, groups, tags, rules, promotions, vouchers, banners, devices, contact_us, referrals. Write: bundles, groups, tags, rules, promotions, vouchers, banners |
| `super_admin` | everything `admin` has, plus `admin.settings.read`, `admin.settings.write`, `admin.audit.read`, `admin.users.ban`, `admin.bundles.bulk`, `admin.referrals.export` |

Settings, settings logs and the audit log stay super-admin only, as they are in the portal today.

- Every request re-reads the user from Supabase Auth, so removing a role or banning an admin takes effect on the next request.
- A route declares the permission it needs with `Depends(require_permission("admin.<area>.<action>"))`; a missing permission returns 403.

To give an account a role (a database change, done by someone with Supabase access):

```sql
update auth.users
set raw_app_meta_data = coalesce(raw_app_meta_data, '{}') || '{"role":"super_admin"}'
where email = 'someone@company.com';
```

Use `"admin"` for a regular admin. To remove access, remove the `role` key.

### Rules shared by every admin endpoint

`app/api/v1/admin/_route.py` gives every admin route these behaviors, without editing `main.py`:

- an unknown query parameter returns 422;
- a request validation error returns HTTP 422 (the existing handler answers 400 for the consumer routes, which is unchanged);
- a JSON body larger than `ADMIN_MAX_JSON_BYTES` returns 413;
- a database error from a repository returns 502 with a generic message.

Admin code raises its own error classes (`app/exceptions/admin.py`), which go through the existing `CustomException` handler.

### Base repository for admin queries

`app/repo/admin/base.py` holds `AdminBaseRepository`. It extends the existing `BaseRepository` without changing it, and every admin repository extends it. It gives each repository:

- **total count with paging**: `q()` selects with the total row count, and `paged(query, page)` returns one page plus the total;
- **a stable order**: `created_at desc, id desc` unless the repository says otherwise, so pages never repeat or skip rows (bug MS-5067);
- **safe search and filters**: `or_ilike` and `filters` match user text literally. `%`, `_`, commas, quotes and parentheses cannot add filters or act as wildcards (bug MS-5068). Column names are checked against a strict pattern;
- **known limitation**: PostgREST treats `*` in a `like`/`ilike` pattern as `%` before the query runs, and offers no escape for it. Searching for `*` therefore matches like a wildcard. No search field in the portal relies on a literal `*`;
- **audited writes**: `insert_audited`, `update_audited` and `delete_audited` call the `admin_*_audited` SQL functions, which save the change and its audit row in one transaction. `update_audited` and `delete_audited` accept a `guard`, the values the row must still have, else 409 `guard_failed`;
- **clear errors**: a duplicate becomes 409 with the field code from the repository's `UNIQUE_CONFLICTS` (for example `duplicate_code`), a foreign key violation 409 `referenced`, the SQL codes AD404, AD409 and AD422 become 404, 409 and 422, and any other failure 502 `DATABASE_ERROR` with the cause only in the log.

Example:

```python
class PromotionAdminRepo(AdminBaseRepository):
    UNIQUE_CONFLICTS = {"promotion_code_key": "duplicate_code"}

    def search(self, text, page):
        query = self.or_ilike(self.q(), ["name", "code"], text)
        return self.paged(query, page)
```

### Audit

- Database writes are audited by the SQL functions, in the same transaction as the change.
- `AuditService.log` (`app/services/admin/audit_service.py`) is for actions that are not one database write, such as ban and unban in Supabase Auth, or exports. It writes through the `admin_write_audit` SQL function.
- `audit_context(user, request, reason)` builds the actor, IP and reason of a request. The actor always comes from the token and the IP from the request headers, never from the request body.

### App cache key

There is no separate Python service for it, by decision of the team. The audited SQL functions take a `rotate_cache` switch (`rotate_cache=True` in Python), which rotates `APP_CACHE_KEY` in the same transaction as the change. It is off by default. Each page story turns it on only where the portal rotates the key today, so the storefront keeps dropping its bundle cache exactly as it does now. `admin_rotate_cache_key()` can also be called on its own through `repo.rpc`.

### Image storage

`MediaStorageService` (`app/services/admin/media_storage_service.py`), used by the banner and group stories:

- checks the real file content: PNG, JPEG or WebP, at most `ADMIN_MAX_UPLOAD_BYTES` (413) and 4000 by 4000 pixels (422);
- stores the file under a name the server generates, `<folder>/<uuid>.<ext>`, in the `ADMIN_MEDIA_BUCKET` bucket (`media`, the bucket the portal uploads to today), and never overwrites a file;
- returns the public URL, and reads the path back from a stored URL with `path_from_url`;
- deletes files after a failed save (`remove_staged`) or after a successful one (`remove_after_commit`). A file that cannot be deleted is logged and added to `storage_cleanup_queue`; it never fails the request.

### Login protection

- Failed logins are counted per IP and email. After `ADMIN_LOGIN_RATE_LIMIT` failures (default 5 in 300 seconds) the next attempt gets 429 without reaching Supabase. A successful login resets the count.
- The count is kept in memory, per backend process.
- When `SUPABASE_JWT_SECRET` is set, tokens are also checked locally before Supabase Auth is asked.

## Files

### New files

| File | Purpose |
|---|---|
| `app/config/admin_permissions.py` | Roles and the permissions of each role |
| `app/dependencies/admin_security.py` | `admin_user`, `require_permission`, `client_ip` |
| `app/exceptions/admin.py` | Admin errors: 401, 403, 404, 409, 413, 422, 429, 502 |
| `app/api/v1/admin/__init__.py` | Admin router, mounts the auth routes |
| `app/api/v1/admin/_route.py` | Route class with the shared rules above, and `admin_router()` |
| `app/api/v1/admin/auth.py` | The four auth endpoints |
| `app/schemas/admin/common.py` | Paging parameters, list envelope, lookup item, status body (used by later stories) |
| `app/schemas/admin/auth.py` | Login, refresh, profile and session shapes |
| `app/services/admin/auth_service.py` | Login, refresh, logout and the failed-login counter |
| `app/repo/admin/base.py` | `AdminBaseRepository`, `AuditContext`, `Paged` |
| `app/repo/admin/audit_log_repo.py` | Audit rows through `admin_write_audit` |
| `app/repo/admin/storage_cleanup_queue_repo.py` | Files waiting for a delete retry |
| `app/services/admin/audit_service.py` | `AuditService`, `audit_context` |
| `app/services/admin/media_storage_service.py` | Image checks, upload, public URL, deletes with retry queue |
| `migrations/admin/*.sql`, `README.md`, `900_seed_super_admin.sql.example` | Foundation SQL: schema drift, audit log, unique constraints, triggers, audited write functions, storage cleanup queue. Written, not applied |
| `tests/admin/...` | 105 tests (permissions, auth service, HTTP contract, base repository, audit, image storage) |
| `adminDocs/` | This folder |

### Existing files, appended only

| File | Change |
|---|---|
| `app/api/v1/__init__.py` | One import and one `include_router` line mounting the admin router at `/admin` |
| `app/config/constants.py` | 10 new `ErrorMessages` members at the end (`ADMIN_*`) |
| `app/config/db.py` | 2 new `DatabaseTables` members at the end: `TABLE_AUDIT_LOG`, `TABLE_STORAGE_CLEANUP_QUEUE` |
| `locales/en.json`, `locales/ar.json` | The 10 matching messages at the end. The previous last line only gained a trailing comma |
| `requirements.txt` | `python-multipart==0.0.20`, `openpyxl==3.1.5`, `pytest-mock==3.14.0` (uploads and exports in later stories, test helper) |
| `.env.example` | `SUPABASE_JWT_SECRET` (optional) and the `ADMIN_*` settings below |
| `tests/mcp/test_legacy_unchanged.py` | The OpenAPI guard now allows paths under `/api/v1/admin/` and `Admin*` schemas. Its check that every existing endpoint is unchanged is untouched |

### New settings

| Variable | Default | Use |
|---|---|---|
| `SUPABASE_JWT_SECRET` | unset | Optional local token check |
| `ADMIN_LOGIN_RATE_LIMIT` | `5/300` | Failed logins allowed per IP and email, as attempts/seconds |
| `ADMIN_MAX_JSON_BYTES` | `262144` | Largest JSON body on admin routes |
| `ADMIN_MEDIA_BUCKET` | `media` | Storage bucket for admin uploads (later stories) |
| `ADMIN_MAX_UPLOAD_BYTES` | `1048576` | Largest admin image upload (1 MB since 2026-10-21, as MS-5060 and MS-5086 ask) |
| `ADMIN_EXPORT_ROW_CAP` | `50000` | Largest synchronous export (later stories) |

## Tests

```powershell
$env:PYTHONPATH="."; pytest tests/admin -q
```

105 passed. The admin tests mock Supabase and need no Firebase file or database.

## For the team leader's review of the SQL

- The SQL has not been run anywhere. Apply it with the command in `migrations/admin/README.md`, twice, to confirm it is idempotent.
- Compare `000_schema_drift.sql` with the deployed database first.
- `010_audit_log.sql` removes the public and signed-in roles' access to `audit_log`. That is safe only if the unmigrated portal pages use the service-role key.
- `audit_log.record_id` is a UUID, but some tables have integer ids. For those, the id is stored in `operation_details.record_id`.

## Left to later stories

- The users copy repository (admin emails shown in the audit log and settings history) comes with the audit log and settings history stories.
- The audited write functions are tested here with mocks only. They run against the database once the team leader applies `migrations/admin/`.
