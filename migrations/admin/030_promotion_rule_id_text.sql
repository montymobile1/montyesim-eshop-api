/*
  030_promotion_rule_id_text.sql  (MS-5075)

  promotion_rule.id_text: text copy of the uuid so the rules list can be
  searched with ilike (GET /promotion-rules?search=). Moved from admin repo
  procedures/alter_promotion_rule.sql and guarded so it can run twice
  (the column already exists in supabase_ddl.sql).

  Idempotent.
*/

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_attribute
         WHERE attrelid = 'public.promotion_rule'::regclass
           AND attname = 'id_text'
           AND attnum > 0
           AND NOT attisdropped
    ) THEN
        ALTER TABLE public.promotion_rule
            ADD COLUMN id_text text GENERATED ALWAYS AS (id::text) STORED;
    END IF;
END
$$;
