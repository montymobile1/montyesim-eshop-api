/*
  000_schema_drift.sql  (MS-5075, admin API foundation)

  Columns the admin portal reads/writes that must exist before the admin API
  and the later migrations can rely on them. REVIEW AGAINST THE DEPLOYED DB
  FIRST: supabase_ddl.sql (qa) is known to drift from production.

  Plan column                     | in supabase_ddl.sql? | action here
  --------------------------------+----------------------+-------------------------------
  bundle.bundle_name text         | yes                  | none (template below)
  tag.sorting_number int          | yes (default 10000)  | none (template below)
  voucher.expired_at timestamp    | yes                  | none (template below)
  promotion_rule.name text        | yes (default '')     | none (template below)
  promotion_usage.order_id uuid   | yes                  | none (template below)
  promotion_usage.referred_to     | yes (varchar)        | none (template below)
  promotion_usage.device_id       | yes (varchar(250))   | none (template below)
  banner table                    | yes                  | none
  banner.is_active boolean        | NO                   | ADDED below
  tag_group.group_category        | yes (varchar)        | none (template below)

  Why each column matters:
  - bundle.bundle_name: PATCH /bundles/{id} (bundlesAPI.jsx updateBundleTitle).
  - tag.sorting_number: group detail ordering + PUT /groups/{id}/tags/order
    (groupsAPI.jsx orders the tag embed by it).
  - voucher.expired_at: POST /vouchers (vouchersAPI.jsx addVoucher).
  - promotion_rule.name: lookup label, filled by trigger
    trg_update_promotion_rule_name (040).
  - promotion_usage.order_id / referred_to / device_id: referral listing and
    export joins (user_order.id = promotion_usage.order_id, referred_to email).
  - banner.is_active: banner status toggle + status facets (contract section 4).
    Default true so existing banners keep being served by GET /app/banners.
  - tag_group.group_category: group create/edit payload and lookups.

  If the deployed DB lacks one of the "template" columns, uncomment its line in
  the template block at the bottom (types match what the portal sends).

  Idempotent: ADD COLUMN IF NOT EXISTS only.
*/

ALTER TABLE public.banner
    ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true;

/*
  Template - present in supabase_ddl.sql, therefore NOT executed.
  Uncomment only the lines the deployed DB is missing.

ALTER TABLE public.bundle          ADD COLUMN IF NOT EXISTS bundle_name    text;
ALTER TABLE public.tag             ADD COLUMN IF NOT EXISTS sorting_number integer DEFAULT 10000;
ALTER TABLE public.voucher         ADD COLUMN IF NOT EXISTS expired_at     timestamp;
ALTER TABLE public.promotion_rule  ADD COLUMN IF NOT EXISTS name           text DEFAULT ''::text;
ALTER TABLE public.promotion_usage ADD COLUMN IF NOT EXISTS order_id       uuid;
ALTER TABLE public.promotion_usage ADD COLUMN IF NOT EXISTS referred_to    varchar;
ALTER TABLE public.promotion_usage ADD COLUMN IF NOT EXISTS device_id      varchar(250);
ALTER TABLE public.tag_group       ADD COLUMN IF NOT EXISTS group_category varchar;
*/
