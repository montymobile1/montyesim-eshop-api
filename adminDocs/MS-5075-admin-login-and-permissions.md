# MS-5075: admin login and permissions

Story: MS-5075, admin API foundation. Branch: `TEOS-139-be-planning-for-changes`. Date: 2026-10-20.

Status: the login and permissions part is done and tested. The rest of the foundation is still to do (see the last section). Nothing is committed and no migration has been applied.

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
| `migrations/admin/*.sql`, `README.md`, `900_seed_super_admin.sql.example` | Foundation SQL: schema drift, audit log, unique constraints, triggers, audited write functions, storage cleanup queue. Written, not applied |
| `tests/admin/...` | 57 tests (permissions, auth service, HTTP contract) |
| `adminDocs/` | This folder |

### Existing files, appended only

| File | Change |
|---|---|
| `app/api/v1/__init__.py` | One import and one `include_router` line mounting the admin router at `/admin` |
| `app/config/constants.py` | 10 new `ErrorMessages` members at the end (`ADMIN_*`) |
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
| `ADMIN_MAX_UPLOAD_BYTES` | `2097152` | Largest admin image upload (later stories) |
| `ADMIN_EXPORT_ROW_CAP` | `50000` | Largest synchronous export (later stories) |

## Tests

```powershell
$env:PYTHONPATH="."; pytest tests/admin -q
```

57 passed. The admin tests mock Supabase and need no Firebase file or database.

## For the team leader's review of the SQL

- The SQL has not been run anywhere. Apply it with the command in `migrations/admin/README.md`, twice, to confirm it is idempotent.
- Compare `000_schema_drift.sql` with the deployed database first.
- `010_audit_log.sql` removes the public and signed-in roles' access to `audit_log`. That is safe only if the unmigrated portal pages use the service-role key.
- `audit_log.record_id` is a UUID, but some tables have integer ids. For those, the id is stored in `operation_details.record_id`.

## Still to do in MS-5075

- `app/repo/admin/base.py`: base repository with safe search escaping, paging and database error mapping.
- Audit, cache-key and media storage services, and the audit log, users copy and storage cleanup repositories.
