# Row level security on the per-user tables (TEOS-268)

The API connects with the service key (`SUPABASE_KEY`, role `service_role`, which bypasses RLS) and
scopes every query in code (`where={"user_id": user.id}`). Clients also get the anon key
(`SUPABASE_BASE_ANON_KEY` in `/configurations`) and sign in through Supabase Auth, so a signed-in
client can query PostgREST directly with its own JWT. Before this change, `select * from user_order`
with any user's JWT returned every user's orders.

Migration: `db/rls_user_tables/001_rls_user_tables.up.sql` (down: `.down.sql`). The same block is at
the end of `supabase_ddl.sql`, so a fresh environment comes up locked down.

## What a request sees

ROW LEVEL SECURITY is enabled and forced on all of these tables. All policies are `FOR SELECT TO authenticated`;
there is no INSERT, UPDATE or DELETE policy, so client writes are refused (writes stay API-only).

| Table | Signed-in user sees | Portal admin also sees |
|---|---|---|
| user_profile | `user_id = auth.uid()` | – |
| user_profile_bundle | `user_id = auth.uid()` | – |
| user_order | `user_id = auth.uid() OR anonymous_user_id = auth.uid()` | every row |
| user_wallet | `user_id = auth.uid()` | every row |
| user_wallet_transaction | `wallet_id` of a wallet of `auth.uid()` | every row |
| promotion_usage | `user_id = auth.uid()` | every row |
| device | `user_id = auth.uid()` | every row |
| notification | `user_id = auth.uid()` | – |
| user_otp | nothing (no owner column) | – |
| users_copy | nothing | every row |
| contact_us | nothing | every row |

Anon (no JWT) sees nothing. Another user's row selected by primary key is an empty result, not 403.

**Portal admin** = a JWT whose `app_metadata.role` is `admin` or `super_admin` (the role the admin
API checks). `app_metadata` can only be written with the service key; `user_metadata` does not count.
This policy exists because the old admin portal (`montyesim-eshop-admin` main) reads those tables
(directly and through `get_wallet_transactions_with_count`, `get_referral_transactions_with_count`,
`export_referrals_data`) with the signed-in admin's JWT. It can go once every environment uses the
admin API.

## Safety checks in the migration

- The owner of every table above and of every SECURITY DEFINER function in public must be a superuser
  or have BYPASSRLS. With FORCE, an owner subject to RLS would see no rows: the admin_* functions would
  return empty lists and the `users_copy` trigger on `auth.users` would make every sign-up fail.
- Any other policy on these tables (for example the admin portal's `rls.sql`: `"select policy"
  USING (true)`, `"insert policy" WITH CHECK (true)`) makes the migration fail and lists them.
  Permissive policies are OR-ed, so one `USING (true)` would undo the lock-down. Drop them first.

## Deploy checklist (per environment)

1. `python scripts/check_admin_roles.py` (SUPABASE_URL + service key). Every real portal admin must
   show `admin` or `super_admin`. For a flagged one:
   `python scripts/check_admin_roles.py --set-role admin --email <email>` (dry run), then `--apply`.
   The role is in their JWT from the next sign-in or token refresh.
2. In the SQL editor (as `postgres`):
   ```sql
   select rolname, rolsuper, rolbypassrls from pg_roles where rolname = 'postgres';
   select tablename, policyname, cmd, roles, qual, with_check from pg_policies
    where schemaname = 'public' and tablename in ('user_profile','user_profile_bundle','user_order',
      'user_wallet','user_wallet_transaction','promotion_usage','device','notification','user_otp',
      'users_copy','contact_us');
   select viewname from pg_views where schemaname = 'public';
   ```
   `postgres` must have `rolsuper` or `rolbypassrls`. Policies other than the `*_select_own` /
   `*_select_admin` ones must be reviewed and dropped. A public view would bypass RLS (none in the DDL).
3. Apply `db/rls_user_tables/001_rls_user_tables.up.sql`.
4. Run the live isolation tests against that environment (below), or the retest steps of the PR.

Rollback: `db/rls_user_tables/001_rls_user_tables.down.sql`.

## Tests

- `tests/rls/test_rls_ddl_assertion.py` reads `supabase_ddl.sql` and the migration: RLS enabled and
  forced, exactly the policies above, nothing disabled later, and no unterminated block comment
  (PostgreSQL nests `/* */`; one such comment used to hide the end of `supabase_ddl.sql`).
- `tests/rls/test_rls_isolation_live.py` signs in two users, an empty user, an anonymous user, a
  portal admin and a user with a self-made `user_metadata.role`, seeds a row of every table for the two
  users and reads every table through PostgREST with each JWT and with the anon key. It creates and
  deletes users and rows: run it against a disposable stack or QA, never production.

  ```bash
  export RLS_TEST_SUPABASE_URL=https://<project>.supabase.co
  export RLS_TEST_ANON_KEY=... RLS_TEST_SERVICE_KEY=...
  export RLS_TEST_DATABASE_URL=postgresql://...   # optional: pg_tables / pg_policies check
  python -m pytest tests/rls -q -p no:cacheprovider
  ```
  Without these variables the live tests are skipped.

## Functions reviewed (TEOS-269)

No function in the DDL filters on `auth.uid()`. The SECURITY DEFINER ones that read these tables are the
admin_* functions (EXECUTE: service_role only) and the `sync_users_copy` trigger function. The legacy
SECURITY INVOKER functions that take a user id (`get_referral_transactions*`, `get_wallet_transactions_with_count`,
`export_referrals_data`, `export_user_data`, `get_latest_*promotion_usage_per_user`) run with the caller's
rights, so RLS now limits them to the caller's rows (portal admins: all rows). Revoking them from anon /
authenticated is the separate anon lock-down story.
