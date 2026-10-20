# Admin API: rules and story records

This folder documents the admin API work tracked under MS-5017. The admin portal moves off direct Supabase access and calls `/api/v1/admin/*` on this backend instead. The full design and the story breakdown are in the admin portal repo, in `docs/admin-api-migration-plan.md`.

Each story gets one record in this folder, named after its Jira key, listing every change it made.

| Story | Record | Status |
|---|---|---|
| MS-5075, admin API foundation | [MS-5075-admin-foundation.md](MS-5075-admin-foundation.md) | Done |
| MS-5077, lookups (dropdown sources) | [MS-5077-lookups.md](MS-5077-lookups.md) | Done (wave 1) |
| MS-5081, orders, devices, contact us | [MS-5081-orders-devices-contact-us.md](MS-5081-orders-devices-contact-us.md) | Done (wave 1) |
| MS-5089, MS-5058, MS-5060, vouchers and banners | [MS-5089-MS-5058-MS-5060-vouchers-banners.md](MS-5089-MS-5058-MS-5060-vouchers-banners.md) | Done (wave 1) |
| MS-5091, MS-5046, MS-5048, settings, settings logs, audit log | [MS-5091-MS-5046-MS-5048-settings-audit-log.md](MS-5091-MS-5046-MS-5048-settings-audit-log.md) | Done (wave 1) |
| Wave 1 shared changes (no story) | [wave-1-coordination.md](wave-1-coordination.md) | Done (wave 1) |

The migration plan is in the admin portal repo, `docs/adminDocs/admin-api-migration-plan.md`.

## Waves

The stories are delivered in waves. A wave is a group of stories developed in parallel, because none of them depends on another one in the same wave. Each wave starts once the previous one is reviewed.

| Wave | Stories (backend / portal) | Content | Status |
|---|---|---|---|
| Foundation | MS-5075 / MS-5076 | Admin login, roles and permissions, base repository, audit, image storage, API client in the portal | Done |
| Wave 1 | MS-5077 / MS-5078 | Lookups (dropdown sources) | Done |
| Wave 1 | MS-5081 / MS-5082 | Orders, devices, contact us | Done |
| Wave 1 | MS-5089, MS-5058, MS-5060 / MS-5090, MS-5059, MS-5061 | Vouchers, banners list, banner add and edit | Done |
| Wave 1 | MS-5091, MS-5046, MS-5048 / MS-5092, MS-5047, MS-5049 | Settings, settings logs, audit log | Done |
| Wave 2 | MS-5079 / MS-5080 | Users and user detail | To do |
| Wave 2 | MS-5083 / MS-5084 | Bundles and assign to groups | To do |
| Wave 2 | MS-5085 / MS-5086 | Groups add, edit, reorder | To do |
| Wave 2 | MS-5087, MS-5050, MS-5052, MS-5054 / MS-5088, MS-5051, MS-5053, MS-5055 | Promotion rules, promotions, promo analysis | To do |
| Wave 2 | MS-5056 / MS-5057 | Referrals and export | To do |
| Wave 3 | MS-5093 / MS-5094 | Clean-up: drop the legacy SQL, remove supabase-js from the portal | To do |

Wave 2 waits for wave 1 because its pages share router files with the lookups (users, bundles, groups, tags, promotion rules). Wave 3 waits for every page.

These waves group the Jira stories. Section 5 of the migration plan also numbers waves (0 to 3) by technical slice (B0, F0a...); the stories above are the reference.

## Review guide for the team leader

### What to read

1. For each story: its record above. Each record has the same sections: "No current flow changed", endpoints, files (new and changed), SQL, tests, bugs fixed.
2. For wave 1: also [wave-1-coordination.md](wave-1-coordination.md), the shared files prepared before the stories and adjusted during the review.
3. Check the rule of this work: every file in the diff is under an admin folder (`app/*/admin/`, `tests/admin/`, `migrations/admin/`, `adminDocs/`), except the appended lines listed in the foundation record and the guard test change listed in the wave 1 coordination record.

### SQL to apply

Nothing has been applied. Apply the whole folder in order, twice, to confirm it is idempotent (command in `migrations/admin/README.md`). Review `000_schema_drift.sql` against the deployed database first.

| File | Added by | Needed by |
|---|---|---|
| `000_schema_drift.sql` | Foundation | Banners list and save (`banner.is_active`); promotion rule and group lookups (`promotion_rule.name`, `tag_group.group_category`) |
| `010_audit_log.sql` | Foundation | Every audited write; audit log and settings history |
| `020_constraints.sql` | Foundation | 409 duplicates: voucher code and banner title regardless of case, tags, settings keys, promotions |
| `030_promotion_rule_id_text.sql`, `040_triggers.sql` | Foundation | Promotion rules and promotions (wave 2); `040` also fills `promotion_rule.name` |
| `050_admin_audit_helpers.sql` | Foundation | Every write through the admin API (vouchers, banners in wave 1) |
| `110_audit_search.sql` | MS-5048 | Audit log and settings history |
| `120_settings.sql` | MS-5091 | Settings save |
| `130_storage_cleanup_queue.sql` | Foundation | Retry of image deletions that failed (banners) |
| `140_orders_devices_indexes.sql` | MS-5081 | Speed of the orders and devices lists only |

Until these are applied, the admin endpoints work in tests only; the pages that need them return 502 against a real database. Give at least one account the `super_admin` role with `900_seed_super_admin.sql.example` to sign in.

## Rules for every admin story

These rules apply to every story in this work. A reviewer should reject a change that breaks one of them.

### 1. No current flow or behavior changes

The mobile app, the website, payments, webhooks, callbacks, the scheduler and every existing endpoint must behave exactly as before the story.

- Do not edit existing services, routers, repositories, schemas, models, `main.py`, `app/dependencies/security.py` or `app/repo/base_repo.py`.
- Do not change the path, request, response, status codes or error messages of any existing endpoint.
- Do not change how existing code reads configuration, the database or Supabase Auth.

### 2. Additive only

All admin code goes in new files under these admin folders:

| Kind | Location |
|---|---|
| Routes | `app/api/v1/admin/` (every router built with `admin_router()`) |
| Services | `app/services/admin/` |
| Repositories | `app/repo/admin/` |
| Schemas | `app/schemas/admin/` |
| SQL | `migrations/admin/` |
| Tests | `tests/admin/` |

Only these edits to existing files are allowed, and only by appending:

- one `include_router` line in `app/api/v1/admin/__init__.py` per new admin router (the line in `app/api/v1/__init__.py` already exists);
- new members at the end of `ErrorMessages` in `app/config/constants.py` and of `DatabaseTables` in `app/config/db.py`;
- new keys at the end of `locales/en.json` and `locales/ar.json`;
- new lines in `requirements.txt` and `.env.example`;
- the admin allowance in `tests/mcp/test_legacy_unchanged.py` (already in place; new admin endpoints and `Admin*` schemas need no further edit).

Anything else needs the team leader's agreement first, recorded in the story's file.

### 3. Database

- New or rewritten database functions are named `admin_*`. Existing functions, tables and columns are not changed or dropped, except in the final clean-up story.
- SQL goes in `migrations/admin/` as idempotent files. Developers write them and never run them; the team leader reviews and applies them.

### 4. Process

- No commits and no Jira changes from the development session; the developer checks the result and the team leader reviews.
- Every story adds tests in `tests/admin/` and keeps the existing test results unchanged. Compare the non-admin test run against the base branch before handing over.
- Each story adds its record to this folder, with the list of changed files and the statement that no current flow changed, or what was agreed otherwise.
