# MS-5089, MS-5058, MS-5060: vouchers and banners admin API

Stories: MS-5089 (vouchers), MS-5058 (banners list and delete), MS-5060 (banner add and edit). Bugs fixed: MS-5062, MS-5063, and the voucher and banner parts of MS-5070 and MS-5071. Portal counterpart: `docs/adminDocs/MS-5090-MS-5059-MS-5061-vouchers-banners.md` in the admin portal repo. Branch: `TEOS-139-be-planning-for-changes`. Date: 2026-10-21.

Status: done and tested with mocks. Nothing is committed. No SQL was written or run for these stories.

## No current flow changed

No existing flow or behavior of the backend changed.

- All code is in new admin files, plus the two router files that were already mounted (`vouchers.py`, `banners.py`, which were empty).
- No existing service, router, repository, schema, model, `main.py` or shared admin file was edited.
- The app route `GET /app/banners` is untouched. It still reads banners through the `get_banners` SQL function, and banner rows still store the full public image URL that it returns.

## Endpoints

All under `/api/v1/admin`. Lists use `page` (1-based), `page_size` (at most 100) and the standard list envelope. Any undeclared query parameter returns 422.

| Method and path | Permission | Request | Success | Errors (`developerMessage`) |
|---|---|---|---|---|
| GET `/vouchers` | `admin.vouchers.read` | `search` (code, literal), `status=all\|active\|inactive` | 200 `{data:[{id,code,amount,is_used,is_active,expired_at,created_at,used_by_user:{id,email}\|null}], total_count, facets:{all,active,inactive}}` | 401, 403, 422 |
| POST `/vouchers` | `admin.vouchers.write` | JSON `{code ^[A-Za-z0-9_-]{2,50}$, amount > 0 (≤ 1,000,000), expired_at? YYYY-MM-DD, today or later}` | 201 voucher | 409 `{"code":"duplicate_code"}` (any letter case), 422 |
| PATCH `/vouchers/{id}/status` | `admin.vouchers.write` | `{is_active, reason?}` | 200 voucher | 404, 409 `no_change`, 422 |
| DELETE `/vouchers/{id}` | `admin.vouchers.write` | none | 204 | 404, 409 `voucher_used` |
| GET `/banners` | `admin.banners.read` | `search` (title, literal), `status=all\|active\|inactive` | 200 `{data:[{id,title,description,image_url,action,platform,is_active,created_at}], total_count, facets:{all,active,inactive}}` | 401, 403, 422 |
| GET `/banners/{id}` | `admin.banners.read` | none | 200 banner | 404, 422 (not an integer) |
| POST `/banners` | `admin.banners.write` | multipart: `title` 1..100, `description` 1..500, `platform` `web\|mobile`, `action?` `[A-Za-z0-9_]{1,50}`, `is_active?` `true\|false`, file `image` (required) | 201 banner | 409 `duplicate_title`, 413 image too large, 422 (missing image, bad field, unknown field, image not PNG/JPEG/WebP or over 4000×4000), 502 storage |
| PUT `/banners/{id}` | `admin.banners.write` | same fields; `image` optional (kept when absent) | 200 banner | 404, 409 `duplicate_title`, 409 `guard_failed` (another admin replaced the image meanwhile), 413, 422, 502 |
| DELETE `/banners/{id}` | `admin.banners.write` | none | 204 | 404 |

Notes:

- Facets count the rows matching the search, whatever the status filter.
- `used_by_user` is joined server-side with `UsersCopyAdminRepo.by_ids`. It is null when the voucher is unused or the user no longer exists.
- Every write goes through `insert_audited`, `update_audited` or `delete_audited`, so one audit row is written in the same transaction as the change, with the actor from the token and the IP from the request. The status reason is stored as the audit reason.
- A voucher delete uses the guard `{"is_used": false}`. A used voucher returns 409 `voucher_used` instead of the foundation's `guard_failed`.
- `PUT /banners/{id}` that changes nothing returns 200 with the banner, not 409 `no_change`, so pressing Save twice is harmless.
- Banner multipart fields other than the six above return 422.
- `is_active` is read as the text `true` or `false`. FastAPI 0.156 turns a missing boolean form field into `false`, which would deactivate a banner whenever the field was left out.

### Banner images

The `MediaStorageService` from the foundation handles the images. They go to bucket `media`, folder `banners-web`, which is where the portal uploads them today. The server generates the file name and builds the URL.

| Action | Order |
|---|---|
| Create | validate and upload → audited insert → if the insert fails (409, 502…), `remove_staged` deletes the uploaded file |
| Edit with a new image | read the row (404 before any upload) → upload → audited update with guard `{"image": <current URL>}` → on failure `remove_staged` → on success `remove_after_commit(<old path>)` |
| Edit without an image | audited update of the fields only. No storage call |
| Delete | audited delete → `remove_after_commit(<path of the deleted row's image>)` |

The old path comes from `path_from_url` on the stored URL, which also resolves legacy URLs uploaded by the portal (`.../object/public/media/banners-web/<uuid>-<title>.png`). Images outside the `media` bucket and values such as `undefined` are skipped. A failed delete is queued in `storage_cleanup_queue` and never fails the request.

### APP_CACHE_KEY

Every banner write (create, edit, delete) rotates `APP_CACHE_KEY` (`ROTATE_CACHE = True` in `banner_service.py`), as MS-5058 and MS-5060 ask. User decision on 2026-10-21; the portal did not rotate it before. Voucher writes still do not rotate it, as before.

## Files

### New

| File | Purpose |
|---|---|
| `app/schemas/admin/vouchers.py` | `AdminVoucherCreate` (extra forbidden, code pattern, amount > 0, expiry today or later), `AdminVoucher`, `VoucherStatus` |
| `app/schemas/admin/banners.py` | `AdminBanner`, field limits, allowed multipart fields, `is_active` pattern |
| `app/repo/admin/voucher_repo.py` | `VoucherAdminRepo`: search, facets, `UNIQUE_CONFLICTS` |
| `app/repo/admin/banner_repo.py` | `BannerAdminRepo`: search, facets, `UNIQUE_CONFLICTS` |
| `app/services/admin/voucher_service.py` | List with user join, create, status, guarded delete |
| `app/services/admin/banner_service.py` | List, get, create, edit, delete with staged upload and cleanup |
| `tests/admin/api/test_vouchers_api.py`, `tests/admin/api/test_banners_api.py` | HTTP contract |
| `tests/admin/services/test_voucher_service.py`, `tests/admin/services/test_banner_service.py` | Services and repositories |
| `adminDocs/MS-5089-MS-5058-MS-5060-vouchers-banners.md` | This record |

### Filled (already existed, were empty and mounted)

| File | Change |
|---|---|
| `app/api/v1/admin/vouchers.py` | The four voucher routes |
| `app/api/v1/admin/banners.py` | The five banner routes and the unknown-field check |

`UNIQUE_CONFLICTS` uses the names from `migrations/admin/020_constraints.sql`: `banner_title_lower_key` → `duplicate_title`, `voucher_code_lower_key` → `duplicate_code`. The original case-sensitive `voucher_code_key` (supabase_ddl.sql) also maps to `duplicate_code`.

## SQL

None written. These stories use the foundation's SQL only:

- `000_schema_drift.sql`: `banner.is_active` (default true). The banner list and its facets filter on this column, so **`GET /banners` returns 502 until 000 is applied**.
- `020_constraints.sql`: unique indexes `banner(lower(title))` and `voucher(lower(code))`.
- `050_admin_audit_helpers.sql`: the audited write functions (`banner` and `voucher` are in its table allowlist).
- `130_storage_cleanup_queue.sql`: the queue for failed image deletes.

### Review notes

- `banner.is_active` has no effect on the app: `get_banners` (supabase_ddl.sql) does not filter on it. The API stores and filters it, but the portal does not show a banner status control yet, so that admins are not misled. Making the app respect it would change `get_banners`, which is outside this story.
- APP_CACHE_KEY rotation on banner writes: on since 2026-10-21 (user decision, see above).
- No active/inactive switch for banners (user decision 2026-10-21): the app's `get_banners` database function ignores `banner.is_active`, so a switch would have no effect in the app. The function change is listed for the lead in `lead-review-old-flow.md`.
- `voucher.amount` is `real`. It is returned as a JSON number, not as the decimal string MS-5089 test case 3 mentions, because the portal formats it as a number.
- The story's banner fields `link` and `sort_order` do not exist in the `banner` table. The real columns are used: `title`, `description`, `action`, `platform`, `image`, `is_active`. The plan already records that `image` keeps the full URL.
- The largest image comes from `ADMIN_MAX_UPLOAD_BYTES`, 1 MB by default since 2026-10-21 (user decision, as MS-5060 asks).
- MS-5058 case 23 (a job that retries queued image deletions) is dropped from the story by user decision on 2026-10-21. Failed deletions stay recorded in `storage_cleanup_queue`; the follow-up is listed for the lead in `lead-review-old-flow.md`.
- `image` must be PNG, JPEG or WebP. The portal's cropper always sends a JPEG, so the SVG listed in the portal tooltip still works: SVGs are rasterised in the browser before upload.

## Tests

```powershell
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m pytest tests/admin -q -p no:cacheprovider
```

The four new files have 90 tests: vouchers API 27, voucher service and repository 13, banners API 30, banner service and repository 20. All pass. They cover:

- 200, 201 (`responseCode` 201), 204 with an empty body, and the paging defaults;
- 401 without a session; 403 for an admin missing the write or read permission, with nothing uploaded;
- 404 and delete twice (204, then 404);
- 409 `duplicate_code`, `duplicate_title`, `no_change`, `voucher_used` (mapped from `guard_failed`), `guard_failed` on concurrent image replacement;
- 413;
- 422: `page_size=101`, an unknown query parameter, a bad status, `amount=0`, a code with spaces, a past expiry, an extra body field, a missing image, a blank or overlong title, an unknown multipart field, a bad platform, action or `is_active`;
- the 23505 mapping per constraint name;
- the image lifecycle: upload folder, staged removal on failure, old file removed after an edit (MS-5062, MS-5063), no storage call without an image, legacy URL paths.

Full admin suite result: see the end of this file.

## Bugs fixed

| Bug | Fix |
|---|---|
| MS-5062: editing a banner image saved `image = undefined` | The server saves the URL returned by its own upload |
| MS-5063: banner images were never deleted (wrong bucket or no bucket) | The server removes the old file from bucket `media` after an edit or delete, and queues it when storage fails |
| MS-5070 (vouchers, banners): browser uniqueness pre-checks raced | Unique indexes in the database. A duplicate returns 409 `duplicate_code` or `duplicate_title` |
| MS-5071 (vouchers, banners): writes were not audited | Every write is audited server-side in the same transaction |

## Full admin suite

`tests/admin`: 432 passed (run on 2026-10-21, other tracks' tests included).
