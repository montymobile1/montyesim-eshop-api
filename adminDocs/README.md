# Admin API: rules and story records

This folder documents the admin API work tracked under MS-5017. The admin portal moves off direct Supabase access and calls `/api/v1/admin/*` on this backend instead. The full design and the story breakdown are in the admin portal repo, in `docs/admin-api-migration-plan.md`.

Each story gets one record in this folder, named after its Jira key, listing every change it made.

| Story | Record | Status |
|---|---|---|
| MS-5075, admin API foundation | [MS-5075-admin-foundation.md](MS-5075-admin-foundation.md) | Done |

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
