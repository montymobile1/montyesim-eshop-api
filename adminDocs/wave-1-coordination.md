# Wave 1: shared changes made outside the stories

The four wave-1 stories were developed in parallel, each in its own files. A few files are shared by several stories, so they were prepared before the stories started, or adjusted during the review of the wave. This record lists those changes, which no story record covers in full.

Date: 2026-10-21. Nothing is committed and no SQL was applied.

## No current flow changed

Every file below is admin code or a test. No existing route, service, repository, schema or `main.py` was touched. The non-admin test suite gives the same result as before the wave, except two guards that fail on any uncommitted file (`test_only_expected_files_were_modified`, `test_no_migration_directory_or_sql_script_was_added`) and pass once the work is committed.

## Prepared before the stories

| File | Change | Why |
|---|---|---|
| `app/api/v1/admin/{users,bundles,groups,tags,promotion_rules,orders,devices,contact_us,vouchers,banners,settings,audit_log}.py` | Created as empty routers (`router = admin_router()`), each naming the story that owns it | So every story fills its own file and no two stories edit the same one |
| `app/api/v1/admin/__init__.py` | Mounts the twelve routers above at the paths of the plan's catalog (`/users`, `/bundles`, `/groups`, `/tags`, `/promotion-rules`, `/orders`, `/devices`, `/contact-us`, `/vouchers`, `/banners`, `/settings`, `/audit-log`) | Same reason. Wave 2 stories fill the routers already mounted here |
| `app/repo/admin/users_copy_repo.py` | `UsersCopyAdminRepo.by_ids(ids)` returns `{id: {id, email, msisdn}}`, by chunks of 200 ids; `user_summary(row)` | Orders, vouchers and the audit log all show a user's email, joined server-side |
| `tests/admin/repo/test_admin_users_copy_repo.py` | Tests for `by_ids` and, after the review, for the lookup by id | |

## Adjusted during the review

| File | Change | Why |
|---|---|---|
| `app/api/v1/admin/_lookup.py`, `app/services/admin/lookup_service.py`, the five lookup repositories | Every lookup accepts `?id=<id>` and returns only that item | The orders and devices pages show the selected user's email after a page reload. They first called `GET /users/{id}`, which only arrives with the Users story (MS-5079). Any dropdown can now restore its label the same way |
| `tests/admin/api/test_lookups_api.py`, `tests/admin/services/test_lookup_service.py` | Updated for the `id` argument, plus a test for it and for an empty `id` (422) | |
| `app/schemas/admin/common.py` | `StatusPatch` renamed to `AdminStatusPatch` (used by the voucher status route) | Every admin schema is named `Admin*`, which the OpenAPI guard relies on |
| `tests/mcp/test_legacy_unchanged.py` | `test_openapi_gained_only_mcp_schemas` accepts schema names containing `admin` in any case | FastAPI names the multipart bodies of the banner routes `Body_<handler>_api_v1_admin_...`. Checks on existing endpoints and schemas are unchanged |
| `adminDocs/MS-5075-admin-foundation.md` | Known limitation: `*` in a search acts as a wildcard | PostgREST turns `*` into `%` in `like` patterns and has no escape for it |

## Tests

```powershell
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m pytest tests/admin -q -p no:cacheprovider
```

439 passed after the review.
