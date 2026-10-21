# MS-5083: bundles and assign-to-groups admin API

Story: MS-5083, bundles page and assign bundle to groups on the admin API. Bugs fixed: MS-5065, and the bundles part of MS-5069 and MS-5071. Portal counterpart: `docs/adminDocs/MS-5084-bundles.md` in the admin portal repo. Branch: `TEOS-139-be-planning-for-changes`. Date: 2026-10-21.

Status: done and tested with mocks. Nothing is committed. Two SQL files were written and not run.

## No current flow changed

No existing flow or behavior of the backend changed.

- All code is in new admin files, plus the router file that was already mounted (`bundles.py`, which held the lookup route and keeps it unchanged, above the `/{id}` routes) and the bundle repository that held the lookup (`bundle_repo.py`, extended; `lookup` is untouched).
- No existing service, router, repository, schema, model, `main.py` or shared admin file was edited.
- The storefront still reads bundles through its own routes and the legacy SQL functions (`search_bundles`, `activate_all_bundles`, `deactivate_all_bundles`) are untouched until the clean-up story.
- `APP_CACHE_KEY` keeps rotating after a rename, a status change, a bulk status change and a tag assignment, exactly where the portal rotated it from the browser, so the storefront drops its bundle cache as before.

## Endpoints

All under `/api/v1/admin/bundles`. Lists use `page` (1-based), `page_size` (at most 100) and the standard list envelope. Any undeclared query parameter returns 422. `{id}` is a uuid (422 otherwise).

| Method and path | Permission | Request | Success | Errors (`developerMessage`) |
|---|---|---|---|---|
| GET `` | `admin.bundles.read` | `search` (literal, on id, `bundle_name`, `data->>bundle_name`, `data->>bundle_info_code`), `tag_ids` (repeated key, uuids), `status=all\|active\|inactive` | 200 `{data:[{id,bundle_name,is_active,created_at,updated_at,data}], total_count, facets:{all,active,inactive}, totals:{total,active_count,inactive_count}}` | 401, 403, 422, 502 |
| GET `/lookup` | `admin.bundles.read` | MS-5077, unchanged | | |
| GET `/{id}` | `admin.bundles.read` | none | 200 bundle + `tags:[{bundle_tag_id, tag:{id,name,icon, group:{id,name,type,group_category}\|null}}]` | 404, 422 |
| PATCH `/{id}` | `admin.bundles.write` | `{bundle_name}` 1..60 characters, not only spaces, trimmed | 200 `{id, bundle_name}` | 404, 409 `no_change`, 422 |
| PATCH `/{id}/status` | `admin.bundles.bulk` (super admin, as the portal's row toggle today) | `{is_active, reason?}`; `reason` (1..500) required when `is_active` is false | 200 `{id, is_active}` | 404, 409 `no_change`, 422 |
| POST `/status/bulk` | `admin.bundles.bulk` (super admin) | `{is_active, reason?}`; `reason` required when `is_active` is false | 200 `{rows_affected, affected_bundle_ids[]}` | 401, 403, 409 `no_change`, 422 |
| PUT `/{id}/tags` | `admin.bundles.write` | `{tag_ids: uuid[]}`, empty allowed, at most 500, duplicates ignored | 200 `{id, tag_ids[]}` (the set as stored, sorted) | 404, 422 (`unknown_tag` in the detail, with the unknown ids), 403 |

Notes:

- `facets` count the rows matching the search and the tag filter, whatever the status filter, for the status tabs. `totals` always cover the whole table, so the "Activate all" / "Deactivate all" buttons know what they would change. Without search or tags both are the same two head counts; with them, `admin_search_bundles` returns the page, the total and the facets in one call and `totals` are two head counts.
- `is_active` is NULL on some old rows. Everywhere (list, facets, totals, SQL) NULL counts as inactive (`IS NOT TRUE`), as the legacy `activate_all_bundles` did, so `all = active + inactive`.
- Every write is audited in the same transaction as the change, with the actor from the token and the IP from the request. The reason of a status change is stored as the audit reason.
- Rename goes through the foundation's `admin_update_audited` (full row snapshots). Status changes go through dedicated functions so the audit row keeps the shape the portal wrote: `old_data` / `new_data` = `{"affected_bundle_ids": [...], "status": "active"|"inactive"}`, operation `UPDATE` for one bundle and `BULK` for the bulk action. A tag assignment writes `{"tag_ids": [...]}` before and after.
- `PUT /{id}/tags` with the set the bundle already has returns 200 and writes nothing: no audit row, no cache rotation. Pressing Save twice is harmless.
- Concurrency: `admin_set_bundle_status` and `admin_set_bundle_tags` lock the bundle row (`FOR UPDATE`). Two admins toggling the same bundle get one 200 and one 409 `no_change`.

## Files

### New

| File | Purpose |
|---|---|
| `app/schemas/admin/bundles.py` | `AdminBundle`, `AdminBundleDetail` (+ `AdminBundleTag`, `AdminBundleTagItem`, `AdminBundleTagGroup`), `AdminBundleRename`, `AdminBundleStatusPatch` (`AdminStatusPatch` + reason rule), `AdminBundleBulkStatus`, `AdminBundleTagsSet`, result shapes |
| `app/services/admin/bundle_service.py` | `BundleAdminService`: list (plain or search function, facets, totals), get with tags, rename, status, bulk status, tag set |
| `migrations/admin/060_admin_search_bundles.sql` | `admin_like_escape`, `admin_search_bundles` |
| `migrations/admin/061_bundles_admin.sql` | `admin_bundles_assert_actor_reason`, `admin_set_bundle_status`, `admin_set_all_bundles_status`, `admin_set_bundle_tags` |
| `tests/admin/api/test_bundles_api.py` | HTTP contract |
| `tests/admin/services/test_bundle_service.py` | Service and repository |
| `adminDocs/MS-5083-bundles.md` | This record |

### Changed (admin files of this track)

| File | Change |
|---|---|
| `app/api/v1/admin/bundles.py` | The six routes above. The MS-5077 lookup line is unchanged and stays first; `POST /status/bulk` is declared before the `/{bundle_id}` routes |
| `app/repo/admin/bundle_repo.py` | `BundleAdminRepo` gains `needs_search_function`, `list_plain`, `search` (RPC), `facets`, `totals`, `detail` (embed `bundle_tag(id,tag(id,name,icon,tag_group(...)))`), `rename`, `set_status`, `set_all_status`, `set_tags`. `lookup` is unchanged |

## SQL

Written, not run. Apply with the command in `migrations/admin/README.md`; both files are idempotent (`CREATE OR REPLACE`, `REVOKE`/`GRANT`) and `service_role`-only.

| File | Function | Replaces | Notes |
|---|---|---|---|
| `060_admin_search_bundles.sql` | `admin_search_bundles(p_search, p_tag_ids, p_is_active, p_page, p_page_size)` → `{items, total_count, facets}` | `search_bundles` (admin repo `src/core/procedures/search_bundles.sql`) | 1-based page, page size clamped to 100, escaped `ILIKE` (literal `%`, `_`, `\`), `EXISTS` on `bundle_tag` instead of `LEFT JOIN` + `DISTINCT`, order `created_at DESC, id DESC`, facets in the same call |
| `061_bundles_admin.sql` | `admin_set_bundle_status(p_id, p_is_active, p_actor, p_ip, p_reason)` → `{id, is_active}` | the portal's single-row update + audit insert + cache update | row lock, 404, 409 `no_change`, legacy audit shape |
| | `admin_set_all_bundles_status(p_is_active, p_actor, p_ip, p_reason)` → `{rows_affected, affected_bundle_ids}` | `activate_all_bundles`, `deactivate_all_bundles` | only the rows that change are updated; 409 `no_change` when none; one `BULK` audit row |
| | `admin_set_bundle_tags(p_bundle_id, p_tag_ids, p_actor, p_ip, p_reason)` → `{id, tag_ids, added, removed, cache_key_rotated}` | the portal's upsert + `not in ()` delete + cache update | `tag_id <> ALL(p_tag_ids)` handles `[]` (MS-5065); `ON CONFLICT (bundle_id, tag_id) DO NOTHING`; 422 `unknown_tag`; no write when the set is unchanged |

Dependencies: `050_admin_audit_helpers.sql` (`admin_write_audit`, `admin_rotate_cache_key`, `admin_update_audited`; `bundle` is in its table allowlist) and the unique key `bundle_tag(bundle_id, tag_id)` (`unique_bundle_tag` in `supabase_ddl.sql`, also ensured by `020_constraints.sql`).

Until `060` and `061` are applied: `GET /bundles` without search or tags, `GET /bundles/{id}` and `PATCH /bundles/{id}` work (PostgREST and `050`); a search or tag filter, a status change, the bulk action and the tag assignment return 502.

### Review notes

- The search also matches `id::text`, as the legacy `search_bundles` did (the portal shows the id column); MS-5083 lists only the three name/code fields.
- The list is ordered `created_at DESC, id DESC` in both paths (the plan's default order). The legacy search function ordered by `bundle_name`; the plain list already used `created_at desc`, so the two paths now agree.
- `PATCH /{id}/status` uses dedicated SQL (`admin_set_bundle_status`) instead of `admin_update_audited`, which the plan named for that row: `admin_update_audited` writes full row snapshots and cannot produce the legacy `{affected_bundle_ids, status}` audit shape the plan asks to keep. Rename does use `admin_update_audited`.
- `AdminStatusPatch` is kept as the body; `AdminBundleStatusPatch` only adds the "reason required when deactivating" rule, so the portal sends the same body as for vouchers.
- Activating one bundle or all bundles accepts a reason but does not require one (the portal's toggle dialog asks for one in both directions; the bulk activation dialog makes it optional).

## Tests

```powershell
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m pytest tests/admin/api/test_bundles_api.py tests/admin/services/test_bundle_service.py tests/admin/api/test_lookups_api.py -q -p no:cacheprovider
```

126 passed (the two new files plus the lookup tests, which prove the lookup route still answers before `/{id}`). They cover:

- list: paging defaults, facets and totals in the envelope, repeated `tag_ids`, a literal search (`a,is_active.eq.false` reaches the service unchanged), 401, 422 for `page_size=101`, `status=foo`, an unknown parameter, `page=0`, a non-uuid tag and `tag_ids[]`, 502;
- detail: tags with their group, dangling links skipped, 404, 422 for a non-uuid id;
- rename: trimmed name, 60 accepted, 61 / blank / extra field refused (422), 404, 403 without write;
- status: reason stored as audit reason, activation without reason, deactivation without reason refused (422), extra field, 409 `no_change`, 404, 403;
- status: 403 for `admin` (super admin only, as today);
- bulk: 403 for `admin`, 200 for `super_admin` with `rows_affected` and ids, reason rule, 409 `no_change`, 401;
- tags: exact set, empty set (MS-5065), duplicates removed, 422 unknown tag / bad body / `null`, 404, 403;
- repository: RPC names and parameters (actor, ip, reason, 1-based page, `p_tag_ids`/`p_is_active` null), `rotate_cache=True` on rename, `is.true` / `not.is.true` status filters, head counts for facets and totals, the detail embed, the AD404 / AD409 / AD422 mappings.

Full admin suite result: see the end of this file.

## Bugs fixed

| Bug | Fix |
|---|---|
| MS-5065: removing every tag from a bundle failed (`not in ()`), and the three calls were not atomic | `admin_set_bundle_tags` replaces the set in one transaction; `[]` is valid |
| MS-5069 (bundles): the portal called `search_bundles(p_is_active)`, which exists only in the admin repo | `admin_search_bundles` in `migrations/admin/060`, applied with the rest |
| MS-5071 (bundles): status changes were audited from the browser, with the actor and IP from the client; the rename rotated the cache even when the update failed | Audit and cache rotation happen in the SQL function, in the transaction of the change |

## Full admin suite

`tests/admin`: see the hand-over report (run on 2026-10-21 with the other wave-2 tracks' tests present).
