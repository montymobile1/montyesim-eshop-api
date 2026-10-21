# Wave 2: shared changes made outside the stories

The five wave-2 tracks were developed in parallel, each in its own files. This record lists the shared files prepared before the tracks started and the changes made during the review of the wave. No story record covers them in full.

Date: 2026-10-21. Nothing is committed and no SQL was applied.

## No current flow changed

Every file below is admin code, an admin test or an admin document. No existing route, service, repository, schema or `main.py` was touched. The non-admin test suite gives the same failures as the pre-admin commit `997356d`, except the two guards that fail on any uncommitted file (`test_only_expected_files_were_modified`, `test_no_migration_directory_or_sql_script_was_added`). They pass once the work is committed.

## Prepared before the stories

| File | Change | Why |
|---|---|---|
| `app/api/v1/admin/promotions.py`, `app/api/v1/admin/referrals.py` | Created as empty routers (`router = admin_router()`), each naming the story that owns it | So the Promotions and Referrals tracks fill their own files |
| `app/api/v1/admin/__init__.py` | Mounts them at `/promotions` and `/referrals` | Same reason |

SQL ownership was split before the start: `080_users.sql` holds `admin_search_users` and `admin_wallet_transactions` (Users track); `090_wallet_referrals.sql` holds `admin_referral_transactions` only (Referrals track). The plan's migrations table put both wallet and referral functions in `090`.

## Changed during the review

| File | Change | Why |
|---|---|---|
| `app/api/v1/admin/bundles.py`, `tests/admin/api/test_bundles_api.py` | `PATCH /bundles/{id}/status` requires `admin.bundles.bulk` (super admin) instead of `admin.bundles.write`; a test checks that `admin` gets 403 | The portal shows the row status toggle to super admins only today. Same behaviour kept |
| Portal `src/pages/bundles/BundlesList.jsx`, `src/tests/Bundles.test.jsx` | The row toggle is gated by `PERMISSIONS.BUNDLES_BULK`; a test checks it is hidden from `admin` | Same reason |
| `app/services/admin/group_service.py`, `tests/admin/services/test_group_service.py` | Every group write rotates `APP_CACHE_KEY`, including delete and the status switch | User decision on 2026-10-21. The portal rotated it on add, edit and reorder only |
| `adminDocs/MS-5083-bundles.md`, portal `docs/adminDocs/MS-5084-bundles.md`, `adminDocs/MS-5085-groups.md` | Updated for the two decisions above | |
| Portal `src/pages/users/UserDetail.jsx`, `tabs/ReferralTransactionsTab.jsx` (deleted), `src/core/apis/usersAPI.jsx`, `src/tests/UserDetail.test.jsx`, `src/tests/UsersAPI.test.jsx`, `docs/adminDocs/MS-5080-users.md` | The Referral Transactions tab and `getUserReferrals` are removed | User decision on 2026-10-21, as MS-5080 asks |
| Portal `src/core/routes/allRoutes.jsx`, new `src/tests/Routes.test.jsx` | The Add Promotion route needs `PERMISSIONS.PROMOTIONS_WRITE` instead of read | MS-5053 test case 16: without write permission the add page shows the permission page. No current role is affected: both roles hold `admin.promotions.write` |
| `adminDocs/README.md`, `migrations/admin/README.md`, portal `docs/adminDocs/README.md` | Index rows for the wave-2 records, SQL files `060` to `100`, waves table | |

## Open points for the team leader

- `100_promotions.sql` was not syntax-checked (no PostgreSQL parser offline). Review it before applying.
- Promotions: unused promotions can now change code, rule, amount and start date, and an expired promotion counts only under the Expired tab (both accepted by the user). The end-of-day end date is waiting for the user's decision.
- Referrals: the "To User" filter keeps today's meaning (the buyer). Export is synchronous (v1).
- Leftover for wave 3: portal `src/pages/users/helpers/exportExcel.js` imports a removed function and has no importer; `tagsAPI.jsx` has no importer; the shims `getAllUsersDropdown` and `getAllReferralTransactions` in `usersAPI.jsx`.

## Tests

| Suite | Result |
|---|---|
| Backend `pytest tests/admin` | 901 passed |
| Portal `npx vitest run` | 216 passed, 28 files |
| Backend non-admin suite vs `997356d` | Same failures, plus the two uncommitted-file guards |
