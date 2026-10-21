# MS-5093: legacy admin functions handed to the lead, admin API documented

Story: MS-5093 (retire the legacy admin database functions and document the admin API). Portal counterpart: MS-5094 (portal clean-up), record `docs/adminDocs/MS-5094-cleanup.md` in admin-open-source. Date: 2026-10-21.

## No current flow changed

- No database function, table or policy is dropped or changed, and no SQL file is added to `migrations/admin/`.
- No code changed. The only edits are documents: this record, `adminDocs/lead-review-old-flow.md`, `adminDocs/README.md` and an appended "Admin API" section in `README.md`.

## Deviation from the Jira text (user decision, 2026-10-21)

MS-5093 asks for `migrations/admin/990_drop_legacy.sql`, which drops twelve functions after checking that nobody calls them. The user decided that the developer drops nothing from the current flow. Instead:

- the twelve functions, what used them and what replaces them are listed in `adminDocs/lead-review-old-flow.md`;
- the same file holds the proposed check queries and the commented-out `DROP` statements, for the team leader to run if and when they decide, after the new portal is deployed to that environment.

Jira test cases 1 and 5 (idempotent drop, refusal when a function was called recently) therefore do not apply; the check query in the review file covers the intent of case 5.

## What was checked

| Check | Result |
|---|---|
| The admin portal calls none of the twelve functions (after MS-5094) | Yes; the procedures folder is deleted except `rls-db-format.sql` |
| Backend `app/` calls any of them (Jira case 6) | No. Only the new `admin_*` functions are called; two Python repository methods keep the names `insert_group_with_tags` and `edit_tag_group` and call the `admin_` versions |
| The website (subscriber-open-source-3) calls any of them | No |
| The mobile app, other clients, the deployed database | Not checked from here; listed for the lead |

## Documentation

- `README.md`: new "Admin API" section before "Contributing": base path, auth, permissions, list and error envelope, how to apply `migrations/admin`, how to seed a super admin, admin settings, how to run `tests/admin` (Jira "README section").
- `.env.example`: reviewed; the five `ADMIN_*` settings are present, `ADMIN_MAX_UPLOAD_BYTES` is 1 MB.
- `/docs` tag descriptions: not added. They need `openapi_tags` in `app/main.py`, which the admin work does not edit.

## Files

| File | Change |
|---|---|
| `adminDocs/lead-review-old-flow.md` | Section "Legacy database functions the admin portal no longer calls (MS-5093)" with the list and the proposed SQL |
| `adminDocs/MS-5093-legacy-review-and-docs.md` | New, this record |
| `adminDocs/README.md` | Index row, wave 3 status |
| `README.md` | Appended "Admin API" section |

## Tests

No code changed, so the test results are those of the wave-2 commit (901 admin tests). The guard `tests/mcp/test_legacy_unchanged.py::test_only_expected_files_were_modified` fails while `README.md` is uncommitted and passes once committed, like every earlier wave.
