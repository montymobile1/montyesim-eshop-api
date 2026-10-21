# MS-5087, MS-5050, MS-5052, MS-5054: promotion rules, promotions and promo analysis admin API

Stories: MS-5087 (promotion rules), MS-5050 (promotions list, status, delete), MS-5052 (promotion add and edit), MS-5054 (promo usage analysis). Bugs fixed: the rules and promotions parts of MS-5070 (browser uniqueness races) and MS-5071 (unaudited writes), the filter-string injection of MS-5050, MS-5069 (`id_text` only in the admin repo's SQL). Portal counterpart: `docs/adminDocs/MS-5088-MS-5051-MS-5053-MS-5055-promotions.md` in the admin portal repo. Branch: `TEOS-139-be-planning-for-changes`. Date: 2026-10-21.

Status: done and tested with mocks. Nothing is committed. One SQL file was written (`100_promotions.sql`) and nothing was run against any database.

## No current flow changed

No existing flow or behavior of the backend changed.

- All code is in new admin files, plus the two router files that were already mounted (`promotion_rules.py`, which kept its lookup line, and `promotions.py`, which was empty).
- No existing service, router, repository, schema, model, `main.py` or shared admin file was edited. `app/repo/admin/promotion_rule_repo.py` (the MS-5077 lookup repository of this area) was extended; its `lookup` method is unchanged and still tested.
- The checkout flow that reads promotions and writes `promotion_usage` is untouched. The new SQL only adds read functions and indexes.

## Endpoints

All under `/api/v1/admin`. Lists use `page` (1-based), `page_size` (at most 100) and the standard list envelope. Any undeclared query parameter returns 422. Ids are uuids (a non-uuid path id returns 422).

### Promotion rules (MS-5087)

| Method and path | Permission | Request | Success | Errors (`developerMessage`) |
|---|---|---|---|---|
| GET `/promotion-rules` | `admin.rules.read` | `search` (part of the id, literal on `id_text`), `action_id`, `event_id` | 200 `{data:[{id,id_text,name,max_usage,beneficiary,created_at,action:{id,name},event:{id,name}}], total_count}` | 401, 403, 422 |
| GET `/promotion-rules/lookup` | `admin.rules.read` | MS-5077, unchanged | | |
| GET `/promotion-rules/actions`, `/events` | `admin.rules.read` | none | 200 `[{id,name}]` | 401, 403 |
| POST `/promotion-rules` | `admin.rules.write` | `{promotion_rule_action_id ≥ 1, promotion_rule_event_id ≥ 1, max_usage 1..2147483647, beneficiary 0\|1\|2}` | 201 rule | 409 `duplicate_rule`, 422 (unknown action or event, bad values, extra field) |
| PUT `/promotion-rules/{id}` | `admin.rules.write` | same body | 200 rule (also when nothing changed) | 404, 409 `duplicate_rule`, 422 |
| DELETE `/promotion-rules/{id}` | `admin.rules.write` | none | 204 | 404, 409 `rule_in_use` |

- `name` is filled by the `update_promotion_rule_name` trigger (`040_triggers.sql`); the API re-reads the row after a write so the response carries it.
- The unique index `promotion_rule_action_event_max_usage_key` (`020_constraints.sql`) gives the 409; the FK from `promotion.rule_id` gives `rule_in_use` (23503 → `referenced` → `rule_in_use`).
- An unknown action or event id is refused before the write (422), and a 23503 on insert/update is also mapped to 422.

### Promotions (MS-5050, MS-5052)

| Method and path | Permission | Request | Success | Errors |
|---|---|---|---|---|
| GET `/promotions` | `admin.promotions.read` | `search` (name or code, literal), `status=all\|active\|inactive\|expired\|scheduled`, `rule_ids` (repeated), `bundle_codes` (repeated), `include_global` | 200 `{data:[row], total_count, facets:{all,active,inactive,expired,scheduled}}` | 401, 403, 422 |
| GET `/promotions/{id}` | `admin.promotions.read` | none | 200 row | 404, 422 |
| POST `/promotions` | `admin.promotions.write` | `{name 2..100, code ^[A-Za-z0-9_-]{2,50}$, rule_id, amount? > 0, valid_from, valid_to > valid_from, bundle_codes[0..5]}` | 201 row | 409 `duplicate_code` / `duplicate_name`, 422 (unknown rule, amount missing for an amount rule, percentage over 100, unknown bundle code, a 6th code, a code with a comma, any other field such as `type`, `is_active`, `times_used`, `callback_url`, `callback_headers`) |
| PUT `/promotions/{id}` | `admin.promotions.write` | `{name, bundle_codes, valid_to?, code?, rule_id?, amount?, valid_from?}` | 200 row (also when nothing changed) | 404, 409 `duplicate_*`, 409 `{"code":"promotion_used","times_used":n,"fields":[...]}`, 422 |
| PATCH `/promotions/{id}/status` | `admin.promotions.write` | `{is_active, reason?}` | 200 row | 404, 409 `no_change`, 422 |
| DELETE `/promotions/{id}` | `admin.promotions.write` | none | 204 | 404, 409 `{"code":"promotion_used","times_used":n}` |

Row: `{id, name, code, type, amount, valid_from, valid_to, is_active, times_used, bundle_codes[], rule_id, rule:{id,name,max_usage,beneficiary,action:{id,name},event:{id,name}}, status, created_at}`. `callback_url` and `callback_headers` are never read or written by the API.

- Status tabs (same rule in SQL and in Python): `expired` = `valid_to` in the past; `scheduled` = not expired and `valid_from` in the future; `active` / `inactive` = `is_active` of the promotions inside their validity window. The four tabs partition `all`, so the counts add up. `status` on each row says which tab it belongs to.
- Facets count the searched set (search, rules, bundles), whatever the `status` filter.
- `bundle_codes` filter: whole codes matched against the stored CSV, with bound parameters (the portal used to build a PostgREST `or()` string from user input). `include_global=true` also lists the promotions with no bundle restriction, which is what the portal did before.
- The server sets `type = "Promo"`, `is_active = true`, `times_used = 0` on create. Dates are stored as UTC (`timestamp` without time zone) and returned as stored.
- `amount` is required when the rule's action is amount-based (`DISCOUNT_AMOUNT`, `CASHBACK_AMOUNT`) and capped at 100 for a percentage action; the rule is read to decide (unknown `rule_id` → 422).
- Bundle codes are checked by the `validate_bundle_codes` trigger (`040_triggers.sql`): `P0001 'Invalid bundle code: X'` becomes 422 `bundle_codes: unknown bundle code X`.
- Edit: `name`, `bundle_codes` and `valid_to` always change. `code`, `rule_id`, `amount` and `valid_from` are accepted and immutable once used: a different value with `times_used > 0` is 409 `promotion_used` before any write; with `times_used = 0` the update runs with `p_guard = {"times_used": 0}`, so a checkout racing the edit makes it fail with the same 409. Sending the current values is not a change.
- Delete: `times_used > 0` → 409 before the write; otherwise `admin_delete_audited` with `p_guard = {"times_used": 0}`. A `guard_failed` (used meanwhile) or a 23503 from `promotion_usage.promotion_code` / `user_order.promo_code` is also returned as `promotion_used`.
- Every write goes through the audited SQL helpers (one audit row in the same transaction). No write rotates `APP_CACHE_KEY`: the portal does not rotate it on promotions or rules today.

### Promo analysis (MS-5054)

| Method and path | Permission | Request | Success | Errors |
|---|---|---|---|---|
| GET `/promotions/usage` | `admin.promotions.read` | `code` (literal partial match), `user_id` (uuid), `bundle_ids` (uuids, repeated), `from`, `to` (days, inclusive, at most 366 days apart) | 200 `{data:[row], total_count, totals:{uses, distinct_users, discount_amount:[{currency, amount}]}}` | 401, 403, 422 (bad uuid or date, from after to, over 366 days, unknown parameter), 502 |

Row: `{id, created_at, promotion_code, promotion:{id,code,name}|null, user:{id,email,msisdn}|null, bundle_id, bundle_code, order_id, discount_amount, currency, status}`.

- `/usage` is declared before `/{promotion_id}`, so it is never read as an id (tested).
- Only rows with a promotion code are listed (as the portal did). The promotion, the user (`users_copy`) and the bundle code are joined in SQL; a deleted promotion or account gives `null` and the row is still listed and counted.
- `currency` comes from the order (`promotion_usage.order_id → user_order.currency`); rows without an order have `currency: null` and are summed under it. `totals.discount_amount` is one entry per currency.
- Totals are computed over every row the filters select, never over the page. The OpenAPI summary says so.
- `msisdn` on the user and `status` on the row are extras over the MS-5054 shape: the page shows them today.

## Files

### New

| File | Purpose |
|---|---|
| `app/schemas/admin/promotion_rules.py` | `AdminPromotionRuleBody` (extra forbidden), `AdminPromotionRule`, `AdminPromotionRuleRef` |
| `app/schemas/admin/promotions.py` | `AdminPromotionCreate`, `AdminPromotionUpdate` (both extra forbidden, dates in order, bundle codes cleaned), `AdminPromotion`, `AdminPromotionPage`, the usage row, totals and page, `PromotionStatus` |
| `app/repo/admin/promotion_repo.py` | `PromotionAdminRepo`: `search` through `admin_search_promotions`, `get` with the rule embed (no callback columns), `UNIQUE_CONFLICTS` |
| `app/repo/admin/promotion_usage_repo.py` | `PromotionUsageAdminRepo.search` through `admin_promotion_usage` |
| `app/services/admin/promotion_rule_service.py` | List, actions, events, create, update, delete, with the 409/422 mappings above |
| `app/services/admin/promotion_service.py` | List, get, usage, create, update (immutable fields, guard), status, delete, plus the shaping helpers |
| `migrations/admin/100_promotions.sql` | See SQL |
| `tests/admin/api/test_promotion_rules_api.py`, `tests/admin/api/test_promotions_api.py` | HTTP contract |
| `tests/admin/services/test_promotion_rule_service.py`, `tests/admin/services/test_promotion_service.py` | Services and repositories |
| `adminDocs/MS-5087-MS-5050-MS-5052-MS-5054-promotions.md` | This record |

### Changed (admin files of this area)

| File | Change |
|---|---|
| `app/api/v1/admin/promotion_rules.py` | The lookup line kept first; `/actions`, `/events`, list, create, update, delete added |
| `app/api/v1/admin/promotions.py` | Was empty; list, `/usage`, get, create, update, status, delete |
| `app/repo/admin/promotion_rule_repo.py` | `lookup` unchanged; `search`, `get`, `UNIQUE_CONFLICTS` added, plus `PromotionRuleActionAdminRepo` and `PromotionRuleEventAdminRepo` |

## SQL

`migrations/admin/100_promotions.sql` (the number from the plan's migrations table). Idempotent, service_role-only EXECUTE, never run:

- `admin_search_promotions(p_search, p_status, p_rule_ids uuid[], p_bundle_codes text[], p_include_global, p_limit, p_offset)` → `{data, total, facets}`. Literal search (`%`, `_`, `\` escaped), the status buckets above, whole-code bundle matching, rule and action/event embedded with the same keys as the PostgREST embed, order `created_at desc, id desc`.
- `admin_promotion_usage(p_code, p_user_id, p_bundle_ids uuid[], p_from, p_to exclusive, p_limit, p_offset)` → `{data, total, totals}` with the joins and the totals per currency.
- `admin_promotions_like(text)`: internal helper, no grant.
- Indexes `IF NOT EXISTS`: `promotion_usage (promotion_code, created_at desc)`, `promotion (valid_from)`, `promotion (valid_to)` from the plan, plus `promotion_usage (created_at desc, id desc)` for the default order of the usage page (one more than the plan lists).

The stories also depend on the foundation's SQL: `000` (`promotion_rule.name`, `promotion_usage.order_id`), `020` (unique rule index; `promotion_name_key` and `promotion_code_key` exist in `supabase_ddl.sql` already), `030` (`id_text`), `040` (the two triggers), `050` (audited writes). **Until `100_promotions.sql` is applied, `GET /promotions` and `GET /promotions/usage` return 502**; the other routes work with the foundation SQL.

## Tests

```powershell
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m pytest tests/admin -q -p no:cacheprovider
```

The four new files have 158 tests (two API files, two service and repository files). All pass. They cover:

- 200 with the envelope and facets or totals, 201 (`responseCode` 201), 204 with an empty body, paging defaults;
- 401 without a session; 403 for an admin missing the write or read permission, with nothing written;
- 404, delete twice (204 then 404), a non-uuid id (422);
- 409 `duplicate_rule`, `rule_in_use`, `duplicate_code`, `duplicate_name`, `no_change`, `promotion_used` with `times_used` (and `fields` on edit), mapped from `referenced` / `guard_failed` where applicable;
- 422: `page_size` 0 or 101, unknown parameter, bad status, non-uuid `rule_ids` / `user_id` / `bundle_ids`, bad date, from after to, over 366 days, `max_usage=0`, `beneficiary=3`, unknown action, forbidden body fields (`type`, `is_active`, `times_used`, `callback_url`, `callback_headers`), code format (spaces, 1 or 51 characters), `valid_to ≤ valid_from`, 6 bundle codes, a code with a comma, an amount-based rule without amount, a percentage over 100, unknown rule, unknown bundle code (trigger message);
- the repositories: unique and FK mappings, the trigger `P0001` → 422, the RPC names and bound parameters (search text with `,` `)` `%` passed as a value), the list-wrapped RPC result, the `created_to` exclusive end, 502 on a database error;
- the shaping: `bundle_codes` split, status buckets at the boundary, UTC timestamps, orphans in usage rows, totals per currency;
- `/promotions/usage` not read as an id, `/promotion-rules/lookup` still above `/{id}`.

Full admin suite: see the end of this file.

## Bugs fixed

| Bug | Fix |
|---|---|
| MS-5069: `id_text` existed only in the admin repo's SQL | `030_promotion_rule_id_text.sql` (foundation) + the rules list searches it |
| MS-5070 (rules, promotions): browser read-then-write uniqueness checks raced | Unique indexes; 409 `duplicate_rule` / `duplicate_code` / `duplicate_name` from the database |
| MS-5071 (rules, promotions): writes not audited | Every write audited server-side in the same transaction |
| MS-5050: search and bundle filters interpolated into a PostgREST `or()` string | `admin_search_promotions` with bound parameters; `a,code.ilike.%` is a literal search |
| MS-5050: "used" check and delete as two browser calls | `times_used` check plus `p_guard` and the FK, in the delete transaction |
| MS-5052: every `promotion` column writable from the browser (`callback_url`, `times_used`, `is_active`, `type`) | Strict bodies (`extra="forbid"`); the server sets `type`, `is_active`, `times_used` |
| MS-5054: usages filtered by `bundle_id in (<bundle codes>)` (codes compared with uuids, never matched) | `bundle_ids` are uuids from the bundles lookup |

## Deviations and review notes

- **Status buckets**: MS-5050 defines `expired` and `scheduled` only. `active` / `inactive` are restricted to promotions in their validity window so the five facets are consistent (`all` = sum of the four). An expired promotion with `is_active = false` counts as `expired`.
- **PUT body** (MS-5052 says `{name, bundle_codes, valid_to?}`): `code`, `rule_id`, `amount` and `valid_from` are also accepted, and refused with 409 `promotion_used` once used. Without them the contract's "409 when immutable fields change" could never happen. The portal only sends them for an unused promotion.
- **`amount?`**: optional only for percentage rules; required for amount rules. The portal still requires it in both cases, as before.
- **Amounts are JSON numbers**, not decimal strings (`real` columns; the portal formats numbers), as for vouchers.
- **Timestamps** are returned without an offset (the columns are `timestamp` without time zone, stored in UTC), as every other admin row.
- **`include_global`**: the portal sends `true` with a bundle filter to keep today's result (global promotions were always included).
- **MS-5050 "FK `promotion_usage.promotion_code → promotion.code ON DELETE RESTRICT` if absent"**: the FK already exists in `supabase_ddl.sql` (and `user_order.promo_code` too), so no SQL was added for it.
- **Extra index** `promotion_usage (created_at desc, id desc)`; drop it from `100_promotions.sql` if unwanted.
- The rules lookup (`/promotion-rules/lookup`) returns `{id, label: name}` only; the portal derives "amount or percentage" from the rule name (`DISCOUNT_AMOUNT-...`), which the trigger builds from the action name. Adding `action_name` to the lookup would need `lookup_service.py` (shared file).

## Full admin suite

`tests/admin`: 900 passed (run on 2026-10-21, other wave-2 tracks' tests included; 439 before the wave).
