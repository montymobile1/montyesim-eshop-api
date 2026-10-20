# MS-5077: lookups (dropdown sources)

Story: MS-5077, admin API lookups for users, bundles, promotion rules, groups and tags. Portal counterpart: MS-5078. Branch: `TEOS-139-be-planning-for-changes`. Date: 2026-10-20.

Status: done and tested. Nothing is committed and no SQL was written or applied (the story needs no new database objects).

## No current flow changed

- All the code is in admin files. No existing service, router, repository, schema, `main.py` or SQL function was touched.
- `app/repo/admin/users_copy_repo.py` (foundation file) only gained the `lookup` method and two imports; `by_ids` is unchanged.
- The resource router files (`users.py`, `bundles.py`, `groups.py`, `tags.py`, `promotion_rules.py`) only hold the lookup route. The page stories add their routes after it.
- The RPC `get_tags_with_group_all` is no longer needed by the API but is left in place: the bundles page still calls it until its own story (MS-5083/MS-5084); it is dropped in the clean-up story.

## Endpoints

All under `/api/v1/admin`, plain PostgREST queries with an exact count.

Query parameters (all optional): `search` (literal, at most 100 characters), `page` (1-based, default 1), `page_size` (1..100, default 20), `id` (returns only that item, so the portal can show a dropdown's label after a reload; added during the wave 1 review).
Response `data`: `{data: [{id, label, ...extra}], total_count}`; top-level `totalCount` = `total_count`. `id` is always a string (group ids are bigint in the database).

| Method and path | Permission | Search on | Item | Order | Source |
|---|---|---|---|---|---|
| GET `/users/lookup` | `admin.users.read` | `email` | `{id, label: email, email}`; users without an email are left out | email, id | `users_copy` |
| GET `/bundles/lookup` | `admin.bundles.read` | `data->>bundle_code` | `{id, label: "display_title (bundle_code)", bundle_code}`; label is just the code when the title is empty | `data->>display_title`, id | `bundle` |
| GET `/groups/lookup` | `admin.groups.read` | `name` | `{id, label: name, type, group_category}` | name, id | `tag_group` |
| GET `/tags/lookup` | `admin.tags.read` | tag `name` | `{id, label: "name (group name)"}` | name, id | `tag` + `tag_group(name)` embed (replaces `get_tags_with_group_all`) |
| GET `/promotion-rules/lookup` | `admin.rules.read` | `name` | `{id, label: name}` | name, id | `promotion_rule` |

Errors (`developerMessage`):

| Status | When |
|---|---|
| 401 | no or invalid session |
| 403 | the admin lacks the resource's read permission (`developerMessage` = the permission) |
| 422 | `page_size` > 100 or < 1, `page` < 1, `search` longer than 100, any unknown query parameter |
| 502 `DATABASE_ERROR` | database failure |

Like the portal today, inactive bundles and inactive groups are included (the dropdowns filter existing rows, active or not).

## Files

### New

| File | Purpose |
|---|---|
| `app/api/v1/admin/_lookup.py` | `add_lookup_route(router, method, permission)`: the shared `GET /lookup` route (params, permission, envelope) |
| `app/repo/admin/bundle_repo.py` | `BundleAdminRepo.lookup` (MS-5083 adds the page methods) |
| `app/repo/admin/tag_group_repo.py` | `TagGroupAdminRepo.lookup` (MS-5085 adds the page methods) |
| `app/repo/admin/tag_repo.py` | `TagAdminRepo.lookup` |
| `app/repo/admin/promotion_rule_repo.py` | `PromotionRuleAdminRepo.lookup` (MS-5087 adds the page methods) |
| `app/services/admin/lookup_service.py` | `LookupService`: rows to `{id, label, ...extra}` items |
| `tests/admin/api/test_lookups_api.py` | HTTP contract |
| `tests/admin/services/test_lookup_service.py` | Repository queries and item mapping |

No new schema file: items use the foundation's `LookupItem` and `AdminPage`.

### Changed (admin files only)

| File | Change |
|---|---|
| `app/api/v1/admin/users.py`, `bundles.py`, `groups.py`, `tags.py`, `promotion_rules.py` | One `add_lookup_route(...)` line each, above any future `/{id}` route |
| `app/repo/admin/users_copy_repo.py` | `lookup` added; `by_ids` unchanged |

## SQL

None. The story reads `users_copy`, `bundle`, `promotion_rule`, `tag_group` and `tag` only. Columns used that may be missing on old databases are added by the foundation's `migrations/admin/000_schema_drift.sql`: `promotion_rule.name` (filled by the trigger in `040_triggers.sql`) and `tag_group.group_category`.

Index advice for the team leader (story case 15, page 1 under 200 ms with 100k users): `users_copy.email` is `unique`, so ordering by email uses its index. Searching with `ilike '%x%'` cannot use a btree index; if it proves slow, a `pg_trgm` GIN index on `users_copy.email` and on `(bundle.data->>'bundle_code')` would help. Not written, since the story says "no new objects".

## Review notes

- Search is literal for `%`, `_` and `\` (escaped by the foundation's `like`). PostgREST also treats `*` as a wildcard in `ilike` values; the foundation's `AdminBaseRepository.like` does not escape it, so a search for `*` matches everything. This is a foundation point (`app/repo/admin/base.py`), reported, not changed here.
- The bundle search and order use the JSON paths `data->>bundle_code` / `data->>display_title`, which the column guard of `filters()` / `paged(order=...)` does not accept, so `BundleAdminRepo.lookup` calls `ilike` / `order` directly with constant paths; the user's text still goes through `like()`.
- Portal values that differ from today: the bundle dropdowns on the promotion pages use the bundle code as value and label today; the API's label is `"display_title (bundle_code)"` and `bundle_code` is on each item, so a page can keep the code as value (`valueKey="bundle_code"` in the portal's `LookupSelect`).

## Tests

```powershell
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m pytest tests/admin -q -p no:cacheprovider
```

Lookup tests: 75 passed (`tests/admin/api/test_lookups_api.py` 47, `tests/admin/services/test_lookup_service.py` 28). Whole `tests/admin` run (with the other tracks' work in the tree): 432 passed. The tests patch the repositories or the service and never reach the network.

## Bugs fixed

- Dropdown paging no longer guesses `hasMore` from `data.length === pageSize` (an extra empty request on exact multiples): `total_count` is exact.
- Dropdown search is literal (`%` and `_` no longer act as wildcards; commas and quotes no longer break the query), as in MS-5068.
- Order is stable across pages (label, then id), so options are not repeated or skipped (as in MS-5067).
