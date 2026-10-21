# Lead review: items left to the team leader

The admin API work never changes the current flow (mobile app, website, existing endpoints, existing database functions). When a story would need such a change, or leaves a follow-up for the team, it is listed here instead, for the team leader to review and decide. Nothing in this file has been applied.

| # | Object | Current behaviour | Suggested change | Why | Raised by |
|---|---|---|---|---|---|
| 1 | Database function `public.get_banners(p_platform, p_locale)`, used by `GET /app/banners` | Returns every banner of the platform; `banner.is_active` is ignored | Add `AND COALESCE(b.is_active, true)` to its `WHERE` clause | Without it an admin cannot hide a banner from the apps without deleting it. The admin portal has no active/inactive switch for banners until this is decided (user decision, 2026-10-21) | MS-5058 / MS-5059 |
| 2 | Table `storage_cleanup_queue` (admin migration `130`) | Image deletions that fail (banners, group icons) are written to this queue; nothing retries them | Add a scheduled job, or a periodic manual check, that retries the queued deletions and removes the rows | Without it, failed deletions leave unused files in the `media` bucket. Dropped from MS-5058 (case 23) by user decision on 2026-10-21; nothing is lost, every failure stays recorded | MS-5058 |

Wave 3 (MS-5093) will add here the legacy database functions the admin portal no longer calls, with who used them, what replaces them and why removing them would be safe. The developer drops nothing; the lead decides.
