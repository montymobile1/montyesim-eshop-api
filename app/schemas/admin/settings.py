"""Shapes of /api/v1/admin/settings and /settings/history (MS-5091, MS-5046, TEOS-134).

- ``GET /settings`` rows: ``{id, key, value, description, created_at, is_secret, editable, deletable}``.
  Every row is listed with its real value, secret-shaped keys included (user decision 2026-10-21: those rows
  exist only in the qa database and the backend reads secrets from the environment only, get_secret);
  ``is_secret`` is kept for compatibility and is always false (nothing is masked).
- ``PUT /settings`` body: :class:`AdminSettingsUpdate`; response :class:`AdminSettingsUpdateResult`.
- ``GET /settings/history`` rows: :class:`AdminSettingsHistoryRow`.

Key rules (TEOS-134), all checked before anything is written:

- secret-shaped keys (``is_secret_key`` of app/config/helper.py) are treated like any other key (user decision
  2026-10-21, for now);
- ``APP_CACHE_KEY`` cannot be written (422 ``cache_key_forbidden``, the server rotates it);
- :data:`PROTECTED_SETTING_KEYS` cannot be deleted -> 409 ``protected_key`` (the whole batch is refused);
- known keys are validated by type (:data:`SETTING_VALUE_RULES`) -> 422 on the offending key;
- a NEW key must match :data:`NEW_KEY_PATTERN`; at most :data:`MAX_ITEMS` keys per save.
"""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional

from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, ConfigDict, Field, model_validator

CACHE_KEY = "APP_CACHE_KEY"
#: Key format for a NEW setting (TEOS-134). Existing keys (some are lower case, e.g. ``login_type``) can still
#: be edited; the service applies this pattern to keys that do not exist yet.
NEW_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
MAX_VALUE_LENGTH = 10_000
#: Most keys in one save, items and deleted_keys together (TEOS-134).
MAX_ITEMS = 100

#: Keys that can never be deleted through the admin API (409 ``protected_key``): the cache version key and
#: every non-secret app_config key the backend reads at runtime (RESELLER_ID and the other secrets are read
#: from the environment, get_secret, so they are not protected). Found with
#: ``grep -rn "get_setting(\|get_config(\|ConfigKeysEnum\." app`` (non-admin code) plus the keys the admin
#: API itself reads (TOP_COUNTRIES_COUNT: groups reorder page; VOUCHER_MAX_AMOUNT: voucher create).
#: Keep in sync when a consumer starts reading a new key.
PROTECTED_SETTING_KEYS = frozenset({
    # cache version and identity
    "APP_CACHE_KEY", "CACHE_KEY", "CACHE_TIME", "RESELLER_NAME", "ESIM_NAME", "BASE_URL",
    # ConfigKeysEnum (app/config/db.py)
    "DEFAULT_REFERRAL_RULE_ID", "REFERRAL_CODE_AMOUNT", "REFERRAL_CODE_PERCENTAGE", "STRIPE_TAX_CODE",
    "STRIPE_TAX_BEHAVIOR", "OTP_EXPIRATION_TIME", "login_type", "DEFAULT_CURRENCY", "DEFAULT_PAYMENT_METHODS",
    # catalogue, promotions, e-mails, support
    "FORBIDDEN_COUNTRIES", "PROMOTION_LIMIT_ACTIVE", "PROMOTION_LIMIT_WINDOW_SECONDS", "SMTP_SENDER",
    # WHATSAPP_NUMBER is not protected: a client may drop WhatsApp support by deleting the key.
    "SMTP_SENDER_NAME", "SUPPORT_EMAIL", "WALLET_TOP_UP_ALERT_RECIPIENTS",
    # direct carrier billing
    "DEFAULT_DCB_PROVIDER", "DCB_HUB_BUSINESS_TYPE", "DCB_HUB_CHARGE_CODE", "DCB_HUB_CHARGE_URL",
    "DCB_HUB_CONVERSION_RATE", "DCB_HUB_CURRENCY_ID", "DCB_HUB_MSISDN_EXTENSION", "DCB_HUB_SEND_SMS_URL",
    "DCB_HUB_SOURCE_MSISDN", "DCB_HUB_TAX_AMOUNT", "DCB_HUB_TAX_CODE", "DCB_HUB_VERIFY_OTP_URL",
    # read by the admin API
    "TOP_COUNTRIES_COUNT", "VOUCHER_MAX_AMOUNT",
})
#: Keys whose value cannot be changed here (``editable = false`` in GET).
READ_ONLY_SETTING_KEYS = frozenset({CACHE_KEY})

VOUCHER_MAX_AMOUNT_KEY = "VOUCHER_MAX_AMOUNT"

#: ISO 3166-1 alpha-2 country codes (officially assigned), for FORBIDDEN_COUNTRIES.
ISO2_COUNTRY_CODES = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR
GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO
JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR
MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO
RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV
TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split())


def _top_countries_count(value: str) -> Optional[str]:
    text = value.strip()
    if not re.fullmatch(r"\d+", text) or not 1 <= int(text) <= 50:
        return "must be a whole number from 1 to 50"
    return None


def _country_list(value: str) -> Optional[str]:
    if value == "":
        return None  # no forbidden country
    codes = value.split(",")  # the consumers read it with .split(","): no spaces allowed
    bad = [code for code in codes if code not in ISO2_COUNTRY_CODES]
    if bad:
        listed = ", ".join(repr(code) for code in bad[:5])
        return f"must be comma-separated upper-case ISO 3166-1 alpha-2 codes without spaces (invalid: {listed})"
    if len(set(codes)) != len(codes):
        return "lists a country more than once"
    return None


def _email_list(value: str) -> Optional[str]:
    parts = [part.strip() for part in value.split(",")]
    if not value.strip() or any(not part for part in parts):
        return "must be a comma-separated list of e-mail addresses"
    for part in parts:
        try:
            validate_email(part, check_deliverability=False)
        except EmailNotValidError:
            return f"{part!r} is not a valid e-mail address"
    return None


def _positive_decimal(value: str) -> Optional[str]:
    try:
        number = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return "must be a positive number"
    if not number.is_finite() or number <= 0:
        return "must be a positive number"
    return None


#: Typed validation of known keys: ``key -> check(value) -> error text or None``.
SETTING_VALUE_RULES: Dict[str, Callable[[str], Optional[str]]] = {
    "TOP_COUNTRIES_COUNT": _top_countries_count,
    "FORBIDDEN_COUNTRIES": _country_list,
    "WALLET_TOP_UP_ALERT_RECIPIENTS": _email_list,
    VOUCHER_MAX_AMOUNT_KEY: _positive_decimal,
}


def setting_value_error(key: str, value: str) -> Optional[str]:
    """Why ``value`` is not valid for the known setting ``key``; None when valid or not a known key."""
    rule = SETTING_VALUE_RULES.get(key)
    return rule(value) if rule else None


class AdminSettingItem(BaseModel):
    """One key to create or update. ``key``: 1-100 characters, no spaces; ``value``: up to 10 000."""
    key: str = Field(..., min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.\-]+$")
    value: str = Field(..., max_length=MAX_VALUE_LENGTH)

    model_config = ConfigDict(extra="forbid")


class AdminSettingsUpdate(BaseModel):
    """Body of ``PUT /settings``: the keys to create or update and the keys to delete, in one save.

    422 when both lists are empty, when more than 100 keys are sent (both lists together), when a key
    appears twice, when a key is in both lists, when ``APP_CACHE_KEY`` is in ``items`` (the server rotates
    it), or when a known key has a value of the wrong type (``items.<index>.value: invalid_value``). Error texts
    start with ``items.<index>.key`` / ``items.<index>.value`` or ``deleted_keys.<index>`` so the portal can
    mark the row. Deleting a protected key (409 ``protected_key``) is refused by the service.
    """
    items: List[AdminSettingItem] = Field(default_factory=list, max_length=MAX_ITEMS)
    deleted_keys: List[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    reason: Optional[str] = Field(None, min_length=1, max_length=500)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _check(self) -> "AdminSettingsUpdate":
        if not self.items and not self.deleted_keys:
            raise ValueError("items and deleted_keys are both empty: nothing_to_save")
        if len(self.items) + len(self.deleted_keys) > MAX_ITEMS:
            raise ValueError(f"too_many_items: at most {MAX_ITEMS} keys per save (items and deleted_keys together)")
        seen = set()
        for index, item in enumerate(self.items):
            if item.key == CACHE_KEY:
                raise ValueError(f"items.{index}.key: cache_key_forbidden, APP_CACHE_KEY is rotated by the server")
            if item.key in seen:
                raise ValueError(f"items.{index}.key: duplicate_key {item.key}")
            seen.add(item.key)
            error = setting_value_error(item.key, item.value)
            if error:
                raise ValueError(f"items.{index}.value: invalid_value, {item.key} {error}")
        deleted_seen = set()
        for index, key in enumerate(self.deleted_keys):
            if not key or len(key) > 100:
                raise ValueError(f"deleted_keys.{index}: invalid key")
            if key in seen:
                raise ValueError(f"deleted_keys.{index}: {key} is also in items")
            if key in deleted_seen:
                raise ValueError(f"deleted_keys.{index}: duplicate_key {key}")
            deleted_seen.add(key)
        return self


class AdminSettingsUpdateResult(BaseModel):
    updated: List[str]
    deleted: List[str]
    cache_key_rotated: bool


class AdminSettingRow(BaseModel):
    """``editable``: the value can be changed here (false for APP_CACHE_KEY). ``deletable``: the key can be
    deleted (false for :data:`PROTECTED_SETTING_KEYS`). ``description``: app_config.description (TEOS-134)."""
    id: Any
    key: str
    value: Optional[str] = None
    description: Optional[str] = None
    created_at: Optional[datetime] = None
    is_secret: bool = False
    editable: bool = True
    deletable: bool = True


class AdminActor(BaseModel):
    id: str
    email: Optional[str] = None


class AdminSettingChange(BaseModel):
    """One changed key. ``old_value`` null = the key was created; ``new_value`` null = it was deleted.
    Secret-shaped keys have both values replaced by ``"***"`` and ``redacted = true``."""
    key: str
    old_value: Optional[Any] = None
    new_value: Optional[Any] = None
    redacted: bool = False


class AdminSettingsHistoryRow(BaseModel):
    """One settings save. ``changes`` is computed on the server from old_data/new_data and lists only the
    keys whose value differs; values of keys matching SECRET|PASSWORD|TOKEN|PRIVATE|API_KEY (or the
    secret rule of app/config/helper.py, APP_CACHE_KEY excepted) are ``"***"``. ``parse_error`` is true
    when a legacy row's data could not be read (``changes`` is then empty)."""
    id: int
    changed_at: Optional[datetime] = None
    changed_by: Optional[AdminActor] = None
    ip_address: Optional[str] = None
    client_reported: bool = False
    reason: Optional[str] = None
    operation: Optional[str] = None
    changes: List[AdminSettingChange]
    parse_error: bool = False
