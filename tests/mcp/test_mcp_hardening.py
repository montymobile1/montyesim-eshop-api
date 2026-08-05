"""Pre-deployment hardening: mandatory hash secret, and currency in the identity.

Two independent guarantees are covered here.

*Secret* - while ``MCP_PURCHASE_ENABLED`` is true the endpoint must fail closed if
``MCP_IDEMPOTENCY_HASH_SECRET`` is missing, blank, placeholder-like or too short.
Nothing else in the application may be affected by its absence.

*Currency* - the effective currency (after backend defaults) is part of the request
identity, and the endpoint settles in the system currency only.
"""

import json

import pytest

from app.config.mcp_constants import (
    IDEMPOTENCY_HASH_SECRET_ENV,
    IDEMPOTENCY_HASH_SECRET_MIN_LENGTH,
    MCP_WALLET_BUNDLE_ASSIGN,
)
from app.exceptions import CustomException
from app.services.mcp_idempotency import (
    build_canonical_request,
    build_request_hash,
    effective_currency,
    hash_idempotency_key,
    hash_secret_problem,
    is_hash_secret_usable,
    normalize_currency,
    require_hash_secret,
    system_currency,
)
from tests.mcp.conftest import (
    OTHER_VALID_KEY,
    USER_A_ID,
    USER_A_TOKEN,
    VALID_HASH_SECRET,
    VALID_KEY,
    mcp_body,
    mcp_headers,
    post_mcp,
)

MCP_PATH = "/api/v1/mcp/user/bundle/assign"
LEGACY_PATH = "/api/v1/user/bundle/assign"

INVALID_SECRETS = [
    pytest.param("", id="blank"),
    pytest.param("   ", id="whitespace"),
    pytest.param("short", id="too-short"),
    pytest.param("a" * (IDEMPOTENCY_HASH_SECRET_MIN_LENGTH - 1), id="one-char-under-minimum"),
    pytest.param("changeme", id="placeholder-changeme"),
    pytest.param("change-me-before-you-deploy-this-service", id="placeholder-prefix"),
    pytest.param("secret", id="placeholder-secret"),
    pytest.param("replace-with-openssl-rand-hex-32", id="the-env-example-placeholder"),
    pytest.param("test" * 12, id="placeholder-test-repeated"),
    pytest.param("x" * 40, id="single-repeated-character"),
]


# ============================================================== secret validation

@pytest.mark.parametrize("secret", INVALID_SECRETS)
def test_unusable_secrets_are_detected(monkeypatch, secret):
    monkeypatch.setenv(IDEMPOTENCY_HASH_SECRET_ENV, secret)
    assert is_hash_secret_usable() is False
    assert hash_secret_problem() is not None


def test_a_missing_secret_is_detected(monkeypatch):
    monkeypatch.delenv(IDEMPOTENCY_HASH_SECRET_ENV, raising=False)
    assert is_hash_secret_usable() is False
    assert "not set" in hash_secret_problem()


def test_a_real_secret_is_accepted(monkeypatch):
    monkeypatch.setenv(IDEMPOTENCY_HASH_SECRET_ENV, VALID_HASH_SECRET)
    assert is_hash_secret_usable() is True
    assert hash_secret_problem() is None
    assert require_hash_secret() == VALID_HASH_SECRET.encode("utf-8")


@pytest.mark.parametrize("secret", INVALID_SECRETS)
def test_require_hash_secret_fails_closed(monkeypatch, secret):
    monkeypatch.setenv(IDEMPOTENCY_HASH_SECRET_ENV, secret)
    with pytest.raises(CustomException) as exc:
        require_hash_secret()

    assert exc.value.code == 503
    assert str(exc.value.name) == "MCP_IDEMPOTENCY_SECRET_MISCONFIGURED"


@pytest.mark.parametrize("secret", [
    "changeme-Q7Zx4Kp2Vn8Lm3Rt6Yw9Bs1Dg5Hj0Fc",   # placeholder prefix, distinctive tail
    "Q7Zx4Kp2",                                    # too short, distinctive
    "replace-with-openssl-rand-hex-32",            # the .env.example placeholder
])
def test_the_secret_value_never_appears_in_the_error(monkeypatch, secret):
    """Distinctive values only - a secret literally spelled 'secret' would false-positive."""
    monkeypatch.setenv(IDEMPOTENCY_HASH_SECRET_ENV, secret)
    with pytest.raises(CustomException) as exc:
        require_hash_secret()

    rendered = f"{exc.value.details} {exc.value.name} {exc.value.code}"
    assert secret not in rendered
    # Not even a distinctive fragment of it.
    assert secret[:12] not in rendered


def test_an_enabled_feature_never_falls_back_to_an_unkeyed_digest(monkeypatch):
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", "true")
    monkeypatch.delenv(IDEMPOTENCY_HASH_SECRET_ENV, raising=False)

    with pytest.raises(CustomException) as exc:
        hash_idempotency_key(VALID_KEY, USER_A_ID, MCP_WALLET_BUNDLE_ASSIGN)
    assert exc.value.code == 503


def test_the_secret_actually_keys_the_digest(monkeypatch):
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", "true")
    monkeypatch.setenv(IDEMPOTENCY_HASH_SECRET_ENV, VALID_HASH_SECRET)
    keyed = hash_idempotency_key(VALID_KEY, USER_A_ID, MCP_WALLET_BUNDLE_ASSIGN)

    monkeypatch.setenv(IDEMPOTENCY_HASH_SECRET_ENV, VALID_HASH_SECRET[::-1])
    rotated = hash_idempotency_key(VALID_KEY, USER_A_ID, MCP_WALLET_BUNDLE_ASSIGN)

    # This difference is exactly why the secret must stay stable across deployments:
    # a rotated secret makes an already-used key look brand new.
    assert keyed != rotated
    assert len(keyed) == 64


# -------------------------------------------------- secret: endpoint-level effect

def test_enabled_mcp_refuses_to_operate_without_a_secret(db, hub, client, monkeypatch):
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", "true")
    monkeypatch.delenv(IDEMPOTENCY_HASH_SECRET_ENV, raising=False)

    response = post_mcp(client)

    assert response.status_code == 503
    assert response.json()["developerMessage"].startswith("MCP purchase is enabled but its idempotency")
    # Fail closed: nothing was written, nothing was bought, no money moved.
    assert db.tables.get("mcp_purchase_idempotency") is None
    assert db.tables.get("user_order") is None
    assert db.tables.get("user_wallet_transaction") is None
    assert hub.create_order_calls == 0


@pytest.mark.parametrize("secret", INVALID_SECRETS)
def test_enabled_mcp_refuses_every_unusable_secret(db, hub, client, monkeypatch, secret):
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", "true")
    monkeypatch.setenv(IDEMPOTENCY_HASH_SECRET_ENV, secret)

    response = post_mcp(client)

    assert response.status_code == 503
    assert db.tables.get("mcp_purchase_idempotency") is None
    assert db.tables.get("user_order") is None


def test_the_secret_is_never_echoed_in_a_response(db, hub, client, monkeypatch):
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", "true")
    monkeypatch.setenv(IDEMPOTENCY_HASH_SECRET_ENV, "changeme-" + "b" * 40)

    body = json.dumps(post_mcp(client).json())

    assert "changeme-" not in body
    assert "b" * 40 not in body


def test_a_valid_secret_restores_normal_behaviour(db, hub, client, mcp_enabled):
    response = post_mcp(client)

    assert response.status_code == 200
    assert data(response)["status"] == "COMPLETED"
    record = db.tables["mcp_purchase_idempotency"][0]
    assert len(record["idempotency_key_hash"]) == 64
    # The raw secret must not be persisted anywhere.
    assert VALID_HASH_SECRET not in json.dumps(record, default=str)


def test_disabled_mcp_needs_no_secret(db, hub, client, monkeypatch):
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", "false")
    monkeypatch.delenv(IDEMPOTENCY_HASH_SECRET_ENV, raising=False)

    response = post_mcp(client)

    assert response.status_code == 503
    assert response.json()["title"] is not None
    # Disabled for the ordinary reason, not the secret one.
    assert "idempotency hash secret" not in str(response.json()["developerMessage"])
    assert db.tables.get("mcp_purchase_idempotency") is None


@pytest.mark.parametrize("flag", ["false", "true"])
def test_the_legacy_flow_never_needs_the_secret(db, hub, client, monkeypatch, flag):
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", flag)
    monkeypatch.delenv(IDEMPOTENCY_HASH_SECRET_ENV, raising=False)

    response = client.post(LEGACY_PATH, json={
        "bundle_code": mcp_body()["bundle_code"], "payment_type": "Wallet",
        "related_search": {"region": None, "countries": [{"iso3_code": "FRA", "country_name": "France"}]},
        "promo_code": None, "affiliate_code": None},
        headers={"Authorization": f"Bearer {USER_A_TOKEN}", "X-Device-Id": "device-1"})

    assert response.status_code == 200
    assert response.json()["data"]["payment_status"] == "COMPLETED"
    assert len(db.tables["user_order"]) == 1
    assert db.tables.get("mcp_purchase_idempotency") is None


def test_the_application_still_serves_other_routes_without_a_secret(client, monkeypatch):
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", "true")
    monkeypatch.delenv(IDEMPOTENCY_HASH_SECRET_ENV, raising=False)
    assert client.get("/api/v1/health_check").status_code in (200, 404)


# ================================================================ currency identity

def data(response) -> dict:
    return response.json()["data"]


def test_currency_normalization():
    assert normalize_currency("  usd ") == "USD"
    assert normalize_currency("Usd") == "USD"
    assert normalize_currency("") is None
    assert normalize_currency(None) is None


def test_effective_currency_falls_back_to_the_system_currency(monkeypatch):
    monkeypatch.setenv("SYSTEM_CURRENCY", "USD")
    # DEFAULT_CURRENCY is a presentation default (EUR in this deployment) and must
    # never be what an MCP purchase silently settles in.
    monkeypatch.setenv("DEFAULT_CURRENCY", "EUR")
    assert effective_currency(None) == "USD"
    assert effective_currency("  ") == "USD"
    assert effective_currency("usd") == "USD"
    assert effective_currency(" eur ") == "EUR"
    assert system_currency() == "USD"


def test_case_and_padding_do_not_change_the_request_hash():
    def hash_for(currency):
        return build_request_hash(user_id=USER_A_ID, operation=MCP_WALLET_BUNDLE_ASSIGN,
                                  bundle_code="bundle-1", payment_type="Wallet",
                                  related_search=None, currency=currency)

    assert hash_for("USD") == hash_for("usd") == hash_for("  Usd  ")


def test_a_different_currency_changes_the_request_hash():
    def hash_for(currency):
        return build_request_hash(user_id=USER_A_ID, operation=MCP_WALLET_BUNDLE_ASSIGN,
                                  bundle_code="bundle-1", payment_type="Wallet",
                                  related_search=None, currency=currency)

    assert hash_for("USD") != hash_for("EUR")
    assert hash_for("USD") != hash_for("GBP")


def test_currency_is_part_of_the_canonical_request():
    canonical = build_canonical_request(user_id=USER_A_ID, operation=MCP_WALLET_BUNDLE_ASSIGN,
                                        bundle_code="bundle-1", payment_type="Wallet",
                                        related_search=None, currency="usd")
    assert '"currency":"USD"' in canonical
    assert '"version":2' in canonical


# -------------------------------------------------- currency: endpoint behaviour

def test_omitting_the_currency_header_works(db, hub, client, mcp_enabled):
    response = client.post(MCP_PATH, json=mcp_body(), headers=mcp_headers())
    assert response.status_code == 200
    assert data(response)["status"] == "COMPLETED"


@pytest.mark.parametrize("currency", ["USD", "usd", " Usd "])
def test_the_system_currency_is_accepted_in_any_casing(db, hub, client, mcp_enabled, currency):
    response = client.post(MCP_PATH, json=mcp_body(),
                           headers={**mcp_headers(), "X-Currency": currency})
    assert response.status_code == 200


def test_same_key_with_differently_cased_currency_replays(db, hub, client, mcp_enabled):
    first = client.post(MCP_PATH, json=mcp_body(), headers={**mcp_headers(), "X-Currency": "USD"})
    second = client.post(MCP_PATH, json=mcp_body(), headers={**mcp_headers(), "X-Currency": "usd"})

    assert (first.status_code, second.status_code) == (200, 200)
    assert data(second)["idempotent_replay"] is True
    assert data(second)["order_id"] == data(first)["order_id"]
    assert len(db.tables["user_order"]) == 1


def test_an_absent_header_and_the_system_currency_are_the_same_identity(db, hub, client, mcp_enabled):
    first = client.post(MCP_PATH, json=mcp_body(), headers=mcp_headers())
    second = client.post(MCP_PATH, json=mcp_body(), headers={**mcp_headers(), "X-Currency": "USD"})

    assert data(second)["idempotent_replay"] is True
    assert len(db.tables["user_order"]) == 1


@pytest.mark.parametrize("currency", ["EUR", "eur", "GBP", "JPY", "ZZZ"])
def test_a_non_system_currency_is_rejected_before_anything_happens(db, hub, client, mcp_enabled,
                                                                    currency):
    response = client.post(MCP_PATH, json=mcp_body(),
                           headers={**mcp_headers(), "X-Currency": currency})

    assert response.status_code == 400
    assert response.json()["developerMessage"].startswith("MCP purchases are settled in USD only")
    # Rejected before the claim: no record, no order, no debit, no misleading replay.
    assert db.tables.get("mcp_purchase_idempotency") is None
    assert db.tables.get("user_order") is None
    assert db.tables.get("user_wallet_transaction") is None
    assert hub.create_order_calls == 0


def test_a_used_key_presented_with_another_currency_never_replays_a_success(db, hub, client,
                                                                             mcp_enabled):
    first = client.post(MCP_PATH, json=mcp_body(), headers={**mcp_headers(), "X-Currency": "USD"})
    assert first.status_code == 200

    second = client.post(MCP_PATH, json=mcp_body(), headers={**mcp_headers(), "X-Currency": "EUR"})

    # It must not execute, and must not answer with the USD purchase's success body.
    assert second.status_code == 400
    assert data(second) is None or data(second).get("status") != "COMPLETED"
    assert len(db.tables["user_order"]) == 1
    assert hub.create_order_calls == 1


def test_the_currency_bound_into_identity_would_conflict_if_it_were_ever_accepted(db, hub, client,
                                                                                   mcp_enabled,
                                                                                   monkeypatch):
    """If the single-currency rule is ever relaxed, identity already separates them.

    Proven at the hash layer rather than over HTTP, because the endpoint rejects a
    foreign currency outright today - which is the stronger behaviour.
    """
    usd = build_request_hash(user_id=USER_A_ID, operation=MCP_WALLET_BUNDLE_ASSIGN,
                             bundle_code="b", payment_type="Wallet", related_search=None,
                             currency="USD")
    eur = build_request_hash(user_id=USER_A_ID, operation=MCP_WALLET_BUNDLE_ASSIGN,
                             bundle_code="b", payment_type="Wallet", related_search=None,
                             currency="EUR")
    assert usd != eur

    # A stored record under one currency + the same key under another => CONFLICT,
    # which is what the claim function returns on a request-hash mismatch.
    monkeypatch.setenv("SYSTEM_CURRENCY", "EUR")
    response = client.post(MCP_PATH, json=mcp_body(),
                           headers={**mcp_headers(), "X-Currency": "EUR"})
    assert response.status_code == 200

    monkeypatch.setenv("SYSTEM_CURRENCY", "USD")
    conflict = client.post(MCP_PATH, json=mcp_body(),
                           headers={**mcp_headers(), "X-Currency": "USD"})
    assert conflict.status_code == 409
    assert "already used for a different purchase" in conflict.json()["developerMessage"]
    assert len(db.tables["user_order"]) == 1


def test_locale_is_not_part_of_the_identity(db, hub, client, mcp_enabled):
    first = client.post(MCP_PATH, json=mcp_body(),
                        headers={**mcp_headers(), "Accept-Language": "en"})
    second = client.post(MCP_PATH, json=mcp_body(),
                         headers={**mcp_headers(), "Accept-Language": "ar"})

    # Locale is provably inert in the wallet path, so it must replay rather than conflict.
    assert (first.status_code, second.status_code) == (200, 200)
    assert data(second)["idempotent_replay"] is True
    assert len(db.tables["user_order"]) == 1


def test_a_different_key_is_still_a_separate_purchase_under_one_currency(db, hub, client, mcp_enabled):
    client.post(MCP_PATH, json=mcp_body(), headers=mcp_headers())
    second = client.post(MCP_PATH, json=mcp_body(),
                         headers=mcp_headers(idempotency_key=OTHER_VALID_KEY))

    assert second.status_code == 200
    assert data(second)["idempotent_replay"] is False
    assert len(db.tables["user_order"]) == 2
