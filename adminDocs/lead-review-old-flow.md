# Lead review: items left to the team leader

The admin API work never changes the current flow (mobile app, website, existing endpoints, existing database functions). When a story would need such a change, or leaves a follow-up for the team, it is listed here instead, for the team leader to review and decide. Nothing in this file has been applied.

| # | Object | Current behaviour | Suggested change | Why | Raised by |
|---|---|---|---|---|---|
| 1 | Database function `public.get_banners(p_platform, p_locale)`, used by `GET /app/banners` | Returns every banner of the platform; `banner.is_active` is ignored | Add `AND COALESCE(b.is_active, true)` to its `WHERE` clause | Without it an admin cannot hide a banner from the apps without deleting it. The admin portal has no active/inactive switch for banners until this is decided (user decision, 2026-10-21) | MS-5058 / MS-5059 |
| 2 | Table `storage_cleanup_queue` (admin migration `130`) | Image deletions that fail (banners, group icons) are written to this queue; nothing retries them | Add a scheduled job, or a periodic manual check, that retries the queued deletions and removes the rows | Without it, failed deletions leave unused files in the `media` bucket. Dropped from MS-5058 (case 23) by user decision on 2026-10-21; nothing is lost, every failure stays recorded | MS-5058 |

## Legacy database functions the admin portal no longer calls (MS-5093)

MS-5093 asks for `migrations/admin/990_drop_legacy.sql`. By user decision (2026-10-21) the developer does not write a drop migration and drops nothing. The functions are listed here; the team leader decides whether and when to drop them.

Checked on 2026-10-21:
- the admin portal calls none of them since the wave-2 and wave-3 commits (its copies in `src/core/procedures/` are deleted, except `rls-db-format.sql`);
- no code in this backend calls them (`app/` only has the new `admin_*` functions; Python methods named `insert_group_with_tags` / `edit_tag_group` in `app/repo/admin/tag_group_repo.py` call the `admin_` versions);
- the website (subscriber-open-source-3) does not call them.
Not checked: the mobile app and any other client or job outside these three repos, and the deployed database itself.

| # | Function (signature as committed in the portal) | Used by (before) | Replaced by |
|---|---|---|---|
| 3 | `search_bundles(text, integer, integer, uuid[], boolean)` | Bundles page search and tag filter | `admin_search_bundles` (`060`), `GET /bundles` |
| 4 | `activate_all_bundles()` | Bundles "Activate all" | `admin_set_all_bundles_status` (`061`), `POST /bundles/status/bulk` |
| 5 | `deactivate_all_bundles(text)` | Bundles "Deactivate all" | same as 4 |
| 6 | `insert_group_with_tags(text, text, integer, jsonb)` | Group create | `admin_insert_group_with_tags` (`070`), `POST /groups` |
| 7 | `edit_tag_group(integer, text, integer, text, jsonb, jsonb, uuid[])` | Group edit | `admin_edit_tag_group` (`070`), `PUT /groups/{id}` |
| 8 | `delete_group_if_no_bundle(integer)` | Group delete | `admin_delete_group` (`070`), `DELETE /groups/{id}` |
| 9 | `get_tags_with_group_all(text, integer, integer)` | Tag dropdown on the bundles page | `GET /tags/lookup` (no function needed) |
| 10 | `get_wallet_transactions_with_count(uuid, integer, integer)` | User detail, wallet tab | `admin_wallet_transactions` (`080`) |
| 11 | `get_referral_transactions_with_count(uuid, integer, integer, text, text, date)` | Referrals page | `admin_referral_transactions` (`090`), `GET /referrals` |
| 12 | `export_referrals_data(uuid, text)` | Referrals export (browser-built Excel) | `POST /referrals/export` (server-built xlsx) |
| 13 | `export_user_data(uuid)` | Users export helper (no page called it at the end of wave 2) | none needed |
| 14 | `get_client_ip_address()` | Browser-written audit rows | the API records the caller IP itself |

Also for review: the portal's old `src/core/procedures/all procedures.sql` (deleted in MS-5094) contained fully permissive storage policies. If those policies were ever applied to a database, MS-5011 (RLS) should have replaced them; worth confirming on the deployed database.

### Proposed SQL (not a migration, not applied)

Run the check first. It needs `track_functions = 'all'` (or `'pl'`) to have been on for long enough to be meaningful; otherwise `calls` stays empty and proves nothing.

```sql
-- 1. Who still calls them? Expect no rows, or calls that stopped before the new portal was deployed.
select schemaname, funcname, calls, total_time
from pg_stat_user_functions
where schemaname = 'public'
  and funcname in ('search_bundles','activate_all_bundles','deactivate_all_bundles','insert_group_with_tags',
                   'edit_tag_group','delete_group_if_no_bundle','get_tags_with_group_all',
                   'get_wallet_transactions_with_count','get_referral_transactions_with_count',
                   'export_referrals_data','export_user_data','get_client_ip_address');

-- 2. Every signature present (overloads included), to build the exact DROP list.
select p.oid::regprocedure
from pg_proc p join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public'
  and p.proname in ('search_bundles','activate_all_bundles','deactivate_all_bundles','insert_group_with_tags',
                    'edit_tag_group','delete_group_if_no_bundle','get_tags_with_group_all',
                    'get_wallet_transactions_with_count','get_referral_transactions_with_count',
                    'export_referrals_data','export_user_data','get_client_ip_address');

-- 3. Only if the lead decides to drop, and only after the new portal is deployed to that environment:
-- drop function if exists public.search_bundles(text, integer, integer, uuid[], boolean);
-- drop function if exists public.activate_all_bundles();
-- drop function if exists public.deactivate_all_bundles(text);
-- drop function if exists public.insert_group_with_tags(text, text, integer, jsonb);
-- drop function if exists public.edit_tag_group(integer, text, integer, text, jsonb, jsonb, uuid[]);
-- drop function if exists public.delete_group_if_no_bundle(integer);
-- drop function if exists public.get_tags_with_group_all(text, integer, integer);
-- drop function if exists public.get_wallet_transactions_with_count(uuid, integer, integer);
-- drop function if exists public.get_referral_transactions_with_count(uuid, integer, integer, text, text, date);
-- drop function if exists public.export_referrals_data(uuid, text);
-- drop function if exists public.export_user_data(uuid);
-- drop function if exists public.get_client_ip_address();
```

Use the signatures returned by query 2 if they differ from the ones above (the deployed database may have drifted from the portal copies).
