/*
  140_orders_devices_indexes.sql  (MS-5081)

  Indexes for the admin Orders and Devices lists (GET /api/v1/admin/orders, /devices).
  - user_order: default order created_at desc, id desc (story MS-5081: "new index
    user_order(created_at desc, id desc) if absent"), and the per-user filter.
  - device: the per-user filter, newest first.
  No function, no data change. Plain CREATE INDEX (not CONCURRENTLY) because the files are
  applied with --single-transaction; on a large production table the team leader may prefer
  to create these CONCURRENTLY by hand first, which makes this file a no-op.

  Idempotent.
*/

CREATE INDEX IF NOT EXISTS user_order_created_at_id_idx
    ON public.user_order (created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS user_order_user_id_created_at_idx
    ON public.user_order (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS device_user_id_created_at_idx
    ON public.device (user_id, created_at DESC);
