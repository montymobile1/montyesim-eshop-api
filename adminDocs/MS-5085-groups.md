# MS-5085: groups admin API (list, add, edit, delete, status, tag order)

Story: MS-5085 (groups page, group add / edit, tag reorder). Bugs fixed: MS-5064, MS-5066, and the groups part of MS-5070 and MS-5071. Portal counterpart: `docs/adminDocs/MS-5086-groups.md` in the admin portal repo. Branch: `TEOS-139-be-planning-for-changes`. Date: 2026-10-21.

Status: done and tested with mocks. Nothing is committed. The SQL file was written and not run.

## No current flow changed

No existing flow or behavior of the backend changed.

- All code is in new admin files, plus the two router files that were already mounted (`groups.py`, `tags.py`). `tags.py` is untouched: the portal's tags page is a placeholder, so no tag route was needed beyond the existing `GET /tags/lookup`.
- No existing service, router, repository, schema, model, `main.py` or shared admin file was edited.
- The legacy SQL functions (`insert_group_with_tags`, `edit_tag_group`, `delete_group_if_no_bundle`) are not touched; the new functions have `admin_*` names.
- The app's reads of `tag_group` and `tag` are untouched. Rows written by the API have the same columns and the same `data` shape as the ones the portal wrote (except the URL-encoding bug fixed below).

## Endpoints

All under `/api/v1/admin/groups`. Lists use `page` (1-based), `page_size` (at most 100) and the standard list envelope. Any undeclared query parameter returns 422. `GET /groups/lookup` (MS-5077) is unchanged and stays above `/{id}`.

| Method and path | Permission | Request | Success | Errors (`developerMessage`) |
|---|---|---|---|---|
| GET `/groups` | `admin.groups.read` | `search` (name, literal), `status=all\|active\|inactive` | 200 `{data:[{id,name,type,group_category,is_active,created_at,updated_at,tag_count}], total_count, facets:{all,active,inactive}}` | 401, 403, 422 |
| GET `/groups/{id}` | `admin.groups.read` | `tag_search` (tag name, literal) | 200 group + `tags:[{id,tag_group_id,name,icon,sorting_number,data,created_at,updated_at}]` ordered by `sorting_number`, then `name`; `top_countries_count` (the `TOP_COUNTRIES_COUNT` setting, only for the group named Countries, else null) | 404, 422 |
| GET `/groups/{id}/tags` | `admin.groups.read` | none | 200 `{data:[tags], total_count}` (every tag, same order) | 404 |
| POST `/groups` | `admin.groups.write` | multipart: `payload` JSON `{name 1..29, type 1\|2, group_category 1..50, tags:[{client_key, name 1..300, alternative_country?: string[] \| string, data?}]}` (at least one tag) + files `icon_<client_key>` (optional, PNG/JPEG/WebP) | 201 group + tags | 409 `duplicate_group_name` / `duplicate_tag_name`; 413 icon too large; 422 (payload missing or not JSON, bad field, unknown form field, icon for an unknown `client_key`, bad image); 502 |
| PUT `/groups/{id}` | `admin.groups.write` | multipart: `payload` `{name, type, group_category, new_tags:[as above], updated_tags:[{id, name, remove_icon?, alternative_country?, data?}], deleted_tag_ids:[uuid]}` + files `icon_<client_key>` (new tags) / `icon_<tag id>` (existing tags) | 200 group + tags | 404; 409 `duplicate_group_name` / `duplicate_tag_name`; 409 `{"code":"linked_bundles","bundle_count":n}` when a deleted tag is assigned to a bundle; 422 `tag_not_in_group`, a tag both updated and deleted, unknown icon key, bad payload; 413; 502 |
| DELETE `/groups/{id}` | `admin.groups.write` | none | 204 | 404; 409 `{"code":"linked_bundles","bundle_count":n}` |
| PATCH `/groups/{id}/status` | `admin.groups.write` | `{is_active, reason?}` | 200 `{id, is_active}` | 404, 409 `no_change`, 422 |
| PUT `/groups/{id}/tags/order` | `admin.groups.write` | `{tag_ids:[uuid]}`: every tag of the group in the wanted order | 200 `{tag_ids}` | 404; 422 `tag_not_in_group` (a tag of another group), `incomplete_order` (a tag of the group is missing), repeated id |

Notes:

- `tag_count` comes from the PostgREST embedded count (`tag(count)`), one query for the page.
- `facets` count the rows matching the search, whatever the status filter.
- Group name: the database checks `length(name) < 30`, so the API accepts 1 to 29 characters (the portal form says 30; the 30th character was already refused by the database).
- On edit, the icon of an existing tag is kept unless a new file `icon_<id>` is sent or `remove_icon` is true. An edit that changes nothing returns 200 (no `no_change`), so pressing Save twice is harmless.
- `alternative_country` (the search keywords) is accepted as a list or a comma-separated string and stored as the comma-separated string the app reads, or null when empty.
- The 409 of a delete carries `bundle_count` in `developerMessage` (`{"code":"linked_bundles","bundle_count":n}`), which the portal's notice shows.

### Tag `data` built server-side (MS-5066)

`build_tag_data` in `group_service.py` builds `tag.data` with one rule for create and edit, mirroring `groupsAPI.jsx` but without `encodeURIComponent`:

| Group name (case-insensitive) | `data` |
|---|---|
| `regions` | `guid` = tag id, `icon` = icon URL, `zone_name` = name, `region_code` = name with spaces replaced by `_`, `region_name` = name. Rebuilt on every save |
| `countries`, new tag only | `id` = tag id, `icon`, `country` = name, `iso3_code` = `country_code` = name with `_`, `zone_name` = `Unknown`, `operator_list` = null. An existing country keeps its synced data |
| every tag | `alternative_country` = keywords joined by commas, or null |

Existing data of an edited tag is kept and the payload's `data` (if any) is merged over it before these keys are set.

### Icons

The `MediaStorageService` from the foundation handles the files: bucket `media`, folder = the group name (lower-cased, letters, digits, `-` and `_`; the portal used the URL-encoded name as the folder), file name generated by the server. `tag.icon` stores the public URL, as today.

| Action | Order |
|---|---|
| Create | validate and upload every icon → `admin_insert_group_with_tags` → on any failure (409, 422 on the second icon, 502…) `remove_staged` deletes every file just uploaded |
| Edit | read the group (404 before any upload) → validate the tag ids and icon keys (422 before any upload) → upload the new icons → `admin_edit_tag_group` → on failure `remove_staged`; on success `remove_after_commit` of the replaced icons, the icons cleared with `remove_icon` and the icons of the deleted tags |
| Delete | `admin_delete_group` (usage check inside the transaction; 409 touches nothing) → `remove_after_commit` of the tag icons |

A failed delete is queued in `storage_cleanup_queue` and never fails the request.

### APP_CACHE_KEY and audit

Every group write rotates `APP_CACHE_KEY` (`ROTATE_CACHE = True` in `group_service.py`; the SQL functions take `p_rotate_cache`). The portal rotates it today on add, edit and tag reorder. Delete and the status switch did not rotate it in the portal; the plan's catalog and MS-5085 ask for rotation on every group write, so they rotate too. The user confirmed this on 2026-10-21: rotate on every group write.

Every write is audited in the same transaction: `tag_group` INSERT / UPDATE / DELETE rows with `{group, tags}` snapshots as old and new data, `operation_details.record_id` = the group id (a bigint, so `audit_log.record_id` stays null), the number of tags inserted / updated / deleted, `action: reorder_tags` for a reorder, the IP from the request and the status reason.

## Files

### New

| File | Purpose |
|---|---|
| `app/schemas/admin/groups.py` | `AdminGroup`, `AdminGroupDetail`, `AdminTag`, `AdminGroupCreate` / `AdminGroupUpdate` (the multipart `payload`, extra forbidden), `AdminTagOrder`, `AdminGroupStatus`, field limits, `normalize_alternative_country` |
| `app/services/admin/group_service.py` | List, detail, tags, create, update, delete, status, reorder; `build_tag_data` |
| `migrations/admin/070_groups.sql` | `admin_insert_group_with_tags`, `admin_edit_tag_group`, `admin_delete_group`, `admin_reorder_tags` (+ internal `admin_group_snapshot`, `admin_lock_group`, `admin_assert_group_args`) |
| `tests/admin/api/test_groups_api.py`, `tests/admin/services/test_group_service.py` | HTTP contract; service, tag data and repositories |
| `adminDocs/MS-5085-groups.md` | This record |

### Changed (admin files of this track)

| File | Change |
|---|---|
| `app/api/v1/admin/groups.py` | The lookup line kept first; the eight routes above and the multipart `payload` + `icon_*` parsing (unknown form fields → 422; the permission is checked before any file is read) |
| `app/repo/admin/tag_group_repo.py` | `lookup` unchanged; `search` (with `tag(count)`), `facets`, `get_with_tags` (embed ordered by `sorting_number, name`, `tag.name` filter), `top_countries_count`, the four RPC wrappers, `UNIQUE_CONFLICTS` |
| `app/repo/admin/tag_repo.py` | `lookup` unchanged; `by_group` |

`UNIQUE_CONFLICTS`: `tag_group_name_key` (the `unique` on `tag_group.name` in supabase_ddl.sql) → `duplicate_group_name`; `unique_tag_name_per_group` (`020_constraints.sql`) → `duplicate_tag_name`.

## SQL

`migrations/admin/070_groups.sql`, idempotent, EXECUTE for `service_role` only, not run. Rewrites of the portal's `src/core/procedures/{insert_group_with_tags,edit_tag_group,delete_group_if_no_bundle}.sql`:

| Function | Differences from the legacy one |
|---|---|
| `admin_insert_group_with_tags(p_name, p_type, p_group_category, p_tags, p_actor, p_ip, p_reason, p_rotate_cache)` → `{group, tags}` | Raises instead of returning `{error}`; audit row; cache rotation; a missing tag id is generated |
| `admin_edit_tag_group(p_id, …, p_new_tags, p_updated_tags, p_deleted_tag_ids, p_actor, …)` → `{group, tags}` | Locks the group (AD404); AD422 `tag_not_in_group` when an updated or deleted tag belongs to another group; AD409 `linked_bundles` + `bundle_count` when a deleted tag is assigned to a bundle (the legacy function failed with a foreign key error); still removes `tag_translation` rows of deleted tags and syncs `tag_translation.data` of updated tags; sets `updated_at`; audit; rotation |
| `admin_delete_group(p_id, p_actor, …)` → `{group, tags}` | AD409 `linked_bundles` with `bundle_count` (the legacy one returned `{code:500, bundles}` while the portal expected `code:"linked-bundles"` + `bundle_count`, MS-5085); deletes `tag_translation` and `tag` rows explicitly; audit; rotation |
| `admin_reorder_tags(p_group_id, p_tag_ids, p_actor, …)` → `{tag_ids}` | New: `sorting_number` = position; AD422 when a tag is repeated, of another group or missing; audit with the old and new order; rotation. Replaces the portal's upsert of whole tag rows |

Dependencies: `000_schema_drift.sql` (`tag.sorting_number`, `tag_group.group_category`), `010`, `020` (unique `tag(name, tag_group_id)`), `050_admin_audit_helpers.sql` (`admin_write_audit`, `admin_rotate_cache_key`), `130_storage_cleanup_queue.sql`. **Until 070 is applied, create, edit, delete and reorder return 502 against a real database**; list, detail, tags and status (which uses `admin_update_audited`) work with the foundation SQL.

### Review notes

- APP_CACHE_KEY rotation on delete and on the status switch is new (the portal did neither). Both are one `p_rotate_cache` argument; `ROTATE_CACHE` in `group_service.py` switches all group writes at once.
- Icon folder: the server sanitises the group name (`countries`, `regions`), where the portal used `encodeURIComponent(name)` (`Countries`, `Middle%20East`). Old files stay where they are; `path_from_url` resolves both for deletion.
- `GET /groups/{id}/tags` has no paging (the plan's catalog gives it no parameters); the bundles page needs every tag of a group at once. MS-5085 mentions `?page_size`.
- `GET /groups/{id}` reads `TOP_COUNTRIES_COUNT` only for the group named Countries, as the portal did (`getTopCountriesCount` was called for that group only).
- Create requires at least one tag (MS-5085 edge case: "0 tags on create → 422 unless product says otherwise"); the portal always sends one.
- `tag_group.name` is accepted up to 29 characters because of the database check `length(name) < 30`.
- Delete of a tag assigned to a bundle (through `PUT /groups/{id}` `deleted_tag_ids`) is refused with 409 `linked_bundles` like the group delete. The legacy function failed with a raw foreign key error.

## Tests

```powershell
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m pytest tests/admin/api/test_groups_api.py tests/admin/services/test_group_service.py -q -p no:cacheprovider
```

100 tests: groups API 58, group service and repositories 42. All pass. They cover:

- 200 lists with `tag_count` and `facets`, the detail with ordered tags and `top_countries_count`, the tags route, 201 with `responseCode` 201, 204 with an empty body;
- 401 without a session; 403 for an admin missing `admin.groups.write` (and no file read), or `admin.groups.read`;
- 404 (detail, tags, update, delete twice, status), the lookup route still answered before `/{id}`;
- 409 `duplicate_group_name`, `duplicate_tag_name`, `linked_bundles` with `bundle_count` (delete and edit), `no_change`;
- 413 on an icon;
- 422: `page_size=101`, `page=0`, unknown query parameter, bad status, non-integer id, payload missing / not JSON / blank or 30-character name / `type=3` / no tags / repeated `client_key` / extra field, unknown multipart field, `icon_` without a key, icon for an unknown tag, bad uuid, repeated ids, tag of another group, reorder body errors;
- the tag data: identical `region_code` on create and edit with no `%20` (MS-5066), country data for new countries only, keywords normalised;
- the icon lifecycle: upload then one RPC, staged files removed when the RPC or a later upload fails, nothing written when an upload fails, replaced / removed / deleted icons removed after the commit, a 409 on delete touches no file (MS-5064);
- the repositories: RPC names and parameters (actor, ip, reason, rotate), the 23505 mapping per constraint, AD409 detail → `bundle_count`, the embedded count select, the ordered `tag(*)` embed with the escaped `tag.name` filter, `TOP_COUNTRIES_COUNT` parsing, `by_group` order.

Full admin suite: see the end of this file.

## Bugs fixed

| Bug | Fix |
|---|---|
| MS-5064: a refused group delete had already deleted the tag icons | The usage check runs inside `admin_delete_group`; the API removes icons only after a successful delete |
| MS-5066: region tags URL-encoded on create, raw on edit | `build_tag_data` on the server, one rule, raw names. The data fix of existing rows containing `%` is not part of this story (needs the team's confirmation of who consumes `region_code`) |
| MS-5070 (groups): browser uniqueness pre-checks | Unique constraints; 409 `duplicate_group_name` / `duplicate_tag_name` |
| MS-5071 (groups): writes not audited, status switch and delete not audited at all | Every write audited server-side in the same transaction |
| Portal `deleteGroup` expected `code:"linked-bundles"` + `bundle_count` while the RPC returned `code:500` + `bundles` | 409 `{"code":"linked_bundles","bundle_count":n}` |

## Full admin suite

`tests/admin`: see the hand-over (run on 2026-10-21 with the other wave-2 tracks' tests in the tree).
