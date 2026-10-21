# Admin API migrations

SQL for the admin API (`/api/v1/admin`, plan MS-5017, foundation MS-5075).
It adds the columns, constraints, triggers and `admin_`-prefixed functions
the admin endpoints rely on. Nothing here changes or drops what the
storefront or the legacy admin portal uses today. The legacy RPCs are retired
later by `990_drop_legacy.sql`, in the cleanup wave only.

## Apply

```sh
cat migrations/admin/[0-9]*.sql | psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --single-transaction
```

- Files run in name order (`000`, `010`, `020`, ...). The glob skips
  `README.md` and `*.sql.example`.
- `--single-transaction` plus `ON_ERROR_STOP` means a failing file rolls
  back the whole run. Nothing is half-applied.
- Only the team leader applies migrations, after review. Never apply them
  from CI or from a developer machine against a shared database.

## Before the first run: review `000_schema_drift.sql` against the deployed DB

`supabase_ddl.sql` (qa) and the deployed databases have drifted apart.
Compare every column in the header table of `000_schema_drift.sql` with the
target database, for example with
`\d public.banner` or `select column_name, data_type from information_schema.columns where table_name = 'banner';`.
Then uncomment the template lines for any column the target is missing.

`020_constraints.sql` aborts the run with a list of duplicate rows if the
data violates a new unique key. Clean those rows, then run again.

## Run order

| File | Content |
|---|---|
| `000_schema_drift.sql` | Columns the admin portal needs (`banner.is_active`; template for the others) |
| `010_audit_log.sql` | `audit_log` (+ `operation_details`), listing indexes, no anon/authenticated access |
| `020_constraints.sql` | Unique keys behind the 409 `duplicate_*` codes, each preceded by a duplicate report |
| `030_promotion_rule_id_text.sql` | `promotion_rule.id_text` generated column (guarded) |
| `040_triggers.sql` | `validate_bundle_codes`, `update_promotion_rule_name` triggers |
| `050_admin_audit_helpers.sql` | `admin_write_audit`, `admin_rotate_cache_key`, `admin_insert_audited`, `admin_update_audited`, `admin_delete_audited` |
| `060_admin_search_bundles.sql`, `061_bundles_admin.sql` | Bundles search, single and bulk status, tag set (MS-5083) |
| `070_groups.sql` | Group insert, edit, delete, tag reorder (MS-5085) |
| `080_users.sql` | `admin_search_users`, `admin_wallet_transactions` (MS-5079) |
| `090_wallet_referrals.sql` | `admin_referral_transactions` (MS-5056) |
| `100_promotions.sql` | `admin_search_promotions`, `admin_promotion_usage` and indexes (MS-5050, MS-5054) |
| `110_audit_search.sql`, `120_settings.sql` | Audit log search, settings save (MS-5048, MS-5091) |
| `130_storage_cleanup_queue.sql` | Queue of storage objects that could not be deleted |
| `900_seed_super_admin.sql.example` | Manual: give a user the `super_admin` / `admin` role |

## Rules for every file

- **Idempotent.** Running the full set twice must succeed and change
  nothing the second time. Use `CREATE ... IF NOT EXISTS`,
  `ADD COLUMN IF NOT EXISTS`, `CREATE OR REPLACE FUNCTION`,
  `DROP TRIGGER IF EXISTS` before `CREATE TRIGGER`, and `DO` blocks that
  check `pg_constraint` / `pg_index` / `pg_attribute` before adding
  anything.
- **Runnable when concatenated** in a single transaction: no
  `CREATE INDEX CONCURRENTLY`, no `VACUUM`, no `COMMIT`.
- **New functions are `admin_`-prefixed** and run `SECURITY DEFINER` with
  `SET search_path = public, pg_temp`. Each one has
  `REVOKE ... FROM PUBLIC, anon, authenticated` and
  `GRANT EXECUTE ... TO service_role`.
- **Dynamic SQL** goes through `format()` with `%I` for identifiers and bind
  parameters (`USING`) or `%L` for values. Never concatenate strings.
- **Errors** use SQLSTATE `AD404` (not found), `AD409` (conflict, the message
  is the machine code, e.g. `no_change`, `guard_failed`, `linked_bundles`) or
  `AD422` (invalid input). `DETAIL` holds a JSON object with `code`.

## Seed a super admin

```sh
cp migrations/admin/900_seed_super_admin.sql.example /tmp/seed_admin.sql
# edit the e-mail address, then:
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f /tmp/seed_admin.sql
```

The role lives in `auth.users.raw_app_meta_data.role` (`super_admin` or
`admin`). The user signs in again to get a token that carries it. The file
also shows how to grant `admin` and how to remove a role.
