/*
  040_triggers.sql  (MS-5075)

  Moved unchanged from the admin repo (src/core/procedures/):
  - validate_bundle_codes.sql      -> promotion.bundle_code must list existing bundle codes
  - update_promotion_rule_name.sql -> promotion_rule.name = action-event-max_usage

  Only change: CREATE OR REPLACE + DROP TRIGGER IF EXISTS so the file can run
  twice (the original validate_bundle_codes.sql had a bare CREATE TRIGGER).

  The API relies on these: POST/PUT /promotions with an unknown bundle code
  raises P0001 'Invalid bundle code: <code>' (mapped to 422), and the rules
  lookup label is promotion_rule.name.

  Idempotent.
*/

------------------------------------------------------------------------------
-- validate_bundle_codes (promotion)
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION validate_bundle_codes()
RETURNS TRIGGER AS $$
DECLARE
    code TEXT;
    exists_count INT;
BEGIN
    -- Only validate if bundle_code is NOT NULL and NOT empty
    IF NEW.bundle_code IS NULL OR trim(NEW.bundle_code) = '' THEN
        RETURN NEW;
    END IF;

    -- Split bundle_code string into array
    FOREACH code IN ARRAY string_to_array(NEW.bundle_code, ',')
    LOOP
        SELECT COUNT(*) INTO exists_count
        FROM bundle
        WHERE data->>'bundle_code' = trim(code);

        IF exists_count = 0 THEN
            RAISE EXCEPTION 'Invalid bundle code: %', code;
        END IF;
    END LOOP;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS validate_bundle_codes_promo_trigger ON promotion;

CREATE TRIGGER validate_bundle_codes_promo_trigger
BEFORE INSERT OR UPDATE ON promotion
FOR EACH ROW
EXECUTE FUNCTION validate_bundle_codes();

------------------------------------------------------------------------------
-- update_promotion_rule_name (promotion_rule)
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION update_promotion_rule_name()
RETURNS TRIGGER AS $$
DECLARE
  action_name TEXT;
  event_name TEXT;
BEGIN
  SELECT name INTO action_name FROM promotion_rule_action WHERE id = NEW.promotion_rule_action_id;
  SELECT name INTO event_name FROM promotion_rule_event WHERE id = NEW.promotion_rule_event_id;

  NEW.name := action_name || '-' || event_name || '-' || NEW.max_usage::text;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_update_promotion_rule_name ON promotion_rule;

CREATE TRIGGER trg_update_promotion_rule_name
BEFORE INSERT OR UPDATE ON promotion_rule
FOR EACH ROW
EXECUTE FUNCTION update_promotion_rule_name();
