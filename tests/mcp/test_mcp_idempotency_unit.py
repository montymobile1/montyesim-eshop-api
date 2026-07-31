"""Unit tests for canonicalization, hashing and Idempotency-Key validation."""

import pytest

from app.config.feature_flags import get_bool_env, is_mcp_purchase_enabled, parse_bool
from app.config.mcp_constants import MCP_WALLET_BUNDLE_ASSIGN
from app.exceptions import CustomException
from app.schemas.mcp import McpAssignRequest
from app.services.mcp_idempotency import (
    build_canonical_request,
    build_request_hash,
    fingerprint,
    hash_idempotency_key,
    normalize_related_search,
    user_fingerprint,
    validate_idempotency_key,
)

USER = "user-1"
OTHER_USER = "user-2"
OPERATION = MCP_WALLET_BUNDLE_ASSIGN
VALID_KEY = "K" * 32


def request_hash(bundle_code="bundle-1", payment_type="Wallet", related_search=None, user_id=USER):
    return build_request_hash(user_id=user_id, operation=OPERATION, bundle_code=bundle_code,
                              payment_type=payment_type, related_search=related_search)


# --------------------------------------------------------------- key validation

@pytest.mark.parametrize("raw_key", [None, "", "   ", "\t"])
def test_missing_or_blank_key_is_rejected(raw_key):
    with pytest.raises(CustomException) as exc:
        validate_idempotency_key(raw_key)
    assert exc.value.code == 400
    assert str(exc.value.name) == "IDEMPOTENCY_KEY_REQUIRED"


@pytest.mark.parametrize("raw_key", ["a" * 31, "a" * 129])
def test_key_length_range_is_enforced(raw_key):
    with pytest.raises(CustomException) as exc:
        validate_idempotency_key(raw_key)
    assert str(exc.value.name) == "IDEMPOTENCY_KEY_INVALID"


@pytest.mark.parametrize("raw_key", ["a" * 32, "b" * 128, "A1-_.~" + "x" * 30])
def test_valid_keys_are_accepted(raw_key):
    assert validate_idempotency_key(raw_key) == raw_key


@pytest.mark.parametrize("raw_key", ["a" * 31 + " ", "a" * 31 + "/", "a" * 31 + "\n", "a" * 31 + "<"])
def test_unsafe_characters_are_rejected(raw_key):
    with pytest.raises(CustomException):
        validate_idempotency_key(raw_key)


def test_key_is_trimmed_but_not_altered_otherwise():
    assert validate_idempotency_key(f"  {VALID_KEY}  ") == VALID_KEY


# ------------------------------------------------------------------- key hashing

def test_key_digest_is_stable_and_hides_the_raw_key():
    digest = hash_idempotency_key(VALID_KEY, USER, OPERATION)
    assert digest == hash_idempotency_key(VALID_KEY, USER, OPERATION)
    assert len(digest) == 64
    assert VALID_KEY not in digest


def test_same_external_key_is_a_different_identity_per_user():
    assert hash_idempotency_key(VALID_KEY, USER, OPERATION) != hash_idempotency_key(VALID_KEY, OTHER_USER,
                                                                                    OPERATION)


def test_same_external_key_is_a_different_identity_per_operation():
    assert hash_idempotency_key(VALID_KEY, USER, OPERATION) != hash_idempotency_key(VALID_KEY, USER, "OTHER")


def test_hmac_secret_changes_the_digest(monkeypatch):
    plain = hash_idempotency_key(VALID_KEY, USER, OPERATION)
    monkeypatch.setenv("MCP_IDEMPOTENCY_HASH_SECRET", "unit-test-secret")
    keyed = hash_idempotency_key(VALID_KEY, USER, OPERATION)
    assert keyed != plain
    assert keyed == hash_idempotency_key(VALID_KEY, USER, OPERATION)


def test_fingerprints_are_short_and_non_reversible():
    digest = hash_idempotency_key(VALID_KEY, USER, OPERATION)
    assert len(fingerprint(digest)) == 12
    assert fingerprint(digest) in digest
    assert USER not in user_fingerprint(USER)
    assert len(user_fingerprint(USER)) == 12


# ------------------------------------------------------------ canonical requests

def test_logically_identical_requests_hash_the_same():
    first = {"region": None, "countries": [{"iso3_code": "FRA", "country_name": "France"},
                                           {"iso3_code": "ITA", "country_name": "Italy"}]}
    # Different country order, different case and padding: same purchase.
    second = {"countries": [{"iso3_code": " ita ", "country_name": "Italy "},
                            {"iso3_code": "fra", "country_name": " France"}], "region": None}
    assert request_hash(related_search=first) == request_hash(related_search=second)


def test_absent_and_empty_related_search_are_equivalent():
    assert request_hash(related_search=None) == request_hash(related_search={"region": None, "countries": []})
    assert request_hash(related_search=None) == request_hash(
        related_search={"region": None, "countries": None})


def test_duplicate_countries_do_not_change_the_hash():
    once = {"countries": [{"iso3_code": "FRA", "country_name": "France"}]}
    twice = {"countries": [{"iso3_code": "FRA", "country_name": "France"},
                           {"iso3_code": "FRA", "country_name": "France"}]}
    assert request_hash(related_search=once) == request_hash(related_search=twice)


def test_materially_different_purchases_hash_differently():
    base = request_hash()
    assert base != request_hash(bundle_code="bundle-2")
    assert base != request_hash(user_id=OTHER_USER)
    assert base != request_hash(payment_type="Card")
    assert base != request_hash(related_search={"countries": [{"iso3_code": "FRA",
                                                               "country_name": "France"}]})


def test_different_region_changes_the_hash():
    europe = {"region": {"iso_code": "EU", "region_name": "Europe"}, "countries": []}
    asia = {"region": {"iso_code": "AS", "region_name": "Asia"}, "countries": []}
    assert request_hash(related_search=europe) != request_hash(related_search=asia)


def test_canonical_request_never_contains_credentials_or_volatile_data():
    canonical = build_canonical_request(user_id=USER, operation=OPERATION, bundle_code="bundle-1",
                                        payment_type="Wallet",
                                        related_search={"countries": [{"iso3_code": "FRA",
                                                                       "country_name": "France"}]})
    for forbidden in ("token", "Bearer", "quote_reference", "currency", "device"):
        assert forbidden not in canonical
    # Deterministic key order regardless of input order.
    assert canonical.index('"bundle_code"') < canonical.index('"operation"') < canonical.index('"user_id"')


def test_pydantic_request_objects_and_dicts_normalize_identically():
    body = McpAssignRequest(bundle_code="bundle-1", payment_type="Wallet",
                            related_search={"region": None,
                                            "countries": [{"iso3_code": "fra", "country_name": "France"}]})
    from_model = normalize_related_search(body.related_search)
    from_dict = normalize_related_search({"region": None,
                                          "countries": [{"iso3_code": "FRA", "country_name": "France"}]})
    assert from_model == from_dict


# ------------------------------------------------------------------ feature flag

@pytest.mark.parametrize("value,expected", [
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("no", False), ("", False), ("  ", False), (None, False),
    ("maybe", False),
])
def test_parse_bool(value, expected):
    assert parse_bool(value) is expected


def test_flag_defaults_to_false_when_unset(monkeypatch):
    monkeypatch.delenv("MCP_PURCHASE_ENABLED", raising=False)
    assert is_mcp_purchase_enabled() is False
    assert get_bool_env("A_FLAG_THAT_DOES_NOT_EXIST") is False


def test_flag_is_read_on_every_call(monkeypatch):
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", "true")
    assert is_mcp_purchase_enabled() is True
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", "false")
    assert is_mcp_purchase_enabled() is False
