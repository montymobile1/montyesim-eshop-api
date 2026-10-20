# MS-5081: orders, devices and contact us

Story: MS-5081, admin API for the Orders, Devices and Contact us pages. Portal counterpart: MS-5082 (`docs/adminDocs/MS-5082-orders-devices-contact-us.md` in the admin portal repo). Depends on MS-5075 (foundation). Branch: `TEOS-139-be-planning-for-changes`. Date: 2026-10-20.

Status: done and tested. Nothing is committed and no migration has been applied.

## No current flow changed

No existing flow or behavior of the backend changed.

- All code is in new files, or in the three router files that MS-5075 created empty for this story (`orders.py`, `devices.py`, `contact_us.py`).
- No existing service, router, repository, schema, model, `main.py` or shared admin file was touched. No `ErrorMessages`, `DatabaseTables` or locale entry was added.
- No SQL has been run against any database.

## What the story adds

### Endpoints

All under `/api/v1/admin`, read-only, in the standard envelope. Lists take `page` (1-based, default 1) and `page_size` (default 20, max 100) and return `data = {data:[...], total_count}` with top-level `totalCount`.

| Method and path | Permission | Query | Rows | Errors |
|---|---|---|---|---|
| GET `/orders` | `admin.orders.read` | `user_id` (UUID), `status=success\|failure\|pending\|canceled`, `sort=created_at\|amount` (default `created_at`), `direction=asc\|desc` (default `desc`) | every `user_order` column except `otp`, plus `user: {id, email, msisdn}` or `null` when the user is not in `users_copy` | 401, 403, 422 (bad enum, bad UUID, unknown parameter, `page_size=101`), 502 `DATABASE_ERROR` |
| GET `/devices` | `admin.devices.read` | `user_id` (UUID) | every `device` column except `fcm_token` | 401, 403, 422, 502 |
| GET `/contact-us` | `admin.contact_us.read` | `search` (email contains, literal, max 200 characters) | `{id, email, content, created_at}` | 401, 403, 422, 502 |

Behavior kept from the portal:

- Orders exclude `order_type = Wallet_Top_Up`, as the page does today. Filters are `user_id` and `order_status`. Order: `created_at desc, id desc` by default; with `sort=amount` the order is `amount <direction>, created_at desc, id desc`, so pages never repeat or skip rows.
- The order's user is joined server-side with `UsersCopyAdminRepo.by_ids` (one query for the page). The portal did a second query from the browser.
- Devices: optional `user_id` filter, newest first. Contact us: optional email search, newest first.

### Not sent any more

- `user_order.otp` (one-time payment code) and `device.fcm_token` (push token) are removed from the rows. The portal never displayed them. All other columns are sent as stored (`select *`), so a column the deployed database has and the DDL lacks still reaches the page.

## Files

### New files

| File | Purpose |
|---|---|
| `app/schemas/admin/orders.py` | `AdminOrderStatus`, `AdminOrderSort`, `AdminSortDirection` enums |
| `app/schemas/admin/devices.py`, `app/schemas/admin/contact_us.py` | Contract notes only (no request body) |
| `app/repo/admin/user_order_repo.py` | `UserOrderAdminRepo.search` |
| `app/repo/admin/device_repo.py` | `DeviceAdminRepo.search` |
| `app/repo/admin/contact_us_repo.py` | `ContactUsAdminRepo.search` |
| `app/services/admin/order_service.py` | Orders page + user join |
| `app/services/admin/device_service.py`, `app/services/admin/contact_us_service.py` | Devices and contact-us pages |
| `migrations/admin/140_orders_devices_indexes.sql` | Indexes below |
| `tests/admin/api/test_orders_api.py`, `test_devices_api.py`, `test_contact_us_api.py` | HTTP contract |
| `tests/admin/services/test_orders_devices_contact_us_service.py` | Services |
| `tests/admin/repo/test_orders_devices_contact_us_repos.py` | Query building, order, hidden columns |
| `adminDocs/MS-5081-orders-devices-contact-us.md` | This record |

### Existing files changed

| File | Change |
|---|---|
| `app/api/v1/admin/orders.py`, `devices.py`, `contact_us.py` | Filled (they were empty routers created by MS-5075 for this story) |

## SQL written, not applied

`migrations/admin/140_orders_devices_indexes.sql`, idempotent (`CREATE INDEX IF NOT EXISTS`), no function, no data change:

- `user_order (created_at desc, id desc)`: the default orders page (asked by the story).
- `user_order (user_id, created_at desc)`: the user filter.
- `device (user_id, created_at desc)`: the devices user filter.

Review notes:

- The number 140 is not in the plan's migrations table; it is the next free number after 130.
- The files are applied with `--single-transaction`, so the indexes are created without `CONCURRENTLY` and lock writes on `user_order` while they build. On a large production table, create them `CONCURRENTLY` by hand first; the file then does nothing.

## Tests

```powershell
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m pytest tests/admin -q -p no:cacheprovider
```

The 44 tests of this story pass. They cover the default call, filters, sort and paging being passed through, the super admin, 422 for `sort=created_at;drop`, `sort=bundle_id`, `direction=sideways`, `status=foo`, a bad `user_id`, `page_size=101`, `page=0` and the old `sortBy` parameter, 401 without a session, 403 without the read permission, 502 on a database error, the empty list, the wallet top-up exclusion, the stable order, the literal email search, the user join with `null` for a deleted user, and the removed `otp` / `fcm_token`.

Full admin run at the time of writing: 219 passed, 1 failed. The failure is `tests/admin/services/test_lookup_service.py`, a file of another story being written at the same time.

## Bugs fixed

- MS-5068 (orders half): the sort column came unchecked from the browser (`.order(sortBy)`). `sort` and `direction` are now closed lists; anything else is 422.
- The orders sort direction could never be ascending (portal side, MS-5082); the API now honors `direction`.
- Orders and devices pages could repeat or skip rows between pages: the order now always ends with `id desc`.
