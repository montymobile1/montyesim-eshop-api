"""Tests for the LinkAja payment integration.

What is asserted here: the payment-type vocabulary, the fail-closed enable
flag, configuration validation, the HTTP transport's error handling and
redaction, the normalized status abstraction, the additive dispatch, the
protocol as specified in "LinkAja Applink SNAP" v1.1 (request bodies, status
vocabulary, notify body, notify signature, notify response), the callback
pipeline (verification, lookup, idempotency, amount reconciliation, status
gating, provisioning after the acknowledgement) and the backward compatibility
of the shared response contract.

Where the Postman collection and the spec disagree, the spec wins, and these
tests pin the spec's values (urlParams / PAY_RETURN, serviceCode 55).

No test in this file performs a real network call.
"""

import asyncio
import base64 as _b64
import importlib.util
import json as _json
import os
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.config.constants import ErrorMessages, PaymentStatusEnum
from app.config.db import OrderStatusEnum, PaymentTypeEnum, UserOrderType
from app.exceptions import CustomException
from app.models.user import UserOrderModel
from app.schemas.bundle import AssignRequest, AssignTopUpRequest, PaymentIntentResponse
from app.services.integration.link_aja import (
    NON_FINAL_STATUSES,
    NOTIFY_SUCCESS_CODE,
    PROVIDER_STATUS_MAP,
    PROVISIONING_STATUSES,
    LinkAjaCallbackInvalidException,
    LinkAjaCallbackResult,
    LinkAjaCallbackVerificationException,
    LinkAjaConfig,
    LinkAjaConfigurationException,
    LinkAjaDisabledException,
    LinkAjaException,
    LinkAjaHttpClient,
    LinkAjaNotConfiguredException,
    LinkAjaNotifyAck,
    LinkAjaOrderData,
    LinkAjaOrderNotFoundException,
    LinkAjaPaymentRequest,
    LinkAjaPaymentResult,
    LinkAjaPaymentStatus,
    LinkAjaProviderException,
    LinkAjaService,
    ORDER_DATA_KEY,
    TokenCache,
    embed_order_data,
    frontend_return_url,
    is_link_aja_enabled,
    is_provisioning_status,
    map_provider_status,
    notify_url_for_order,
    read_order_data,
    reset_token_caches,
)
from app.schemas.home import BundleDTO
from app.services.integration.link_aja import signature as snap
from app.services.integration.link_aja.amount import parse_usd_to_idr_rate, to_linkaja_amount
from app.services.integration.link_aja.sanitize import REDACTED, redact, safe_url
from app.services.integration.link_aja.token_cache import REFRESH_MARGIN_SECONDS
from tests.mocks import get_bundle_mock, get_user_model_mock

# Throwaway RSA key for signing tests - never LinkAja's key.
_TEST_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_TEST_KEY_PEM = _TEST_KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption()).decode()

# A second throwaway pair standing in for LinkAja's notify-signing key.
_NOTIFY_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_NOTIFY_PUBLIC_PEM = _NOTIFY_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

# Paths as documented in the spec (§4.1, §4.2, §4.4) and in Credential.txt.
_DOC_PATHS = dict(
    auth_path="/bi/v1.0/access-token/b2b",
    create_payment_path="/bi/applink/v1.0/debit/payment-host-to-host",
    payment_status_path="/bi/applink/v1.0/debit/status",
)

# Spec §4.3: the URI LinkAja calls, and our own route for it.
_NOTIFY_SPEC_PATH = "/bi/applink/v1.0/debit/notify"
_NOTIFY_API_PATH = "/api/v1/callback/linkaja"


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    """Tokens are cached per credentials and shared between service instances;
    every test starts with an empty cache so call counts are deterministic."""
    reset_token_caches()
    yield
    reset_token_caches()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOTIFY_URL = "https://esim.invalid/bi/applink/v1.0/debit/notify"


def _full_config(**overrides) -> LinkAjaConfig:
    """A configuration with every *deployment* value present.

    Contract values (paths, notify public key) stay empty unless a test
    overrides them (see `_DOC_PATHS`, `_notify_config`), so the default never
    issues a request and never accepts a notify.
    """
    base = dict(
        enabled=True,
        base_url="https://linkaja.invalid",
        client_id="cid",
        client_secret="csecret",
        partner_id="pid",
        channel_id="CHANNEL-TEST",
        merchant_id="MERCHANT-TEST",
        terminal_id="MERCHANT-TEST",
        terminal_name="TERMINAL-TEST",
        private_key=_TEST_KEY_PEM,
        usd_to_idr_rate="16000",
        notify_url=_NOTIFY_URL,
        callback_url="https://esim.invalid/api/v1/callback/linkaja",
        success_url="https://esim.invalid/ok",
        cancel_url="https://esim.invalid/cancel",
    )
    base.update(overrides)
    return LinkAjaConfig(**base)


def _payment_request(order_id="order-1") -> LinkAjaPaymentRequest:
    return LinkAjaPaymentRequest(merchant_order_id=order_id, amount_minor_units=1099, currency="USD")


def _notify_config(**overrides) -> LinkAjaConfig:
    """Fully configured for inbound notifies: LinkAja's (test) public key present."""
    return _full_config(notify_public_key=_NOTIFY_PUBLIC_PEM, **overrides)


# Inform Payment sample request body, spec v1.1 p.15-16, with our order
# reference and the amount our fixture order converts to (1099 USD cents at
# rate 16000). Note the spec sample's "6000" without decimals.
_SPEC_NOTIFY_BODY = {
    "additionalInfo": {
        "approvalCode": "D60232EF01",
        "items": [
            {"id": "01", "name": "Item 1", "qty": "1", "unitPrice": "1000"},
            {"id": "02", "name": "Item 2", "qty": "1", "unitPrice": "5000"},
        ],
        "terminalId": "snap applink",
        "terminalName": "snap applink",
    },
    "amount": {"currency": "IDR", "value": "175840"},
    "createdTime": "20240326093200",
    "finishedTime": "20240326093200",
    "latestTransactionStatus": "00",
    "merchantId": "MERCHANT-TEST",
    "originalPartnerReferenceNo": "order-1",
    "originalReferenceNo": "LA-TXN-9",
}
_NOTIFY_TS = "2026-10-01T10:00:00+07:00"


def _signed_notify_headers(body: bytes, endpoint_url=_NOTIFY_SPEC_PATH, key=_NOTIFY_KEY,
                           timestamp=_NOTIFY_TS) -> dict:
    """Headers LinkAja would send (p.14), signed per §5.1.3 with the test key."""
    return {
        "Content-Type": "application/json",
        "X-TIMESTAMP": timestamp,
        "X-SIGNATURE": snap.notify_signature(key, "POST", endpoint_url, body, timestamp),
        "X-PARTNER-ID": "pid",
        "X-EXTERNAL-ID": "20261001100000001",
        "CHANNEL-ID": "CHANNEL-TEST",
    }


# ---------------------------------------------------------------------------
# 1. Payment-type vocabulary
# ---------------------------------------------------------------------------

def test_link_aja_wire_value_is_exactly_linkaja():
    assert PaymentTypeEnum.LINK_AJA == "LinkAja"
    assert PaymentTypeEnum.LINK_AJA.value == "LinkAja"


def test_existing_payment_type_values_are_unchanged():
    assert PaymentTypeEnum.CARD == "Card"
    assert PaymentTypeEnum.WALLET == "Wallet"
    assert PaymentTypeEnum.DCB == "DCB"
    assert {p.value for p in PaymentTypeEnum} == {"Card", "Wallet", "DCB", "LinkAja"}


def test_assign_request_accepts_link_aja():
    request = AssignRequest.model_validate({
        "bundle_code": "B123", "related_search": None,
        "promo_code": None, "affiliate_code": None, "payment_type": "LinkAja",
    })
    assert request.payment_type == PaymentTypeEnum.LINK_AJA


@pytest.mark.parametrize("wire_value,expected", [
    ("Card", PaymentTypeEnum.CARD),
    ("Wallet", PaymentTypeEnum.WALLET),
    ("DCB", PaymentTypeEnum.DCB),
])
def test_assign_request_still_accepts_existing_payment_types(wire_value, expected):
    request = AssignRequest.model_validate({
        "bundle_code": "B123", "related_search": None,
        "promo_code": None, "affiliate_code": None, "payment_type": wire_value,
    })
    assert request.payment_type == expected


def test_assign_request_without_payment_type_still_defaults_to_card():
    request = AssignRequest.model_validate({
        "bundle_code": "B123", "related_search": None,
        "promo_code": None, "affiliate_code": None,
    })
    assert request.payment_type == PaymentTypeEnum.CARD


def test_assign_request_still_rejects_unknown_payment_type():
    with pytest.raises(Exception):
        AssignRequest.model_validate({
            "bundle_code": "B123", "related_search": None,
            "promo_code": None, "affiliate_code": None, "payment_type": "Bitcoin",
        })


def test_assign_top_up_request_default_is_still_card():
    assert AssignTopUpRequest(iccid="8910", bundle_code="B123").payment_type == "Card"


# ---------------------------------------------------------------------------
# 2. Order persistence
# ---------------------------------------------------------------------------

def test_user_order_model_accepts_and_reads_back_link_aja():
    order = UserOrderModel(id="o1", user_id="u1", amount=1099, currency="USD",
                           payment_type=PaymentTypeEnum.LINK_AJA)
    assert order.payment_type == PaymentTypeEnum.LINK_AJA

    # Repository-style round trip: the model is re-validated from a raw DB row.
    row = order.model_dump()
    assert row["payment_type"] == "LinkAja"
    assert UserOrderModel(**row).payment_type == PaymentTypeEnum.LINK_AJA


@pytest.mark.parametrize("wire_value", ["Card", "Wallet", "DCB"])
def test_user_order_model_still_reads_existing_payment_types(wire_value):
    order = UserOrderModel(id="o1", user_id="u1", amount=1, currency="USD", payment_type=wire_value)
    assert order.payment_type == wire_value


def test_payment_intent_code_carries_the_provider_reference():
    """The provider reference reuses an existing column - no migration needed."""
    order = UserOrderModel(id="o1", user_id="u1", amount=1099, currency="USD",
                           payment_type=PaymentTypeEnum.LINK_AJA,
                           payment_intent_code="LA-TXN-123")
    assert UserOrderModel(**order.model_dump()).payment_intent_code == "LA-TXN-123"


# ---------------------------------------------------------------------------
# 3. Fail-closed flag and configuration
# ---------------------------------------------------------------------------

def test_link_aja_disabled_by_default_when_env_var_absent():
    with patch.dict(os.environ, {}, clear=True):
        assert is_link_aja_enabled() is False
        assert LinkAjaConfig.from_env().enabled is False


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "", "   ", "garbage"])
def test_link_aja_disabled_for_non_truthy_values(value):
    with patch.dict(os.environ, {"LINKAJA_ENABLED": value}):
        assert is_link_aja_enabled() is False


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "on", " true "])
def test_link_aja_enabled_for_truthy_values(value):
    with patch.dict(os.environ, {"LINKAJA_ENABLED": value}):
        assert is_link_aja_enabled() is True


def test_config_construction_never_raises_without_any_env():
    """Startup safety: building config with a bare environment must not raise."""
    with patch.dict(os.environ, {}, clear=True):
        config = LinkAjaConfig.from_env()
    assert config.enabled is False
    assert config.base_url == ""
    assert config.client_secret == ""


def test_config_treats_whitespace_only_values_as_absent():
    with patch.dict(os.environ, {"LINKAJA_BASE_URL": "   "}):
        assert LinkAjaConfig.from_env().base_url == ""


def test_config_falls_back_to_default_timeouts_when_unparseable():
    with patch.dict(os.environ, {"LINKAJA_CONNECT_TIMEOUT": "not-a-number"}):
        config = LinkAjaConfig.from_env()
    assert config.connect_timeout > 0


def test_missing_payment_configuration_is_reported_by_name():
    config = LinkAjaConfig(enabled=True)
    missing = config.missing_payment_settings()
    assert "LINKAJA_BASE_URL" in missing
    assert "LINKAJA_CLIENT_SECRET" in missing
    assert "LINKAJA_PARTNER_ID" in missing
    assert "LINKAJA_USD_TO_IDR_RATE" in missing
    assert "LINKAJA_PRIVATE_KEY or LINKAJA_PRIVATE_KEY_PATH" in missing


def test_callback_success_and_cancel_urls_are_not_required_for_payment():
    """No LinkAja request carries them: urlParams takes the notify URL."""
    _full_config(callback_url="", success_url="", cancel_url="").validate_for_payment()


def test_missing_notify_url_blocks_payment_by_name():
    with pytest.raises(LinkAjaConfigurationException) as exc:
        _full_config(notify_url="").validate_for_payment()
    assert "LINKAJA_NOTIFY_URL" in exc.value.details


def test_notify_url_is_read_from_the_environment():
    with patch.dict(os.environ, {"LINKAJA_NOTIFY_URL": _NOTIFY_URL}):
        assert LinkAjaConfig.from_env().notify_url == _NOTIFY_URL


def test_missing_fx_rate_blocks_payment_by_name():
    with pytest.raises(LinkAjaConfigurationException) as exc:
        _full_config(usd_to_idr_rate="").validate_for_payment()
    assert "LINKAJA_USD_TO_IDR_RATE" in exc.value.details


def test_validate_for_payment_raises_descriptive_configuration_error():
    with pytest.raises(LinkAjaConfigurationException) as exc:
        LinkAjaConfig(enabled=True).validate_for_payment()
    assert exc.value.name == ErrorMessages.LINKAJA_CONFIGURATION_INVALID
    assert exc.value.code == 503
    assert "LINKAJA_BASE_URL" in exc.value.details


def test_validate_for_payment_passes_when_fully_configured():
    _full_config().validate_for_payment()  # must not raise


def test_validate_for_callback_requires_the_notify_public_key():
    """Without LinkAja's public key nothing can authenticate a notify."""
    with pytest.raises(LinkAjaConfigurationException) as exc:
        _full_config().validate_for_callback()
    assert "LINKAJA_NOTIFY_PUBLIC_KEY" in exc.value.details


def test_validate_for_callback_accepts_key_text_or_key_path():
    _full_config(notify_public_key=_NOTIFY_PUBLIC_PEM).validate_for_callback()
    _full_config(notify_public_key_path="/etc/linkaja/notify.pem").validate_for_callback()


def test_notify_settings_are_read_from_the_environment():
    with patch.dict(os.environ, {"LINKAJA_NOTIFY_PUBLIC_KEY": " pem ",
                                 "LINKAJA_NOTIFY_PUBLIC_KEY_PATH": "/k.pem",
                                 "LINKAJA_NOTIFY_ENDPOINT_URL": "/bi/applink/v1.0/debit/notify",
                                 "LINKAJA_URL_PARAM_IS_DEEPLINK": "Y"}):
        config = LinkAjaConfig.from_env()
    assert config.notify_public_key == "pem"
    assert config.notify_public_key_path == "/k.pem"
    assert config.notify_endpoint_url == "/bi/applink/v1.0/debit/notify"
    assert config.url_param_is_deeplink == "Y"


def test_is_deeplink_defaults_to_n():
    with patch.dict(os.environ, {}, clear=True):
        assert LinkAjaConfig.from_env().url_param_is_deeplink == "N"
    with patch.dict(os.environ, {"LINKAJA_URL_PARAM_IS_DEEPLINK": "  "}):
        assert LinkAjaConfig.from_env().url_param_is_deeplink == "N"


def test_configuration_error_never_contains_secret_values():
    config = LinkAjaConfig(enabled=True, client_secret="super-secret-value")
    with pytest.raises(LinkAjaConfigurationException) as exc:
        config.validate_for_payment()
    assert "super-secret-value" not in exc.value.details


# ---------------------------------------------------------------------------
# 4. Redaction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", [
    "Authorization", "client_secret", "access_token", "X-Signature", "apiKey", "password",
])
def test_sensitive_keys_are_redacted(key):
    assert redact({key: "sensitive"})[key] == REDACTED


def test_redaction_is_recursive_and_keeps_harmless_values():
    payload = {"orderId": "o1", "auth": {"access_token": "t"}, "items": [{"secret": "s", "qty": 2}]}
    result = redact(payload)
    assert result["orderId"] == "o1"
    assert result["auth"]["access_token"] == REDACTED
    assert result["items"][0]["secret"] == REDACTED
    assert result["items"][0]["qty"] == 2


def test_safe_url_strips_query_string():
    assert safe_url("https://x.invalid/pay?token=abc") == "https://x.invalid/pay"


# ---------------------------------------------------------------------------
# 5. HTTP client
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_refuses_to_call_without_base_url():
    client = LinkAjaHttpClient(LinkAjaConfig(enabled=True))
    with patch("httpx.AsyncClient") as transport:
        with pytest.raises(LinkAjaNotConfiguredException):
            await client.request_json("POST", "/some/path", "test")
    transport.assert_not_called()


@pytest.mark.asyncio
async def test_client_refuses_to_guess_an_unconfigured_endpoint_path():
    """An empty path means the spec has not been applied - never guess one."""
    client = LinkAjaHttpClient(_full_config())
    with patch("httpx.AsyncClient") as transport:
        with pytest.raises(LinkAjaNotConfiguredException) as exc:
            await client.request_json("POST", "", "payment creation")
    transport.assert_not_called()
    assert "not configured" in exc.value.details


@pytest.mark.asyncio
async def test_client_translates_timeout_into_provider_error():
    client = LinkAjaHttpClient(_full_config())
    with patch("httpx.AsyncClient") as transport:
        instance = transport.return_value.__aenter__.return_value
        instance.request = AsyncMock(side_effect=httpx.TimeoutException("slow"))
        with pytest.raises(LinkAjaProviderException) as exc:
            await client.request_json("POST", "/pay", "payment creation")
    assert exc.value.code == 502
    assert "timed out" in exc.value.details


@pytest.mark.asyncio
async def test_client_translates_transport_error_into_provider_error():
    client = LinkAjaHttpClient(_full_config())
    with patch("httpx.AsyncClient") as transport:
        instance = transport.return_value.__aenter__.return_value
        instance.request = AsyncMock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(LinkAjaProviderException):
            await client.request_json("POST", "/pay", "payment creation")


@pytest.mark.asyncio
async def test_client_rejects_error_status_without_leaking_body():
    client = LinkAjaHttpClient(_full_config())
    response = httpx.Response(500, json={"secret_debug": "internal detail"},
                              request=httpx.Request("POST", "https://linkaja.invalid/pay"))
    with patch("httpx.AsyncClient") as transport:
        instance = transport.return_value.__aenter__.return_value
        instance.request = AsyncMock(return_value=response)
        with pytest.raises(LinkAjaProviderException) as exc:
            await client.request_json("POST", "/pay", "payment creation")
    assert "500" in exc.value.details
    assert "internal detail" not in exc.value.details


@pytest.mark.asyncio
async def test_client_rejects_malformed_json():
    client = LinkAjaHttpClient(_full_config())
    response = httpx.Response(200, text="<html>not json</html>",
                              request=httpx.Request("POST", "https://linkaja.invalid/pay"))
    with patch("httpx.AsyncClient") as transport:
        instance = transport.return_value.__aenter__.return_value
        instance.request = AsyncMock(return_value=response)
        with pytest.raises(LinkAjaProviderException) as exc:
            await client.request_json("POST", "/pay", "payment creation")
    assert "malformed" in exc.value.details


@pytest.mark.asyncio
async def test_client_rejects_json_of_unexpected_shape():
    client = LinkAjaHttpClient(_full_config())
    response = httpx.Response(200, json=["not", "an", "object"],
                              request=httpx.Request("POST", "https://linkaja.invalid/pay"))
    with patch("httpx.AsyncClient") as transport:
        instance = transport.return_value.__aenter__.return_value
        instance.request = AsyncMock(return_value=response)
        with pytest.raises(LinkAjaProviderException):
            await client.request_json("POST", "/pay", "payment creation")


@pytest.mark.asyncio
async def test_client_returns_decoded_object_on_success():
    client = LinkAjaHttpClient(_full_config())
    response = httpx.Response(200, json={"ok": True},
                              request=httpx.Request("POST", "https://linkaja.invalid/pay"))
    with patch("httpx.AsyncClient") as transport:
        instance = transport.return_value.__aenter__.return_value
        instance.request = AsyncMock(return_value=response)
        assert await client.request_json("POST", "/pay", "payment creation") == {"ok": True}


def test_client_applies_configured_timeouts():
    client = LinkAjaHttpClient(_full_config(connect_timeout=3.0, read_timeout=7.0))
    timeout = client._timeout()
    assert timeout.connect == 3.0
    assert timeout.read == 7.0


# ---------------------------------------------------------------------------
# 6. Status abstraction
# ---------------------------------------------------------------------------

def test_normalized_statuses_exist():
    assert {s.value for s in LinkAjaPaymentStatus} == {
        "PENDING", "SUCCESS", "FAILED", "CANCELED", "EXPIRED", "REFUNDED", "NOT_FOUND", "UNKNOWN"}


def test_only_success_may_provision():
    assert PROVISIONING_STATUSES == frozenset({LinkAjaPaymentStatus.SUCCESS})
    assert is_provisioning_status(LinkAjaPaymentStatus.SUCCESS) is True
    for status in LinkAjaPaymentStatus:
        if status != LinkAjaPaymentStatus.SUCCESS:
            assert is_provisioning_status(status) is False


def test_provider_status_map_is_exactly_the_spec_vocabulary():
    """Spec v1.1 §5.4 "List of transaction status" (p.25). Nothing else may be
    mapped: an unmapped status is how an unpaid order would get an eSIM."""
    assert PROVIDER_STATUS_MAP == {
        "00": LinkAjaPaymentStatus.SUCCESS,
        "04": LinkAjaPaymentStatus.REFUNDED,
        "06": LinkAjaPaymentStatus.FAILED,
        "07": LinkAjaPaymentStatus.NOT_FOUND,
    }


@pytest.mark.parametrize("raw, expected", [
    ("00", LinkAjaPaymentStatus.SUCCESS),
    ("04", LinkAjaPaymentStatus.REFUNDED),
    ("06", LinkAjaPaymentStatus.FAILED),
    ("07", LinkAjaPaymentStatus.NOT_FOUND),
    (" 00 ", LinkAjaPaymentStatus.SUCCESS),
])
def test_every_spec_status_code_maps(raw, expected):
    assert map_provider_status(raw) == expected
    assert is_provisioning_status(map_provider_status(raw)) is (expected == LinkAjaPaymentStatus.SUCCESS)


def test_refunded_and_not_found_never_provision_and_not_found_is_non_final():
    assert is_provisioning_status(LinkAjaPaymentStatus.REFUNDED) is False
    assert is_provisioning_status(LinkAjaPaymentStatus.NOT_FOUND) is False
    assert LinkAjaPaymentStatus.NOT_FOUND in NON_FINAL_STATUSES
    assert LinkAjaPaymentStatus.REFUNDED not in NON_FINAL_STATUSES
    assert LinkAjaPaymentStatus.SUCCESS not in NON_FINAL_STATUSES


@pytest.mark.parametrize("raw", [None, "", "SUCCESS", "PAID", "0", "000", "01", "anything"])
def test_unmapped_provider_status_is_unknown_and_never_provisions(raw):
    status = map_provider_status(raw)
    assert status == LinkAjaPaymentStatus.UNKNOWN
    assert is_provisioning_status(status) is False


# ---------------------------------------------------------------------------
# 7. Integration service gates and spec-blocked seams
# ---------------------------------------------------------------------------

def test_service_construction_is_safe_without_any_configuration():
    """Constructing the service must never raise - this is what keeps app
    startup working on deployments that have never heard of LinkAja."""
    with patch.dict(os.environ, {}, clear=True):
        service = LinkAjaService()
    assert service.is_enabled() is False


def test_ensure_enabled_raises_when_flag_is_off():
    service = LinkAjaService(config=LinkAjaConfig(enabled=False))
    with pytest.raises(LinkAjaDisabledException) as exc:
        service.ensure_enabled()
    assert exc.value.code == 400
    assert exc.value.name == ErrorMessages.LINKAJA_DISABLED


def test_disabled_takes_precedence_over_missing_configuration():
    """A deployment that never opted in gets 'not enabled', not a 503 about
    credentials it was never supposed to have."""
    service = LinkAjaService(config=LinkAjaConfig(enabled=False))
    with pytest.raises(LinkAjaDisabledException):
        service.ensure_payments_available()


def test_enabled_but_unconfigured_raises_configuration_error():
    service = LinkAjaService(config=LinkAjaConfig(enabled=True))
    with pytest.raises(LinkAjaConfigurationException):
        service.ensure_payments_available()


@pytest.mark.asyncio
async def test_authentication_without_a_configured_path_makes_no_http_call():
    service = LinkAjaService(config=_full_config())
    with patch("httpx.AsyncClient") as transport:
        with pytest.raises(LinkAjaNotConfiguredException) as exc:
            await service.authenticate()
    transport.assert_not_called()
    assert "endpoint path" in exc.value.details


@pytest.mark.asyncio
async def test_create_payment_without_configured_paths_makes_no_http_call():
    service = LinkAjaService(config=_full_config())
    with patch("httpx.AsyncClient") as transport:
        with pytest.raises(LinkAjaNotConfiguredException):
            await service.create_payment(_payment_request())
    transport.assert_not_called()


@pytest.mark.asyncio
async def test_get_payment_status_without_configured_paths_makes_no_http_call():
    service = LinkAjaService(config=_full_config())
    with patch("httpx.AsyncClient") as transport:
        with pytest.raises(LinkAjaNotConfiguredException):
            await service.get_payment_status("LA-TXN-1")
    transport.assert_not_called()


def test_verify_callback_without_the_public_key_fails_closed_before_reading_anything():
    """Key unset -> rejected, whatever the request looks like."""
    service = LinkAjaService(config=_full_config())
    with pytest.raises(LinkAjaConfigurationException) as exc:
        service.verify_callback(raw_body=b"{}", headers={"X-SIGNATURE": "abc", "X-TIMESTAMP": _TS},
                                request_path=_NOTIFY_SPEC_PATH)
    assert "LINKAJA_NOTIFY_PUBLIC_KEY" in exc.value.details


def test_verify_callback_requires_the_flag_and_config_first():
    disabled = LinkAjaService(config=LinkAjaConfig(enabled=False))
    with pytest.raises(LinkAjaDisabledException):
        disabled.verify_callback(raw_body=b"{}", headers={})

    unconfigured = LinkAjaService(config=_full_config())
    with pytest.raises(LinkAjaConfigurationException):
        unconfigured.verify_callback(raw_body=b"{}", headers={})


def test_every_link_aja_error_is_a_translatable_custom_exception():
    for exception in (
        LinkAjaDisabledException(details="d"),
        LinkAjaConfigurationException(details="d"),
        LinkAjaNotConfiguredException(details="d"),
        LinkAjaProviderException(details="d"),
        LinkAjaCallbackVerificationException(details="d"),
        LinkAjaCallbackInvalidException(details="d"),
        LinkAjaOrderNotFoundException(details="d"),
    ):
        assert isinstance(exception, LinkAjaException)
        assert isinstance(exception, CustomException)
        assert exception.name in {e.value for e in ErrorMessages}


def test_no_provider_endpoint_path_is_hardcoded_in_the_package():
    """Endpoint paths must come from configuration, never from source."""
    import inspect

    from app.services.integration.link_aja import client, config, service

    for module in (client, config, service):
        source = inspect.getsource(module).lower()
        # No LinkAja hostname or versioned API path may be baked into source;
        # they must come from configuration.
        assert "linkaja.co" not in source
        assert "/v1/payment" not in source
        assert "api.linkaja" not in source


# ---------------------------------------------------------------------------
# 8. Dispatch - additive, fail-closed, existing branches untouched
# ---------------------------------------------------------------------------

@pytest.fixture
def service_and_mocks():
    """A UserBundleService with every collaborator mocked out."""
    from app.services.user_service import UserBundleService

    with patch("app.services.user_service.esim_hub_service_instance"), \
            patch("app.services.user_service.NotificationRepo"), \
            patch("app.services.user_service.UserOrderRepo"), \
            patch("app.services.user_service.UserProfileRepo"), \
            patch("app.services.user_service.UserProfileBundleRepo"), \
            patch("app.services.user_service.UserWalletService"), \
            patch("app.services.user_service.PromotionService"), \
            patch("app.services.user_service.BundleService"), \
            patch("app.services.user_service.CurrencyService"), \
            patch("app.services.user_service.UserRepo"), \
            patch("app.services.user_service.TaskExecutor"), \
            patch("app.services.user_service.BundleTranslationRepo"), \
            patch("app.services.user_service.TagRepo"), \
            patch("app.services.user_service.dcb_service_instance"), \
            patch("app.services.user_service.link_aja_service_instance"):
        service = UserBundleService()

    bundle = get_bundle_mock()
    bundle.original_price = 10.99

    esim_hub = AsyncMock()
    esim_hub.get_bundle_by_id.return_value = bundle
    esim_hub.check_bundle_applicable.return_value = True
    service._UserBundleService__esim_hub_service = esim_hub

    order = UserOrderModel(id="order-1", user_id="user-1", amount=1099, modified_amount=1099,
                           currency="USD", bundle_id="B123", order_type=UserOrderType.ASSIGN,
                           bundle_data=bundle.model_dump_json())  # as `assign` persists it at creation
    order_repo = MagicMock()
    order_repo.create.return_value = order
    service._UserBundleService__user_order_repo = order_repo

    bundle_service = MagicMock()
    bundle_service.buy_bundle = AsyncMock()
    bundle_service.top_up_bundle = AsyncMock()
    service._UserBundleService__bundle_service = bundle_service

    wallet_service = MagicMock()
    service._UserBundleService__user_wallet_service = wallet_service

    dcb = AsyncMock()
    service._UserBundleService__dcb_service = dcb

    promo = MagicMock()
    promo.is_referral_code.return_value = False
    service._UserBundleService__promotion_service = promo

    service._UserBundleService__currency_service = MagicMock()
    service._UserBundleService__user_repo = MagicMock()
    service._UserBundleService__task_executor = MagicMock()

    return service, {
        "order": order, "order_repo": order_repo, "bundle_service": bundle_service,
        "wallet_service": wallet_service, "dcb": dcb, "esim_hub": esim_hub,
    }


def _install_link_aja(user_service, config: LinkAjaConfig, create_payment=None) -> LinkAjaService:
    """Install a REAL LinkAjaService so the enable/config gates are exercised."""
    link_aja = LinkAjaService(config=config)
    if create_payment is not None:
        link_aja.create_payment = create_payment
    user_service._UserBundleService__link_aja_service = link_aja
    return link_aja


def _assign_request(payment_type: str):
    return AssignRequest.model_validate({
        "bundle_code": "B123",
        "related_search": {"region": None, "countries": None},
        "promo_code": None, "affiliate_code": None, "payment_type": payment_type,
    })


async def _assign(service, payment_type="LinkAja"):
    return await service.assign(user=get_user_model_mock(), device_id="d1",
                                assign_request=_assign_request(payment_type),
                                x_currency="USD", locale="en", request=MagicMock())


@pytest.mark.asyncio
async def test_link_aja_rejected_cleanly_when_flag_disabled(service_and_mocks):
    service, m = service_and_mocks
    _install_link_aja(service, LinkAjaConfig(enabled=False))

    with pytest.raises(LinkAjaDisabledException) as exc:
        await _assign(service)

    assert exc.value.name == ErrorMessages.LINKAJA_DISABLED
    assert exc.value.code == 400
    m["bundle_service"].buy_bundle.assert_not_called()
    m["bundle_service"].top_up_bundle.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_link_aja_order_is_not_left_payable(service_and_mocks):
    service, m = service_and_mocks
    _install_link_aja(service, LinkAjaConfig(enabled=False))

    with pytest.raises(LinkAjaDisabledException):
        await _assign(service)

    m["order_repo"].update_by.assert_called_with(
        where={"id": "order-1"},
        data={"payment_status": OrderStatusEnum.FAILURE, "order_status": OrderStatusEnum.FAILURE},
    )


@pytest.mark.asyncio
async def test_enabled_but_missing_configuration_fails_cleanly(service_and_mocks):
    """Enabled without credentials must be a descriptive 503, not a crash, and
    must not provision."""
    service, m = service_and_mocks
    _install_link_aja(service, LinkAjaConfig(enabled=True))

    with pytest.raises(LinkAjaConfigurationException) as exc:
        await _assign(service)

    assert exc.value.name == ErrorMessages.LINKAJA_CONFIGURATION_INVALID
    assert "LINKAJA_BASE_URL" in exc.value.details
    m["bundle_service"].buy_bundle.assert_not_called()


@pytest.mark.asyncio
async def test_enabled_and_configured_reaches_the_provider_seam(service_and_mocks):
    """Fully credentialed but without endpoint paths, the flow stops at the
    transport - with no HTTP call and no provisioning."""
    service, m = service_and_mocks
    _install_link_aja(service, _full_config())

    with patch("httpx.AsyncClient") as transport:
        with pytest.raises(LinkAjaNotConfiguredException):
            await _assign(service)

    transport.assert_not_called()
    m["bundle_service"].buy_bundle.assert_not_called()


@pytest.mark.asyncio
async def test_successful_initiation_persists_reference_and_returns_payment_url(service_and_mocks):
    """The orchestration around the provider call is complete: reference stored
    on the existing column, payment URL returned, nothing provisioned."""
    service, m = service_and_mocks
    create_payment = AsyncMock(return_value=LinkAjaPaymentResult(
        provider_reference="LA-TXN-9", payment_url="https://linkaja.invalid/pay/9",
        status=LinkAjaPaymentStatus.PENDING))
    _install_link_aja(service, _full_config(), create_payment=create_payment)

    response = await _assign(service)

    # Provider received our order id as the merchant reference.
    sent = create_payment.await_args.args[0]
    assert sent.merchant_order_id == "order-1"
    assert sent.amount_minor_units == 1099
    assert sent.currency == "USD"
    assert sent.notify_url == _NOTIFY_URL

    # The IDR quote was persisted (inside bundle_data) BEFORE the provider call,
    # then the reference onto the existing column after it.
    quote_call, reference_call = m["order_repo"].update_by.call_args_list
    assert quote_call.kwargs["where"] == {"id": "order-1"}
    stored = read_order_data(quote_call.kwargs["data"]["bundle_data"])
    assert stored.amount == "175840.00" and stored.usd_to_idr_rate == "16000"
    assert stored.order_amount_minor_units == 1099 and stored.currency == "IDR"
    assert reference_call.kwargs == dict(where={"id": "order-1"}, data={"payment_intent_code": "LA-TXN-9"})
    assert m["order_repo"].update_by.call_args_list.index(quote_call) == 0
    # The bundle snapshot is still readable by every existing consumer.
    assert BundleDTO.model_validate_json(m["order"].bundle_data).bundle_code == get_bundle_mock().bundle_code

    # Pending response carrying the redirect target; no provisioning.
    assert response.data.payment_url == "https://linkaja.invalid/pay/9"
    assert response.data.payment_status == PaymentStatusEnum.PENDING
    assert response.data.order_id == "order-1"
    m["bundle_service"].buy_bundle.assert_not_called()


@pytest.mark.asyncio
async def test_provider_transport_failure_does_not_provision(service_and_mocks):
    service, m = service_and_mocks
    _install_link_aja(service, _full_config(),
                      create_payment=AsyncMock(side_effect=LinkAjaProviderException(details="down")))

    with pytest.raises(LinkAjaProviderException):
        await _assign(service)

    m["bundle_service"].buy_bundle.assert_not_called()


# --- existing payment types must be indifferent to LinkAja ------------------

@pytest.mark.parametrize("flag", ["true", "false"])
@pytest.mark.asyncio
async def test_wallet_flow_unchanged_regardless_of_link_aja_flag(service_and_mocks, flag):
    service, m = service_and_mocks
    wallet_handler = AsyncMock(return_value="wallet-response")
    service._UserBundleService__handle_wallet_payment = wallet_handler

    with patch.dict(os.environ, {"LINKAJA_ENABLED": flag}):
        assert await _assign(service, "Wallet") == "wallet-response"
    wallet_handler.assert_awaited_once()


@pytest.mark.parametrize("flag", ["true", "false"])
@pytest.mark.asyncio
async def test_dcb_flow_unchanged_regardless_of_link_aja_flag(service_and_mocks, flag):
    service, m = service_and_mocks
    dcb_handler = AsyncMock(return_value="dcb-response")
    service._UserBundleService__handle_dcb_payment = dcb_handler

    with patch.dict(os.environ, {"LINKAJA_ENABLED": flag}):
        assert await _assign(service, "DCB") == "dcb-response"
    dcb_handler.assert_awaited_once()


@pytest.mark.parametrize("flag", ["true", "false"])
@pytest.mark.asyncio
async def test_card_flow_unchanged_regardless_of_link_aja_flag(service_and_mocks, flag):
    service, m = service_and_mocks
    card_handler = AsyncMock(return_value="card-response")
    service._UserBundleService__handle_card_payment = card_handler

    with patch.dict(os.environ, {"LINKAJA_ENABLED": flag}):
        assert await _assign(service, "Card") == "card-response"
    card_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_payment_type_still_raises_invalid_payment_type(service_and_mocks):
    service, m = service_and_mocks
    request = _assign_request("Card")
    object.__setattr__(request, "payment_type", "NotAProvider")

    with pytest.raises(CustomException) as exc:
        await service.assign(user=get_user_model_mock(), device_id="d1", assign_request=request,
                             x_currency="USD", locale="en", request=MagicMock())
    assert exc.value.name == ErrorMessages.INVALID_PAYMENT_TYPE


@pytest.mark.asyncio
async def test_link_aja_top_up_is_rejected_pending_documented_support(service_and_mocks):
    """LinkAja top-ups are not in scope: the top-up dispatcher is unchanged and a
    LinkAja top-up falls through to the existing INVALID_PAYMENT_TYPE guard."""
    service, m = service_and_mocks
    _install_link_aja(service, _full_config())

    with pytest.raises(CustomException) as exc:
        await service.assign_top_up(
            user=get_user_model_mock(),
            assign_top_up_request=AssignTopUpRequest(iccid="8910", bundle_code="B123",
                                                     payment_type="LinkAja"),
            device_id="d1", request=MagicMock(), x_currency="USD", locale="en")
    assert exc.value.name == ErrorMessages.INVALID_PAYMENT_TYPE
    m["bundle_service"].top_up_bundle.assert_not_called()


# ---------------------------------------------------------------------------
# 9. Callback pipeline
# ---------------------------------------------------------------------------

class _FakeExecutor:
    """Stands in for `TaskExecutor`: records tasks instead of running them, so a
    test can assert what happened before vs. after the acknowledgement."""

    def __init__(self, accept=True):
        self.tasks = []
        self.accept = accept

    def add_task(self, task):
        if self.accept:
            self.tasks.append(task)
        return self.accept

    def run_all(self):
        """Run queued tasks the way the real executor does: on a worker thread
        with no event loop (the task itself uses asyncio.run)."""
        import threading

        for task in list(self.tasks):
            thread = threading.Thread(target=task)
            thread.start()
            thread.join(10)
            assert not thread.is_alive(), "background provisioning did not finish"


# 1099 USD cents at the fixture rate of 16000 -> the IDR amount we asked for.
_ORDER_IDR = Decimal("175840.00")


def _stored_quote(amount="175840.00", rate="16000") -> LinkAjaOrderData:
    return LinkAjaOrderData(amount=amount, currency="IDR", usd_to_idr_rate=rate,
                            order_amount_minor_units=1099, quoted_at="2026-10-01T10:00:00+07:00")


def _make_callback_service(config=None, executor=None, quote=_stored_quote()):
    from app.services.link_aja_callback_service import LinkAjaCallbackService

    bundle_data = get_bundle_mock().model_dump_json()
    if quote is not None:
        bundle_data = embed_order_data(bundle_data, quote)
    order = UserOrderModel(id="order-1", user_id="user-1", amount=1099, modified_amount=1099,
                           currency="USD", bundle_id="B123", order_type=UserOrderType.ASSIGN,
                           payment_type=PaymentTypeEnum.LINK_AJA,
                           payment_status=OrderStatusEnum.PENDING,
                           bundle_data=bundle_data)

    order_repo = MagicMock()
    order_repo.get_by_id.return_value = order
    order_repo.update_by.return_value = [{"id": "order-1"}]  # claim succeeds

    bundle_service = MagicMock()
    bundle_service.buy_bundle = AsyncMock(return_value="provisioned")
    bundle_service.top_up_bundle = AsyncMock()

    link_aja = LinkAjaService(config=config or _full_config())
    executor = executor or _FakeExecutor()

    service = LinkAjaCallbackService(link_aja_service=link_aja, user_order_repo=order_repo,
                                     bundle_service=bundle_service, task_executor=executor)
    return service, {"order": order, "order_repo": order_repo, "bundle_service": bundle_service,
                     "executor": executor, "link_aja": link_aja}


@pytest.fixture
def callback_service():
    """Pipeline fixture: no notify key, so only `process_verified_callback` is reachable."""
    return _make_callback_service()


@pytest.fixture
def notify_service():
    """Route fixture: LinkAja's (test) public key configured, so signed notifies verify."""
    return _make_callback_service(config=_notify_config())


def _verified(status=LinkAjaPaymentStatus.SUCCESS, order_id="order-1", reference="LA-TXN-9",
              amount=_ORDER_IDR, approval_code="D60232EF01"):
    return LinkAjaCallbackResult(merchant_order_id=order_id, provider_reference=reference,
                                 status=status, verified=True, amount=amount, currency="IDR",
                                 approval_code=approval_code, merchant_id="MERCHANT-TEST")


def _notify_request(body: bytes, headers: dict, path=_NOTIFY_SPEC_PATH, query="") -> MagicMock:
    request = MagicMock()
    request.body = AsyncMock(return_value=body)
    request.headers = headers
    request.url.path = path
    request.url.query = query
    request.url.__str__.return_value = f"http://testserver{path}" + (f"?{query}" if query else "")
    return request


@pytest.mark.asyncio
async def test_callback_route_fails_closed_and_never_provisions(callback_service):
    """THE critical guarantee: with LinkAja's public key unset, an inbound
    notify cannot reach the order or BundleService - even a well-formed, signed
    one. The answer is a SNAP "retry later" error, not an acknowledgement."""
    service, m = callback_service
    body = _json.dumps(_SPEC_NOTIFY_BODY).encode()
    request = _notify_request(body, _signed_notify_headers(body))

    response = await service.handle_callback(request)

    assert response.status_code == 503
    assert _json.loads(response.body) == {"responseCode": "5005601",
                                          "responseMessage": "Internal Server Error"}
    m["bundle_service"].buy_bundle.assert_not_called()
    m["bundle_service"].top_up_bundle.assert_not_called()
    m["order_repo"].update_by.assert_not_called()
    m["order_repo"].get_by_id.assert_not_called()
    assert m["executor"].tasks == []


@pytest.mark.asyncio
async def test_fully_configured_callback_rejects_a_bad_signature_with_4015600(notify_service):
    """Public key present, signature wrong: 401 / 4015600 and nothing touched."""
    service, m = notify_service
    body = _json.dumps(_SPEC_NOTIFY_BODY).encode()
    headers = _signed_notify_headers(body)
    headers["X-SIGNATURE"] = _b64.b64encode(b"x" * 256).decode()
    request = _notify_request(body, headers)

    response = await service.handle_callback(request)

    assert response.status_code == 401
    assert _json.loads(response.body)["responseCode"] == "4015600"
    m["bundle_service"].buy_bundle.assert_not_called()
    m["order_repo"].get_by_id.assert_not_called()
    assert m["executor"].tasks == []


@pytest.mark.asyncio
async def test_callback_with_disabled_flag_is_rejected(callback_service):
    from app.services.link_aja_callback_service import LinkAjaCallbackService

    _, m = callback_service
    service = LinkAjaCallbackService(link_aja_service=LinkAjaService(config=LinkAjaConfig(enabled=False)),
                                     user_order_repo=m["order_repo"],
                                     bundle_service=m["bundle_service"], task_executor=m["executor"])
    body = _json.dumps(_SPEC_NOTIFY_BODY).encode()
    response = await service.handle_callback(_notify_request(body, _signed_notify_headers(body)))

    assert response.status_code == 400
    assert _json.loads(response.body)["responseCode"] == "4005600"
    m["bundle_service"].buy_bundle.assert_not_called()


@pytest.mark.asyncio
async def test_unverified_result_is_refused_even_internally(callback_service):
    service, m = callback_service
    unverified = LinkAjaCallbackResult(merchant_order_id="order-1",
                                       status=LinkAjaPaymentStatus.SUCCESS, verified=False)

    with pytest.raises(LinkAjaCallbackVerificationException):
        await service.process_verified_callback(unverified)

    m["bundle_service"].buy_bundle.assert_not_called()


@pytest.mark.asyncio
async def test_verified_success_schedules_provisioning_through_bundle_service(callback_service):
    """Provisioning is queued, not awaited: the ack must go out first (p.7)."""
    service, m = callback_service

    ack = await service.process_verified_callback(_verified())

    assert ack == LinkAjaNotifyAck(approval_code="D60232EF01", provisioning_scheduled=True)
    m["bundle_service"].buy_bundle.assert_not_awaited()      # not yet...
    assert len(m["executor"].tasks) == 1

    m["executor"].run_all()                                   # ...the background task does it
    m["bundle_service"].buy_bundle.assert_awaited_once()
    kwargs = m["bundle_service"].buy_bundle.await_args.kwargs
    assert kwargs["payment_type"] == PaymentTypeEnum.LINK_AJA
    assert kwargs["payment_status"] == OrderStatusEnum.SUCCESS
    assert kwargs["user_order"].id == "order-1"
    assert kwargs["user_order"].payment_status == OrderStatusEnum.SUCCESS


@pytest.mark.asyncio
async def test_provisioning_runs_after_the_response_is_sent(notify_service):
    """End to end through `handle_callback`: when the 2005600 response object
    exists, BundleService has not been called; it is called by the queued task."""
    service, m = notify_service
    body = _json.dumps(_SPEC_NOTIFY_BODY).encode()

    response = await service.handle_callback(_notify_request(body, _signed_notify_headers(body)))

    assert response.status_code == 200
    assert _json.loads(response.body) == {"responseCode": "2005600", "responseMessage": "Successful",
                                          "approvalCode": "D60232EF01"}
    m["bundle_service"].buy_bundle.assert_not_awaited()
    assert len(m["executor"].tasks) == 1
    # The claim (pending -> success) happened before the response, so a retry
    # arriving in the meantime cannot provision again.
    claim = m["order_repo"].update_by.call_args
    assert claim.kwargs["where"] == {"id": "order-1", "payment_status": OrderStatusEnum.PENDING}

    m["executor"].run_all()
    m["bundle_service"].buy_bundle.assert_awaited_once()


@pytest.mark.asyncio
async def test_full_executor_queue_still_provisions_on_a_dedicated_thread(callback_service):
    """Back-pressure must never strand a paid order: if the queue refuses the
    task, it runs on its own thread instead."""
    import threading

    service, m = callback_service
    m["executor"].accept = False
    done = threading.Event()

    async def buy_bundle(**kwargs):
        done.set()
        return "provisioned"
    m["bundle_service"].buy_bundle = AsyncMock(side_effect=buy_bundle)

    ack = await service.process_verified_callback(_verified())

    assert ack.provisioning_scheduled is True
    assert done.wait(timeout=5), "provisioning never ran"
    assert m["executor"].tasks == []


@pytest.mark.asyncio
async def test_verified_success_claims_the_order_atomically(callback_service):
    """The duplicate guard is a conditional UPDATE, not a read-then-write."""
    service, m = callback_service

    await service.process_verified_callback(_verified())

    claim = m["order_repo"].update_by.call_args
    assert claim.kwargs["where"] == {"id": "order-1", "payment_status": OrderStatusEnum.PENDING}
    assert claim.kwargs["data"]["payment_status"] == OrderStatusEnum.SUCCESS


@pytest.mark.asyncio
async def test_duplicate_callback_does_not_provision_twice(callback_service):
    """Second delivery of the same success notify: the order is already
    successful, so provisioning is skipped and the notify is acknowledged with
    the same 2005600 so LinkAja stops retrying."""
    service, m = callback_service
    m["order"].payment_status = OrderStatusEnum.SUCCESS

    ack = await service.process_verified_callback(_verified())

    assert ack == LinkAjaNotifyAck(approval_code="D60232EF01", provisioning_scheduled=False)
    m["bundle_service"].buy_bundle.assert_not_called()
    m["order_repo"].update_by.assert_not_called()
    assert m["executor"].tasks == []


@pytest.mark.asyncio
async def test_two_deliveries_of_one_notify_provision_exactly_once(notify_service):
    """Through the route, twice: the first claims and schedules, the second
    finds the row already claimed (the UPDATE matches nothing) and only acks."""
    service, m = notify_service
    body = _json.dumps(_SPEC_NOTIFY_BODY).encode()
    headers = _signed_notify_headers(body)

    first = await service.handle_callback(_notify_request(body, headers))
    m["order_repo"].update_by.return_value = []            # row is no longer pending
    second = await service.handle_callback(_notify_request(body, headers))

    assert first.status_code == second.status_code == 200
    assert _json.loads(first.body) == _json.loads(second.body)
    assert len(m["executor"].tasks) == 1
    m["executor"].run_all()
    m["bundle_service"].buy_bundle.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_duplicate_callback_loses_the_claim_and_does_not_provision(callback_service):
    """Two simultaneous success callbacks: the one whose conditional UPDATE
    matches no row must not provision."""
    service, m = callback_service
    m["order_repo"].update_by.return_value = []  # lost the race

    ack = await service.process_verified_callback(_verified())

    assert ack.provisioning_scheduled is False
    m["bundle_service"].buy_bundle.assert_not_called()
    assert m["executor"].tasks == []


@pytest.mark.parametrize("status", [
    LinkAjaPaymentStatus.FAILED,
    LinkAjaPaymentStatus.CANCELED,
    LinkAjaPaymentStatus.EXPIRED,
])
@pytest.mark.asyncio
async def test_failed_callback_records_failure_and_does_not_provision(callback_service, status):
    service, m = callback_service

    ack = await service.process_verified_callback(_verified(status=status))

    assert ack.provisioning_scheduled is False  # acknowledged, not provisioned
    m["bundle_service"].buy_bundle.assert_not_called()
    assert m["executor"].tasks == []

    recorded = m["order_repo"].update_by.call_args.kwargs
    assert recorded["where"] == {"id": "order-1"}
    assert recorded["data"]["payment_status"] == OrderStatusEnum.FAILURE
    assert recorded["data"]["order_status"] == OrderStatusEnum.FAILURE
    assert recorded["data"]["callback_time"]


@pytest.mark.asyncio
async def test_pending_callback_leaves_order_pending_and_does_not_provision(callback_service):
    service, m = callback_service

    await service.process_verified_callback(_verified(status=LinkAjaPaymentStatus.PENDING))

    m["bundle_service"].buy_bundle.assert_not_called()
    m["order_repo"].update_by.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_status_never_provisions_and_leaves_order_pending(callback_service):
    """An unmapped provider status means our status map is incomplete - it must
    never be optimistically treated as success."""
    service, m = callback_service

    await service.process_verified_callback(_verified(status=LinkAjaPaymentStatus.UNKNOWN))

    m["bundle_service"].buy_bundle.assert_not_called()
    m["order_repo"].update_by.assert_not_called()


@pytest.mark.parametrize("status", [LinkAjaPaymentStatus.NOT_FOUND, LinkAjaPaymentStatus.REFUNDED])
@pytest.mark.asyncio
async def test_not_found_and_refunded_leave_the_order_pending_and_never_provision(callback_service, status):
    """§5.4 "07 Not Found" is "not paid yet", and "04 Refunded" on a pending
    order changes nothing: neither may fail the order or provision."""
    service, m = callback_service

    ack = await service.process_verified_callback(_verified(status=status))

    assert ack.provisioning_scheduled is False
    m["bundle_service"].buy_bundle.assert_not_called()
    m["order_repo"].update_by.assert_not_called()
    assert m["order"].payment_status == OrderStatusEnum.PENDING


@pytest.mark.parametrize("notified", [Decimal("175840"), Decimal("175840.00"), Decimal("175840.000")])
@pytest.mark.asyncio
async def test_amount_matches_with_or_without_decimals(callback_service, notified):
    service, m = callback_service
    ack = await service.process_verified_callback(_verified(amount=notified))
    assert ack.provisioning_scheduled is True


@pytest.mark.parametrize("notified", [Decimal("175839.00"), Decimal("1.00"), Decimal("17584000")])
@pytest.mark.asyncio
async def test_amount_mismatch_is_rejected_before_the_claim(callback_service, notified):
    """A notify for the wrong amount must not settle the order (4045613 Invalid Amount)."""
    service, m = callback_service

    with pytest.raises(LinkAjaCallbackInvalidException) as exc:
        await service.process_verified_callback(_verified(amount=notified))

    assert exc.value.snap_response_code == "4045613"
    m["order_repo"].update_by.assert_not_called()
    m["bundle_service"].buy_bundle.assert_not_called()
    assert m["executor"].tasks == []


@pytest.mark.asyncio
async def test_success_without_an_amount_cannot_be_reconciled(callback_service):
    service, m = callback_service
    with pytest.raises(LinkAjaCallbackInvalidException):
        await service.process_verified_callback(_verified(amount=None))
    m["order_repo"].update_by.assert_not_called()


@pytest.mark.asyncio
async def test_notify_still_succeeds_when_the_rate_changed_after_creation():
    """The whole point of storing the quote: the order was created at 16000
    (175840.00 IDR); by notify time the deployment runs at 20000. The notify
    carries 175840 and must be accepted - the current rate is irrelevant."""
    service, m = _make_callback_service(config=_full_config(usd_to_idr_rate="20000"),
                                        quote=_stored_quote(amount="175840.00", rate="16000"))

    ack = await service.process_verified_callback(_verified(amount=Decimal("175840")))

    assert ack.provisioning_scheduled is True
    assert len(m["executor"].tasks) == 1


@pytest.mark.asyncio
async def test_notify_at_the_new_rate_is_rejected_when_the_order_was_quoted_at_the_old_one():
    """Mirror image: an amount computed with the *current* rate (219800 at
    20000) does not match what was actually requested and must be rejected."""
    service, m = _make_callback_service(config=_full_config(usd_to_idr_rate="20000"),
                                        quote=_stored_quote(amount="175840.00", rate="16000"))
    with pytest.raises(LinkAjaCallbackInvalidException) as exc:
        await service.process_verified_callback(_verified(amount=Decimal("219800.00")))
    assert exc.value.snap_response_code == "4045613"
    m["order_repo"].update_by.assert_not_called()


@pytest.mark.asyncio
async def test_reconciliation_never_consults_the_current_rate(callback_service):
    """Even with the rate unset (which fails payment creation closed), a notify
    for an order with a stored quote is reconciled fine."""
    service, m = _make_callback_service(config=_full_config(usd_to_idr_rate=""))
    ack = await service.process_verified_callback(_verified())
    assert ack.provisioning_scheduled is True


@pytest.mark.asyncio
async def test_order_without_a_stored_quote_cannot_be_reconciled():
    """An order that predates quote storage (or lost it) fails closed: rejected,
    never provisioned - the current rate is not used as a fallback."""
    service, m = _make_callback_service(quote=None)
    with pytest.raises(LinkAjaCallbackInvalidException) as exc:
        await service.process_verified_callback(_verified())
    assert exc.value.snap_response_code == "4045613"
    m["order_repo"].update_by.assert_not_called()
    m["bundle_service"].buy_bundle.assert_not_called()


@pytest.mark.asyncio
async def test_notify_in_another_currency_than_the_quote_is_rejected(callback_service):
    service, m = callback_service
    result = _verified().model_copy(update={"currency": "USD"})
    with pytest.raises(LinkAjaCallbackInvalidException):
        await service.process_verified_callback(result)


@pytest.mark.asyncio
async def test_rate_change_end_to_end_from_assign_to_notify(service_and_mocks):
    """Create the payment at rate 16000 through UserBundleService, take the
    bundle_data it persisted, then deliver the notify to a callback service
    running at rate 20000 - it provisions."""
    user_service, um = service_and_mocks
    create_payment = AsyncMock(return_value=LinkAjaPaymentResult(
        provider_reference="LA-TXN-9", payment_url="https://linkaja.invalid/pay/9",
        status=LinkAjaPaymentStatus.PENDING, amount="175840.00", usd_to_idr_rate="16000"))
    _install_link_aja(user_service, _full_config(usd_to_idr_rate="16000"), create_payment=create_payment)
    await _assign(user_service)
    persisted_bundle_data = um["order_repo"].update_by.call_args_list[0].kwargs["data"]["bundle_data"]

    service, m = _make_callback_service(config=_notify_config(usd_to_idr_rate="20000"), quote=None)
    m["order"].bundle_data = persisted_bundle_data
    m["order"].payment_intent_code = "LA-TXN-9"
    body = _json.dumps({**_SPEC_NOTIFY_BODY, "amount": {"currency": "IDR", "value": "175840.00"}}).encode()

    response = await service.handle_callback(_notify_request(body, _signed_notify_headers(body)))

    assert response.status_code == 200
    assert _json.loads(response.body)["responseCode"] == NOTIFY_SUCCESS_CODE
    assert len(m["executor"].tasks) == 1


@pytest.mark.asyncio
async def test_amount_sent_must_match_the_stored_quote(service_and_mocks):
    """Defensive guard in the payment handler: if the provider call somehow
    sent a different amount than the stored quote, the flow fails rather than
    leaving an unreconcilable order."""
    user_service, um = service_and_mocks
    create_payment = AsyncMock(return_value=LinkAjaPaymentResult(
        provider_reference="LA-TXN-9", payment_url="https://linkaja.invalid/pay/9",
        status=LinkAjaPaymentStatus.PENDING, amount="999.00", usd_to_idr_rate="16000"))
    _install_link_aja(user_service, _full_config(), create_payment=create_payment)
    with pytest.raises(CustomException):
        await _assign(user_service)
    for call in um["order_repo"].update_by.call_args_list:
        assert "payment_intent_code" not in call.kwargs["data"]


@pytest.mark.asyncio
async def test_quote_is_stored_before_the_provider_is_called(service_and_mocks):
    """A storage failure costs no LinkAja call (and leaves no dangling payment)."""
    user_service, um = service_and_mocks
    um["order_repo"].update_by.side_effect = RuntimeError("db down")
    create_payment = AsyncMock()
    _install_link_aja(user_service, _full_config(), create_payment=create_payment)
    with pytest.raises(RuntimeError):
        await _assign(user_service)
    create_payment.assert_not_awaited()


@pytest.mark.asyncio
async def test_order_whose_bundle_data_cannot_carry_the_quote_fails_closed(service_and_mocks):
    user_service, um = service_and_mocks
    um["order"].bundle_data = "-"
    create_payment = AsyncMock()
    _install_link_aja(user_service, _full_config(), create_payment=create_payment)
    with pytest.raises(CustomException) as exc:
        await _assign(user_service)
    assert exc.value.name == ErrorMessages.REQUEST_FAILED
    create_payment.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_for_unknown_order_is_rejected(callback_service):
    service, m = callback_service
    m["order_repo"].get_by_id.return_value = None

    with pytest.raises(LinkAjaOrderNotFoundException):
        await service.process_verified_callback(_verified())
    m["bundle_service"].buy_bundle.assert_not_called()


@pytest.mark.asyncio
async def test_callback_cannot_settle_an_order_paid_with_another_provider(callback_service):
    """A LinkAja callback must never be able to settle a Stripe/Card order."""
    service, m = callback_service
    m["order"].payment_type = PaymentTypeEnum.CARD

    with pytest.raises(LinkAjaOrderNotFoundException):
        await service.process_verified_callback(_verified())
    m["bundle_service"].buy_bundle.assert_not_called()


@pytest.mark.asyncio
async def test_callback_with_mismatched_provider_reference_is_rejected(callback_service):
    service, m = callback_service
    m["order"].payment_intent_code = "LA-TXN-ORIGINAL"

    with pytest.raises(LinkAjaOrderNotFoundException):
        await service.process_verified_callback(_verified(reference="LA-TXN-DIFFERENT"))
    m["bundle_service"].buy_bundle.assert_not_called()


@pytest.mark.asyncio
async def test_link_aja_top_up_callback_refuses_to_provision(callback_service):
    """Top-ups are unsupported; provisioning must fail loudly rather than
    provision a top-up it cannot address (no iccid carrier is specified)."""
    service, m = callback_service
    m["order"].order_type = UserOrderType.BUNDLE_TOP_UP

    with pytest.raises(CustomException) as exc:
        await service.provision(m["order"])
    assert exc.value.name == ErrorMessages.LINKAJA_NOT_CONFIGURED
    m["bundle_service"].top_up_bundle.assert_not_called()


# ---------------------------------------------------------------------------
# 10. Route separation from Stripe
# ---------------------------------------------------------------------------

def test_link_aja_callback_route_exists_and_is_separate_from_stripe():
    """Read the route module as source rather than importing it: importing it
    constructs the Stripe `CallbackService` at module scope, which needs a live
    Supabase connection. The assertion is about routing wiring, not runtime."""
    from pathlib import Path

    source = Path("app/api/v1/callback.py").read_text()

    # LinkAja has its own route, plus the alias at the spec's path (§4.3)...
    assert '@router.post("/linkaja")' in source
    assert '@link_aja_notify_router.post("/bi/applink/v1.0/debit/notify")' in source
    # ...and the Stripe webhook keeps its own, still wired to the Stripe service.
    assert '@router.post("/payment-webhook")' in source
    assert "await service.handle_payment_webhook(request)" in source
    # LinkAja callbacks must not be routed through the Stripe handler.
    assert "service.handle_payment_webhook(request)" in source
    assert source.count("get_link_aja_callback_service().handle_callback(request)") == 2
    # The alias is mounted at the application root, unprefixed, in main.py.
    main_source = Path("app/main.py").read_text()
    assert "esim_app.include_router(link_aja_notify_router" in main_source
    assert 'prefix=api_version, router=router' in main_source


def _route_module():
    """Load app/api/v1/callback.py on its own.

    Importing it normally executes `app/api/v1/__init__.py`, which builds every
    service (and needs a live database). Loading the file directly, with the
    Stripe service stubbed, gives the real routers with no network.
    """
    with patch("app.services.callback_service.CallbackService"):
        spec = importlib.util.spec_from_file_location("linkaja_callback_routes", "app/api/v1/callback.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


@pytest.fixture
def notify_app():
    """A FastAPI app wired exactly like main.py for the two LinkAja routes,
    backed by a fully configured notify service with mocked collaborators."""
    from fastapi import FastAPI

    routes = _route_module()
    service, m = _make_callback_service(config=_notify_config())
    routes.get_link_aja_callback_service = lambda: service

    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/callback")
    app.include_router(routes.link_aja_notify_router)
    return app, m


@pytest.mark.parametrize("path", [_NOTIFY_API_PATH, _NOTIFY_SPEC_PATH])
def test_both_notify_routes_accept_a_signed_notify_and_answer_2005600(notify_app, path):
    from fastapi.testclient import TestClient

    app, m = notify_app
    body = _json.dumps(_SPEC_NOTIFY_BODY).encode()
    headers = _signed_notify_headers(body, endpoint_url=path)

    response = TestClient(app).post(path, content=body, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"responseCode": "2005600", "responseMessage": "Successful",
                               "approvalCode": "D60232EF01"}
    assert list(response.json().keys()) == ["responseCode", "responseMessage", "approvalCode"]
    assert len(m["executor"].tasks) == 1


@pytest.mark.parametrize("path", [_NOTIFY_API_PATH, _NOTIFY_SPEC_PATH])
def test_both_notify_routes_sign_over_the_path_that_was_called(notify_app, path):
    """With LINKAJA_NOTIFY_ENDPOINT_URL unset, the EndpointUrl is the path LinkAja
    actually hit - so a signature made for the other path is rejected."""
    from fastapi.testclient import TestClient

    app, m = notify_app
    body = _json.dumps(_SPEC_NOTIFY_BODY).encode()
    other = _NOTIFY_SPEC_PATH if path == _NOTIFY_API_PATH else _NOTIFY_API_PATH

    response = TestClient(app).post(path, content=body, headers=_signed_notify_headers(body, endpoint_url=other))

    assert response.status_code == 401
    assert response.json() == {"responseCode": "4015600", "responseMessage": "Unauthorized. Invalid signature"}
    assert m["executor"].tasks == []


def test_notify_route_errors_use_snap_codes_not_the_api_envelope(notify_app):
    from fastapi.testclient import TestClient

    app, m = notify_app
    m["order_repo"].get_by_id.return_value = None
    body = _json.dumps(_SPEC_NOTIFY_BODY).encode()

    response = TestClient(app).post(_NOTIFY_SPEC_PATH, content=body, headers=_signed_notify_headers(body))

    assert response.status_code == 404
    assert response.json() == {"responseCode": "4045601", "responseMessage": "Transaction Not Found"}
    assert "status" not in response.json() and "developerMessage" not in response.json()


def test_spec_notify_alias_is_post_only(notify_app):
    """Only /api/v1/callback/linkaja answers the customer's GET return; the
    spec-path alias stays POST only."""
    from fastapi.testclient import TestClient

    app, _ = notify_app
    assert TestClient(app).get(_NOTIFY_SPEC_PATH).status_code == 405


def test_stripe_webhook_service_is_untouched_by_link_aja():
    """LinkAja must never be routed through the Stripe webhook."""
    import inspect

    from app.services import callback_service as stripe_callback_module

    source = inspect.getsource(stripe_callback_module.CallbackService)
    assert "PaymentTypeEnum.CARD" in source
    assert "LINK_AJA" not in source
    assert "link_aja" not in source.lower()


# ---------------------------------------------------------------------------
# 10b. One urlParams URL for the notify (POST) and the customer return (GET)
# ---------------------------------------------------------------------------

_ORDER_UUID = "3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b"
_FRONTEND_TEMPLATE = "https://esim-shop.invalid/plans?order_id={order_id}"


def _notify_app_with(config):
    """Like `notify_app`, but with a given LinkAja config. The service getter
    is a MagicMock so tests can assert the GET never even builds it."""
    from fastapi import FastAPI

    routes = _route_module()
    service, m = _make_callback_service(config=config)
    routes.get_link_aja_callback_service = MagicMock(return_value=service)

    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/callback")
    app.include_router(routes.link_aja_notify_router)
    return app, m, routes.get_link_aja_callback_service


def _return_get(app, params=None, env=_FRONTEND_TEMPLATE):
    from fastapi.testclient import TestClient

    with patch.dict(os.environ):
        if env is None:
            os.environ.pop("LINKAJA_FRONTEND_RETURN_URL", None)
        else:
            os.environ["LINKAJA_FRONTEND_RETURN_URL"] = env
        return TestClient(app).get(_NOTIFY_API_PATH, params=params, follow_redirects=False)


def test_notify_url_for_order_appends_the_order_id():
    assert notify_url_for_order(_NOTIFY_URL, "order-1") == _NOTIFY_URL + "?order_id=order-1"
    assert notify_url_for_order("https://h.invalid/cb?x=1&order_id=old", _ORDER_UUID) == \
        f"https://h.invalid/cb?x=1&order_id={_ORDER_UUID}"
    assert notify_url_for_order("", "order-1") == ""


def test_create_payment_url_params_carry_the_order_id():
    service = LinkAjaService(config=_full_config())
    body = service._build_create_payment_body(_payment_request(order_id=_ORDER_UUID))
    assert body["urlParams"][0]["url"] == f"{_NOTIFY_URL}?order_id={_ORDER_UUID}"


def test_frontend_return_url_is_read_from_the_environment():
    with patch.dict(os.environ, {"LINKAJA_FRONTEND_RETURN_URL": _FRONTEND_TEMPLATE,
                                 "LINKAJA_NOTIFY_SIGNATURE_FORMS": "path,full_url"}):
        config = LinkAjaConfig.from_env()
    assert config.frontend_return_url == _FRONTEND_TEMPLATE
    assert config.notify_signature_forms == "path,full_url"


def test_signature_forms_default_to_the_path_only():
    with patch.dict(os.environ, {}, clear=True):
        assert LinkAjaConfig.from_env().signature_forms() == ["path"]


@pytest.mark.parametrize("forms", ["bogus", "path,full", " , "])
def test_unknown_signature_forms_fail_closed(forms):
    with pytest.raises(LinkAjaConfigurationException) as exc:
        _notify_config(notify_signature_forms=forms).validate_for_callback()
    assert "LINKAJA_NOTIFY_SIGNATURE_FORMS" in exc.value.details


@pytest.mark.parametrize("template, order_id, expected", [
    (_FRONTEND_TEMPLATE, _ORDER_UUID, f"https://esim-shop.invalid/plans?order_id={_ORDER_UUID}"),
    (_FRONTEND_TEMPLATE, _ORDER_UUID.upper(), f"https://esim-shop.invalid/plans?order_id={_ORDER_UUID}"),
    (_FRONTEND_TEMPLATE, None, "https://esim-shop.invalid/plans"),
    (_FRONTEND_TEMPLATE, "order-1", "https://esim-shop.invalid/plans"),
    (_FRONTEND_TEMPLATE, "https://evil.invalid", "https://esim-shop.invalid/plans"),
    ("https://esim-shop.invalid/plans?lang=id&order_id={order_id}", "bad", "https://esim-shop.invalid/plans?lang=id"),
    ("https://esim-shop.invalid/orders/{order_id}", _ORDER_UUID, f"https://esim-shop.invalid/orders/{_ORDER_UUID}"),
    ("https://esim-shop.invalid/plans", _ORDER_UUID, "https://esim-shop.invalid/plans"),
    ("", _ORDER_UUID, None),
    ("javascript:alert(1)", _ORDER_UUID, None),
    ("/plans?order_id={order_id}", _ORDER_UUID, None),
])
def test_frontend_return_url_building(template, order_id, expected):
    assert frontend_return_url(template, order_id) == expected


def test_get_return_redirects_to_the_frontend_with_the_order_id():
    app, _, _ = _notify_app_with(_notify_config())
    response = _return_get(app, params={"order_id": _ORDER_UUID})
    assert response.status_code == 302
    assert response.headers["location"] == f"https://esim-shop.invalid/plans?order_id={_ORDER_UUID}"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("params", [None, {"order_id": ""}, {"order_id": "order-1"},
                                    {"order_id": "https://evil.invalid/x"}, {"order_id": "1' OR '1'='1"}])
def test_get_return_without_a_valid_order_id_redirects_without_it(params):
    app, _, _ = _notify_app_with(_notify_config())
    response = _return_get(app, params=params)
    assert response.status_code == 302
    assert response.headers["location"] == "https://esim-shop.invalid/plans"


def test_get_return_without_a_configured_frontend_url_does_not_redirect():
    app, _, _ = _notify_app_with(_notify_config())
    response = _return_get(app, params={"order_id": _ORDER_UUID}, env=None)
    assert response.status_code == 404
    assert "location" not in response.headers


def test_get_return_never_processes_or_provisions():
    """The GET is not authenticated, so it must not touch orders at all: it
    never builds the callback service, reads or updates an order, or schedules
    provisioning - not even for a pending order with a valid id."""
    app, m, get_service = _notify_app_with(_notify_config())
    for params in ({"order_id": "order-1"}, {"order_id": _ORDER_UUID}, None):
        assert _return_get(app, params=params).status_code == 302
    get_service.assert_not_called()
    m["order_repo"].get_by_id.assert_not_called()
    m["order_repo"].update_by.assert_not_called()
    m["order_repo"].list.assert_not_called()
    m["bundle_service"].buy_bundle.assert_not_called()
    assert m["executor"].tasks == []
    assert m["order"].payment_status == OrderStatusEnum.PENDING


def test_get_return_works_while_link_aja_is_disabled():
    """No processing means nothing to gate: the redirect only needs its URL."""
    app, _, get_service = _notify_app_with(_full_config(enabled=False))
    with patch.dict(os.environ, {"LINKAJA_ENABLED": "false"}):
        response = _return_get(app, params={"order_id": _ORDER_UUID})
    assert response.status_code == 302
    get_service.assert_not_called()


def test_get_return_is_not_in_the_openapi_schema():
    app, _, _ = _notify_app_with(_notify_config())
    operations = app.openapi()["paths"][_NOTIFY_API_PATH]
    assert set(operations) == {"post"}


def test_post_notify_with_the_query_string_still_works():
    """The POST arrives on the urlParams URL, query string included. With the
    default form the signature covers the path only."""
    from fastapi.testclient import TestClient

    app, m, _ = _notify_app_with(_notify_config())
    body = _json.dumps(_SPEC_NOTIFY_BODY).encode()

    response = TestClient(app).post(f"{_NOTIFY_API_PATH}?order_id=order-1", content=body,
                                    headers=_signed_notify_headers(body, endpoint_url=_NOTIFY_API_PATH))

    assert response.status_code == 200
    assert response.json()["responseCode"] == "2005600"
    assert len(m["executor"].tasks) == 1


def test_post_notify_takes_the_order_from_the_body_not_the_query():
    from fastapi.testclient import TestClient

    app, m, _ = _notify_app_with(_notify_config())
    body = _json.dumps(_SPEC_NOTIFY_BODY).encode()

    response = TestClient(app).post(f"{_NOTIFY_API_PATH}?order_id={_ORDER_UUID}", content=body,
                                    headers=_signed_notify_headers(body, endpoint_url=_NOTIFY_API_PATH))

    assert response.status_code == 200
    m["order_repo"].get_by_id.assert_called_once()
    assert "order-1" in m["order_repo"].get_by_id.call_args.args + tuple(
        m["order_repo"].get_by_id.call_args.kwargs.values())
    assert _ORDER_UUID not in str(m["order_repo"].get_by_id.call_args)


_QUERY = "order_id=order-1"


@pytest.mark.parametrize("forms, signed_over, ok", [
    ("path", _NOTIFY_API_PATH, True),
    ("path", f"{_NOTIFY_API_PATH}?{_QUERY}", False),
    ("path_query", f"{_NOTIFY_API_PATH}?{_QUERY}", True),
    ("path_query", _NOTIFY_API_PATH, False),
    ("full_url", f"https://esim.invalid{_NOTIFY_API_PATH}?{_QUERY}", True),
    ("full_url", f"http://testserver{_NOTIFY_API_PATH}?{_QUERY}", False),
    ("path,path_query,full_url", _NOTIFY_API_PATH, True),
    ("path,path_query,full_url", f"{_NOTIFY_API_PATH}?{_QUERY}", True),
    ("path,path_query,full_url", f"https://esim.invalid{_NOTIFY_API_PATH}?{_QUERY}", True),
    ("path,path_query,full_url", "/somewhere/else", False),
])
def test_notify_endpoint_url_forms(forms, signed_over, ok):
    """`full_url` takes scheme and host from LINKAJA_NOTIFY_URL (here
    https://esim.invalid), not from the request as seen behind a proxy."""
    from fastapi.testclient import TestClient

    app, m, _ = _notify_app_with(_notify_config(notify_signature_forms=forms))
    body = _json.dumps(_SPEC_NOTIFY_BODY).encode()

    response = TestClient(app).post(f"{_NOTIFY_API_PATH}?{_QUERY}", content=body,
                                    headers=_signed_notify_headers(body, endpoint_url=signed_over))

    assert response.status_code == (200 if ok else 401)
    assert len(m["executor"].tasks) == (1 if ok else 0)


@pytest.mark.parametrize("signed_over, form", [
    (_NOTIFY_API_PATH, "path"),
    (f"{_NOTIFY_API_PATH}?{_QUERY}", "path_query"),
    (f"https://esim.invalid{_NOTIFY_API_PATH}?{_QUERY}", "full_url"),
])
def test_the_verified_form_is_logged_without_the_signature(signed_over, form):
    from loguru import logger

    service = LinkAjaService(config=_notify_config(notify_signature_forms="path,path_query,full_url"))
    body = _spec_notify_bytes()
    headers = _signed_notify_headers(body, endpoint_url=signed_over)
    captured = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="DEBUG")
    try:
        assert service.verify_callback(raw_body=body, headers=headers, request_path=_NOTIFY_API_PATH,
                                       query_string=_QUERY).verified
    finally:
        logger.remove(sink_id)
    assert any(f"EndpointUrl form={form}" in line for line in captured)
    assert not any(headers["X-SIGNATURE"] in line for line in captured)


def test_pinned_endpoint_url_wins_over_the_forms():
    pinned = f"https://esim.invalid{_NOTIFY_API_PATH}"
    service = LinkAjaService(config=_notify_config(notify_endpoint_url=pinned,
                                                   notify_signature_forms="path,path_query"))
    body = _spec_notify_bytes()
    assert service.verify_callback(raw_body=body, headers=_signed_notify_headers(body, endpoint_url=pinned),
                                   request_path=_NOTIFY_API_PATH, query_string=_QUERY).verified
    with pytest.raises(LinkAjaCallbackVerificationException):
        service.verify_callback(raw_body=body, headers=_signed_notify_headers(body, endpoint_url=_NOTIFY_API_PATH),
                                request_path=_NOTIFY_API_PATH, query_string=_QUERY)


# ---------------------------------------------------------------------------
# 11. Response contract backward compatibility
# ---------------------------------------------------------------------------

CARD_RESPONSE_FIELDS = {
    "publishable_key", "merchant_identifier", "billing_country_code",
    "payment_intent_client_secret", "customer_id", "customer_ephemeral_key_secret",
    "test_env", "merchant_display_name", "stripe_url_scheme", "order_id",
    "payment_status", "total_price_display", "tax_price_display",
    "subtotal_price_display", "has_tax", "otp_expiration",
}


def test_existing_response_fields_are_all_still_present():
    dumped = PaymentIntentResponse(order_id="order-1").model_dump()
    assert CARD_RESPONSE_FIELDS.issubset(dumped.keys())


def test_card_wallet_dcb_responses_do_not_gain_a_payment_url_key():
    """The serialized contract for existing payment types is unchanged: no new
    key appears at all, so strict clients keep deserializing."""
    card = PaymentIntentResponse(order_id="order-1", payment_intent_client_secret="cs_1")
    wallet = PaymentIntentResponse(order_id="order-1", payment_status=PaymentStatusEnum.COMPLETED)
    dcb = PaymentIntentResponse(order_id="order-1",
                                payment_status=PaymentStatusEnum.PENDING_VERIFICATION,
                                otp_expiration=300)

    for response in (card, wallet, dcb):
        assert "payment_url" not in response.model_dump()
        assert "payment_url" not in response.model_dump(mode="json")

    # Existing values and existing nulls are untouched.
    assert card.payment_intent_client_secret == "cs_1"
    assert card.model_dump()["otp_expiration"] is None
    assert wallet.payment_status == PaymentStatusEnum.COMPLETED
    assert dcb.otp_expiration == 300
    assert dcb.stripe_url_scheme == "stripe"


def test_link_aja_response_includes_the_payment_url():
    response = PaymentIntentResponse(order_id="order-1", payment_url="https://linkaja.invalid/pay/1")
    assert response.model_dump()["payment_url"] == "https://linkaja.invalid/pay/1"
    assert response.model_dump(mode="json")["payment_url"] == "https://linkaja.invalid/pay/1"


def test_response_serialization_survives_fastapi_response_model_filtering():
    """FastAPI serializes through the declared response_model, so the conditional
    field must survive that path - not just a direct model_dump()."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.schemas.response import Response, ResponseHelper

    app = FastAPI()

    @app.get("/card", response_model=Response[PaymentIntentResponse])
    def card_endpoint():
        return ResponseHelper.success_data_response(PaymentIntentResponse(order_id="o1"), 0)

    @app.get("/linkaja", response_model=Response[PaymentIntentResponse])
    def link_aja_endpoint():
        return ResponseHelper.success_data_response(
            PaymentIntentResponse(order_id="o2", payment_url="https://linkaja.invalid/pay"), 0)

    client = TestClient(app)
    card_body = client.get("/card").json()["data"]
    link_aja_body = client.get("/linkaja").json()["data"]

    assert "payment_url" not in card_body
    assert "otp_expiration" in card_body  # existing nulls still present
    assert link_aja_body["payment_url"] == "https://linkaja.invalid/pay"


def test_response_still_forbids_unknown_fields():
    with pytest.raises(Exception):
        PaymentIntentResponse(order_id="order-1", not_a_real_field="x")


# ---------------------------------------------------------------------------
# 12. eSIM Hub contract (unverified external dependency)
# ---------------------------------------------------------------------------

def test_esim_hub_receives_payment_type_verbatim_as_payment_method():
    """BundleService forwards payment_type straight through to the hub's
    `PaymentMethod` field, so LinkAja would be sent as the literal "LinkAja".
    Whether the hub accepts that value is an unverified external dependency
    that the eSIM Hub team must confirm."""
    import inspect

    from app.services.integration import esim_hub_service

    source = inspect.getsource(esim_hub_service.EsimHubService.create_reseller_order)
    assert '"PaymentMethod": payment_type' in source


# ---------------------------------------------------------------------------
# 13. SNAP signatures (LinkAja Postman collection pre-request scripts)
# ---------------------------------------------------------------------------

_TS = "2026-09-28T18:12:23+07:00"
_STATUS_PATH = "/bi/applink/v1.0/debit/status"
_STATUS_BODY = (b'{"originalPartnerReferenceNo":"order-1","serviceCode":"54","merchantId":"MERCHANT-TEST",'
                b'"additionalInfo":{"terminalId":"MERCHANT-TEST"}}')
# Computed independently with the openssl CLI:
#   sha256 -hex of the body, then `openssl dgst -sha512 -hmac test-secret -binary | base64`
#   over "POST:<path>:test-token:<hash>:<ts>". The docs ship no sample signature.
_STATUS_BODY_SHA256 = "50dde9df8e101a4c52b6acd727ba0fd17892b15795a3e9f0cdb9c996d8dcc9e9"
_EXPECTED_HMAC = ("7fxRCUhsnto60illSYjDpDRLdx+QBNrJmAQhuUFG6xade7/pAKeRWU+PpBXvqcoxsFGo77kgrSHdwUnnqiiNIA==")


def _verify_token_signature(public_key, client_key, timestamp, signature):
    public_key.verify(_b64.b64decode(signature), f"{client_key}|{timestamp}".encode(),
                      padding.PKCS1v15(), hashes.SHA256())


def test_service_string_to_sign_matches_the_postman_format():
    assert snap.service_string_to_sign("post", _STATUS_PATH, "test-token", _STATUS_BODY, _TS) == \
        f"POST:{_STATUS_PATH}:test-token:{_STATUS_BODY_SHA256}:{_TS}"


def test_service_signature_matches_independent_openssl_vector():
    assert snap.service_signature("test-secret", "POST", _STATUS_PATH, "test-token", _STATUS_BODY, _TS) \
        == _EXPECTED_HMAC


def test_service_signature_changes_when_body_is_tampered():
    tampered = _STATUS_BODY.replace(b"order-1", b"order-2")
    assert snap.service_signature("test-secret", "POST", _STATUS_PATH, "test-token", tampered, _TS) \
        != _EXPECTED_HMAC


def test_service_signature_changes_with_wrong_secret():
    assert snap.service_signature("wrong-secret", "POST", _STATUS_PATH, "test-token", _STATUS_BODY, _TS) \
        != _EXPECTED_HMAC


@pytest.mark.parametrize("field", ["path", "token", "timestamp"])
def test_service_signature_covers_path_token_and_timestamp(field):
    args = {"path": _STATUS_PATH, "token": "test-token", "timestamp": _TS}
    args[field] = args[field] + "x"
    assert snap.service_signature("test-secret", "POST", args["path"], args["token"], _STATUS_BODY,
                                  args["timestamp"]) != _EXPECTED_HMAC


def test_token_signature_verifies_with_the_matching_public_key():
    sig = snap.token_signature(_TEST_KEY, "cid", _TS)
    _verify_token_signature(_TEST_KEY.public_key(), "cid", _TS, sig)  # must not raise


def test_token_signature_fails_with_a_wrong_key():
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sig = snap.token_signature(other, "cid", _TS)
    with pytest.raises(InvalidSignature):
        _verify_token_signature(_TEST_KEY.public_key(), "cid", _TS, sig)


def test_token_signature_fails_when_signed_string_is_tampered():
    sig = snap.token_signature(_TEST_KEY, "cid", _TS)
    with pytest.raises(InvalidSignature):
        _verify_token_signature(_TEST_KEY.public_key(), "cid", "2026-09-28T18:12:24+07:00", sig)


def test_private_key_accepts_escaped_newlines_from_a_single_line_env_var():
    snap.load_private_key(_TEST_KEY_PEM.replace("\n", "\\n"))  # must not raise


def test_unparseable_private_key_is_a_configuration_error_without_key_material():
    with pytest.raises(LinkAjaConfigurationException) as exc:
        snap.load_private_key("-----BEGIN RSA PRIVATE KEY-----\nnot-a-key\n-----END RSA PRIVATE KEY-----")
    assert "not-a-key" not in exc.value.details


# --- §5.1.3 notify signature primitives ---------------------------------------

_NOTIFY_BODY = b'{"a":"x y","b":1}'


def test_notify_string_to_sign_has_no_access_token():
    """§5.1.3: METHOD:EndpointUrl:lowerhex(sha256(body)):timestamp - unlike the
    service signature there is no access token component."""
    expected_hash = "sha256"
    import hashlib
    expected_hash = hashlib.sha256(_NOTIFY_BODY).hexdigest()
    assert snap.notify_string_to_sign("post", _NOTIFY_SPEC_PATH, _NOTIFY_BODY, _TS) == \
        f"POST:{_NOTIFY_SPEC_PATH}:{expected_hash}:{_TS}"
    assert expected_hash == expected_hash.lower()


def test_notify_signature_round_trips_with_the_matching_public_key():
    sig = snap.notify_signature(_NOTIFY_KEY, "POST", _NOTIFY_SPEC_PATH, _NOTIFY_BODY, _TS)
    assert snap.verify_notify_signature(_NOTIFY_KEY.public_key(), sig, "POST", _NOTIFY_SPEC_PATH,
                                        _NOTIFY_BODY, _TS) is True


def test_notify_body_candidates_cover_the_spec_minify_ambiguity():
    """§5.1.5 strips "all whitespace"; its example only strips whitespace between
    tokens. Both readings, plus the raw bytes, are candidates - nothing else."""
    pretty = b'{\n  "a": "x y",\n  "b": 1\n}'
    candidates = snap.notify_body_candidates(pretty)
    assert candidates[0] == pretty
    assert b'{"a":"x y","b":1}' in candidates          # compact JSON, string intact
    assert b'{"a":"xy","b":1}' in candidates           # every whitespace char removed
    assert len(candidates) == 3
    # Already-compact input without inner spaces collapses to a single candidate.
    assert snap.notify_body_candidates(b'{"a":1}') == [b'{"a":1}']


@pytest.mark.parametrize("sent", [
    b'{\n  "a": "x y",\n  "b": 1\n}',    # LinkAja hashed the pretty body as sent
    b'{"a":"x y","b":1}',                 # LinkAja hashed compact JSON
    b'{"a":"xy","b":1}',                  # LinkAja stripped every whitespace char
])
def test_notify_verifies_whichever_canonical_form_linkaja_hashed(sent):
    pretty = b'{\n  "a": "x y",\n  "b": 1\n}'
    sig = snap.notify_signature(_NOTIFY_KEY, "POST", _NOTIFY_SPEC_PATH, sent, _TS)
    assert snap.verify_notify_signature(_NOTIFY_KEY.public_key(), sig, "POST", _NOTIFY_SPEC_PATH,
                                        pretty, _TS) is True


@pytest.mark.parametrize("bad", ["", "not base64!", "AAAA", _b64.b64encode(b"\x00" * 256).decode()])
def test_notify_malformed_signature_is_simply_invalid(bad):
    assert snap.verify_notify_signature(_NOTIFY_KEY.public_key(), bad, "POST", _NOTIFY_SPEC_PATH,
                                        _NOTIFY_BODY, _TS) is False


def test_public_key_accepts_escaped_newlines_and_rejects_garbage():
    snap.load_public_key(_NOTIFY_PUBLIC_PEM.replace("\n", "\\n"))  # must not raise
    with pytest.raises(LinkAjaConfigurationException) as exc:
        snap.load_public_key("-----BEGIN PUBLIC KEY-----\nnot-a-key\n-----END PUBLIC KEY-----")
    assert "not-a-key" not in exc.value.details


def test_timestamp_and_trx_date_formats():
    import re
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+07:00", snap.snap_timestamp())
    assert re.fullmatch(r"\d{14}", snap.trx_date())


def test_serialize_body_is_compact_and_stable():
    assert snap.serialize_body({"a": 1, "b": {"c": "é"}}) == '{"a":1,"b":{"c":"é"}}'.encode()


def test_external_ids_are_numeric_and_distinct():
    ids = {snap.external_id() for _ in range(200)}
    assert len(ids) == 200 and all(i.isdigit() for i in ids)


# ---------------------------------------------------------------------------
# 14. Currency conversion (business decision: LINKAJA_USD_TO_IDR_RATE)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("minor_units, rate, expected", [
    (1099, "16000", "175840.00"),
    (100, "15500.5", "15501.00"),     # 15500.5 rounds half-up to whole rupiah
    (18675, "160", "29880.00"),       # the Postman sample amount
    (1, "16000", "160.00"),
])
def test_usd_minor_units_convert_to_whole_rupiah_strings(minor_units, rate, expected):
    assert to_linkaja_amount(minor_units, "USD", rate) == expected


@pytest.mark.parametrize("rate", ["", "abc", "0", "-1", "NaN", "Infinity"])
def test_unset_or_invalid_rate_fails_closed(rate):
    with pytest.raises(LinkAjaConfigurationException) as exc:
        parse_usd_to_idr_rate(rate)
    assert "LINKAJA_USD_TO_IDR_RATE" in exc.value.details


def test_non_usd_order_currency_fails_closed():
    with pytest.raises(LinkAjaConfigurationException):
        to_linkaja_amount(1000, "EUR", "16000")


@pytest.mark.asyncio
async def test_create_payment_without_rate_makes_no_http_call():
    service = LinkAjaService(config=_full_config(usd_to_idr_rate="", **_DOC_PATHS))
    with patch("httpx.AsyncClient") as transport:
        with pytest.raises(LinkAjaConfigurationException):
            await service.create_payment(_payment_request())
    transport.assert_not_called()


# ---------------------------------------------------------------------------
# 15. Request / response mapping
# ---------------------------------------------------------------------------

# Body of "Create Applink Payment", spec v1.1 §4.2 (p.11-12): `urlParams` (plural,
# the v1.1 rename on p.2) with type PAY_RETURN. The Postman collection's
# `urlParam` / `partnerApplink` are superseded. Values are our merchant's.
_SPEC_CREATE_PAYMENT_BODY = {
    "partnerReferenceNo": "dsfhbvdf", "merchantId": "MERCHANT-TEST", "terminalId": "MERCHANT-TEST",
    "amount": {"value": "29880.00", "currency": "IDR"},
    "urlParams": [{"url": _NOTIFY_URL + "?order_id=dsfhbvdf", "type": "PAY_RETURN", "isDeeplink": "N"}],
    "additionalInfo": {"trxDate": "20260901135300", "terminalName": "TERMINAL-TEST",
                       "items": [{"id": "1", "name": "MERCHANT-TEST", "unitPrice": "29880.00", "qty": "1"}]},
}
# Responses per the spec (p.10, p.13) as also observed from the dev sandbox on 2026-09-28.
_SANDBOX_TOKEN_RESPONSE = {"accessToken": "tok-123", "expiresIn": "899", "responseCode": "2007300",
                           "responseMessage": "Successful", "tokenType": "Bearer"}
_SANDBOX_CREATE_RESPONSE = {"appRedirectUrl": "https://cdn-dev.linkaja.com/applink/payment?data=abc",
                            "referenceNo": "p3880523032nFcEwIrcFo", "responseCode": "2005400",
                            "responseMessage": "Successful"}
_SANDBOX_NOT_FOUND = {"responseCode": "4045501", "responseMessage": "Transaction Not Found"}
# Inquiry status positive sample, spec p.18, with our reference.
_SPEC_STATUS_RESPONSE = {
    "approvalCode": "D60153DB03", "latestTransactionStatus": "00",
    "originalPartnerReferenceNo": "order-1", "originalReferenceNo": "p128494068431236255186812016",
    "responseCode": "2005500", "responseMessage": "Successful", "serviceCode": "55",
    "transactionStatusDesc": "Success",
}


def _transport(*responses):
    """Patch httpx.AsyncClient to return the given (status, json) pairs in order."""
    patcher = patch("httpx.AsyncClient")
    transport = patcher.start()
    instance = transport.return_value.__aenter__.return_value
    instance.request = AsyncMock(side_effect=[httpx.Response(code, json=body) for code, body in responses])
    return patcher, instance.request


def test_create_payment_body_matches_the_spec():
    service = LinkAjaService(config=_full_config(usd_to_idr_rate="160"))
    request = LinkAjaPaymentRequest(merchant_order_id="dsfhbvdf", amount_minor_units=18675, currency="USD",
                                    description="MERCHANT-TEST")
    with patch("app.services.integration.link_aja.service.trx_date", return_value="20260901135300"):
        body = service._build_create_payment_body(request)
    assert body == _SPEC_CREATE_PAYMENT_BODY
    assert "urlParam" not in body                          # the pre-v1.1 name is gone
    assert body["urlParams"][0]["type"] == "PAY_RETURN"    # not partnerApplink


def test_url_params_carry_the_notify_url_not_the_success_url():
    """LinkAja confirmed: urlParams[0].url is our notify endpoint (LINKAJA_NOTIFY_URL)."""
    service = LinkAjaService(config=_full_config(success_url="https://www.google.com/"))
    body = service._build_create_payment_body(_payment_request())
    assert body["urlParams"] == [{"url": _NOTIFY_URL + "?order_id=order-1", "type": "PAY_RETURN", "isDeeplink": "N"}]


def test_create_payment_is_deeplink_is_configurable():
    service = LinkAjaService(config=_full_config(url_param_is_deeplink="Y"))
    body = service._build_create_payment_body(_payment_request())
    assert body["urlParams"] == [{"url": _NOTIFY_URL + "?order_id=order-1", "type": "PAY_RETURN", "isDeeplink": "Y"}]


@pytest.mark.asyncio
async def test_create_payment_end_to_end_request_shape_and_signatures():
    config = _full_config(**_DOC_PATHS, user_agent="ios")
    patcher, request = _transport((200, _SANDBOX_TOKEN_RESPONSE), (200, _SANDBOX_CREATE_RESPONSE))
    try:
        result = await LinkAjaService(config=config).create_payment(_payment_request())
    finally:
        patcher.stop()

    assert result.provider_reference == "p3880523032nFcEwIrcFo"
    assert result.payment_url == _SANDBOX_CREATE_RESPONSE["appRedirectUrl"]
    assert result.status == LinkAjaPaymentStatus.PENDING  # created is not paid

    token_call, pay_call = request.await_args_list
    assert token_call.args == ("POST", "https://linkaja.invalid/bi/v1.0/access-token/b2b")
    t_headers = token_call.kwargs["headers"]
    assert token_call.kwargs["json"] == {"grantType": "client_credentials"}
    assert t_headers["X-CLIENT-KEY"] == "cid"
    _verify_token_signature(_TEST_KEY.public_key(), "cid", t_headers["X-TIMESTAMP"], t_headers["X-SIGNATURE"])

    assert pay_call.args == ("POST", "https://linkaja.invalid/bi/applink/v1.0/debit/payment-host-to-host")
    headers, content = pay_call.kwargs["headers"], pay_call.kwargs["content"]
    assert "json" not in pay_call.kwargs  # signed bytes are sent verbatim
    assert headers["Authorization"] == "Bearer tok-123"
    assert headers["X-PARTNER-ID"] == "pid"
    assert headers["CHANNEL-ID"] == "CHANNEL-TEST"
    assert headers["User-Agent"] == "ios"
    assert headers["X-EXTERNAL-ID"].isdigit()
    assert headers["X-SIGNATURE"] == snap.service_signature(
        "csecret", "POST", "/bi/applink/v1.0/debit/payment-host-to-host", "tok-123", content,
        headers["X-TIMESTAMP"])
    sent = _json.loads(content)
    assert sent["partnerReferenceNo"] == "order-1"
    assert sent["amount"] == {"value": "175840.00", "currency": "IDR"}
    assert sent["urlParams"] == [{"url": _NOTIFY_URL + "?order_id=order-1", "type": "PAY_RETURN", "isDeeplink": "N"}]


@pytest.mark.parametrize("payload", [
    {**_SANDBOX_CREATE_RESPONSE, "responseCode": "2025400"},
    {k: v for k, v in _SANDBOX_CREATE_RESPONSE.items() if k != "referenceNo"},
    {k: v for k, v in _SANDBOX_CREATE_RESPONSE.items() if k != "appRedirectUrl"},
    {"responseCode": "4005402", "responseMessage": "Invalid Mandatory Field"},
])
def test_unexpected_create_payment_response_fails_closed(payload):
    with pytest.raises(LinkAjaProviderException):
        LinkAjaService(config=_full_config())._parse_create_payment(payload)


def test_token_response_without_access_token_is_a_provider_error():
    with pytest.raises(LinkAjaProviderException):
        LinkAjaService(config=_full_config())._parse_auth_response(
            {"responseCode": "4017300", "responseMessage": "Unauthorized"})


@pytest.mark.asyncio
async def test_status_inquiry_request_matches_the_spec_body():
    """§4.4 p.17-18: serviceCode "55" (the Postman collection's "54" is superseded)."""
    patcher, request = _transport((200, _SANDBOX_TOKEN_RESPONSE), (200, _SPEC_STATUS_RESPONSE))
    try:
        result = await LinkAjaService(config=_full_config(**_DOC_PATHS)).get_payment_status("order-1")
    finally:
        patcher.stop()
    status_call = request.await_args_list[1]
    assert status_call.args == ("POST", "https://linkaja.invalid/bi/applink/v1.0/debit/status")
    assert _json.loads(status_call.kwargs["content"]) == {
        "originalPartnerReferenceNo": "order-1", "serviceCode": "55", "merchantId": "MERCHANT-TEST",
        "additionalInfo": {"terminalId": "MERCHANT-TEST"}}
    assert result.status == LinkAjaPaymentStatus.SUCCESS
    assert result.provider_reference == "p128494068431236255186812016"
    assert result.approval_code == "D60153DB03"
    assert result.provider_status == "00" and result.status_description == "Success"
    assert result.merchant_order_id == "order-1"


@pytest.mark.parametrize("raw, expected", [
    ("00", LinkAjaPaymentStatus.SUCCESS),
    ("04", LinkAjaPaymentStatus.REFUNDED),
    ("06", LinkAjaPaymentStatus.FAILED),
    ("07", LinkAjaPaymentStatus.NOT_FOUND),
    ("99", LinkAjaPaymentStatus.UNKNOWN),
])
def test_status_response_maps_every_spec_status(raw, expected):
    service = LinkAjaService(config=_full_config())
    result = service._parse_status_response({**_SPEC_STATUS_RESPONSE, "latestTransactionStatus": raw}, "order-1")
    assert result.status == expected
    assert is_provisioning_status(result.status) is (raw == "00")


@pytest.mark.asyncio
async def test_status_inquiry_404_not_found_means_not_paid_yet_and_stays_pending():
    """HTTP 404 / 4045501 is a documented answer (the sandbox gives it for a
    created-but-unpaid payment): NOT_FOUND, not a transport error, so the
    caller leaves the order pending."""
    patcher, _ = _transport((200, _SANDBOX_TOKEN_RESPONSE), (404, _SANDBOX_NOT_FOUND))
    try:
        result = await LinkAjaService(config=_full_config(**_DOC_PATHS)).get_payment_status("order-1")
    finally:
        patcher.stop()
    assert result.status == LinkAjaPaymentStatus.NOT_FOUND
    assert result.status in NON_FINAL_STATUSES
    assert is_provisioning_status(result.status) is False
    assert result.provider_reference == ""


def test_status_response_for_another_reference_is_rejected():
    service = LinkAjaService(config=_full_config())
    with pytest.raises(LinkAjaProviderException):
        service._parse_status_response({**_SPEC_STATUS_RESPONSE, "originalPartnerReferenceNo": "other"},
                                       "order-1")


@pytest.mark.parametrize("payload", [
    {"responseCode": "4015500", "responseMessage": "Unauthorized. Invalid signature"},
    {"responseCode": "5005500", "responseMessage": "General Error"},
    {},
])
def test_unexpected_status_response_is_a_provider_error(payload):
    with pytest.raises(LinkAjaProviderException):
        LinkAjaService(config=_full_config())._parse_status_response(payload, "order-1")


@pytest.mark.asyncio
async def test_status_inquiry_other_http_errors_are_still_provider_errors():
    patcher, _ = _transport((200, _SANDBOX_TOKEN_RESPONSE), (500, {"responseCode": "5005500"}))
    try:
        with pytest.raises(LinkAjaProviderException):
            await LinkAjaService(config=_full_config(**_DOC_PATHS)).get_payment_status("order-1")
    finally:
        patcher.stop()


# ---------------------------------------------------------------------------
# 16. Notify signature verification (spec §5.1.3) - fail-closed
# ---------------------------------------------------------------------------

def _spec_notify_bytes(**overrides) -> bytes:
    return _json.dumps({**_SPEC_NOTIFY_BODY, **overrides}).encode()


def test_valid_notify_is_verified_and_parsed():
    service = LinkAjaService(config=_notify_config())
    body = _spec_notify_bytes()

    result = service.verify_callback(raw_body=body, headers=_signed_notify_headers(body),
                                     request_path=_NOTIFY_SPEC_PATH)

    assert result.verified is True
    assert result.merchant_order_id == "order-1"
    assert result.status == LinkAjaPaymentStatus.SUCCESS


def test_notify_headers_are_matched_case_insensitively():
    """Starlette lower-cases header names; LinkAja's spec writes them upper-case."""
    service = LinkAjaService(config=_notify_config())
    body = _spec_notify_bytes()
    headers = {k.lower(): v for k, v in _signed_notify_headers(body).items()}
    assert service.verify_callback(raw_body=body, headers=headers, request_path=_NOTIFY_SPEC_PATH).verified


def test_tampered_notify_body_is_rejected():
    service = LinkAjaService(config=_notify_config())
    body = _spec_notify_bytes()
    headers = _signed_notify_headers(body)
    tampered = body.replace(b'"175840"', b'"1"')

    with pytest.raises(LinkAjaCallbackVerificationException) as exc:
        service.verify_callback(raw_body=tampered, headers=headers, request_path=_NOTIFY_SPEC_PATH)
    assert exc.value.snap_response_code == "4015600"
    assert exc.value.code == 401


def test_notify_signed_with_the_wrong_key_is_rejected():
    """Signed with OUR key (the one in the credential pack) instead of LinkAja's."""
    service = LinkAjaService(config=_notify_config())
    body = _spec_notify_bytes()
    with pytest.raises(LinkAjaCallbackVerificationException):
        service.verify_callback(raw_body=body, headers=_signed_notify_headers(body, key=_TEST_KEY),
                                request_path=_NOTIFY_SPEC_PATH)


@pytest.mark.parametrize("missing", ["X-SIGNATURE", "X-TIMESTAMP"])
def test_notify_with_a_missing_signature_header_is_rejected(missing):
    service = LinkAjaService(config=_notify_config())
    body = _spec_notify_bytes()
    headers = _signed_notify_headers(body)
    del headers[missing]
    with pytest.raises(LinkAjaCallbackVerificationException) as exc:
        service.verify_callback(raw_body=body, headers=headers, request_path=_NOTIFY_SPEC_PATH)
    assert missing in exc.value.details


def test_notify_with_a_different_timestamp_than_signed_is_rejected():
    service = LinkAjaService(config=_notify_config())
    body = _spec_notify_bytes()
    headers = _signed_notify_headers(body)
    headers["X-TIMESTAMP"] = "2026-10-01T10:00:01+07:00"
    with pytest.raises(LinkAjaCallbackVerificationException):
        service.verify_callback(raw_body=body, headers=headers, request_path=_NOTIFY_SPEC_PATH)


def test_notify_with_the_public_key_unset_is_rejected_even_when_valid():
    """Key unset -> every notify rejected, including one that would verify."""
    service = LinkAjaService(config=_full_config())
    body = _spec_notify_bytes()
    with pytest.raises(LinkAjaConfigurationException):
        service.verify_callback(raw_body=body, headers=_signed_notify_headers(body),
                                request_path=_NOTIFY_SPEC_PATH)


def test_notify_public_key_can_be_loaded_from_a_file(tmp_path):
    key_file = tmp_path / "linkaja-notify.pem"
    key_file.write_text(_NOTIFY_PUBLIC_PEM)
    service = LinkAjaService(config=_full_config(notify_public_key_path=str(key_file)))
    body = _spec_notify_bytes()
    assert service.verify_callback(raw_body=body, headers=_signed_notify_headers(body),
                                   request_path=_NOTIFY_SPEC_PATH).verified


def test_unreadable_public_key_file_is_a_configuration_error():
    service = LinkAjaService(config=_full_config(notify_public_key_path="/nonexistent/notify.pem"))
    body = _spec_notify_bytes()
    with pytest.raises(LinkAjaConfigurationException) as exc:
        service.verify_callback(raw_body=body, headers=_signed_notify_headers(body),
                                request_path=_NOTIFY_SPEC_PATH)
    assert "LINKAJA_NOTIFY_PUBLIC_KEY_PATH" in exc.value.details


def test_configured_endpoint_url_overrides_the_request_path():
    """If LinkAja signs the full URL rather than the path, the operator pins it."""
    full_url = "https://esim.invalid/bi/applink/v1.0/debit/notify"
    service = LinkAjaService(config=_notify_config(notify_endpoint_url=full_url))
    body = _spec_notify_bytes()

    ok = service.verify_callback(raw_body=body, headers=_signed_notify_headers(body, endpoint_url=full_url),
                                 request_path=_NOTIFY_SPEC_PATH)
    assert ok.verified
    with pytest.raises(LinkAjaCallbackVerificationException):
        service.verify_callback(raw_body=body, headers=_signed_notify_headers(body, endpoint_url=_NOTIFY_SPEC_PATH),
                                request_path=_NOTIFY_SPEC_PATH)


def test_notify_without_any_endpoint_url_is_rejected():
    service = LinkAjaService(config=_notify_config())
    body = _spec_notify_bytes()
    with pytest.raises(LinkAjaCallbackVerificationException) as exc:
        service.verify_callback(raw_body=body, headers=_signed_notify_headers(body), request_path="")
    assert "LINKAJA_NOTIFY_ENDPOINT_URL" in exc.value.details


def test_rejected_notify_never_logs_the_signature():
    """A logged signature could be replayed; only the shape may be logged."""
    from loguru import logger

    service = LinkAjaService(config=_notify_config())
    body = _spec_notify_bytes()
    headers = _signed_notify_headers(body)
    headers["X-SIGNATURE"] = _b64.b64encode(b"\x01" * 256).decode()
    captured = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="DEBUG")
    try:
        with pytest.raises(LinkAjaCallbackVerificationException):
            service.verify_callback(raw_body=body, headers=headers, request_path=_NOTIFY_SPEC_PATH)
    finally:
        logger.remove(sink_id)
    assert captured and all(headers["X-SIGNATURE"] not in line for line in captured)


# ---------------------------------------------------------------------------
# 17. Notify body parsing (spec §4.3, p.14-16)
# ---------------------------------------------------------------------------

def _parse(body: dict) -> LinkAjaCallbackResult:
    return LinkAjaService(config=_notify_config())._parse_callback_body(_json.dumps(body).encode())


def test_spec_sample_notify_body_is_parsed_field_by_field():
    result = _parse(_SPEC_NOTIFY_BODY)

    assert result.verified is True
    assert result.merchant_order_id == "order-1"
    assert result.provider_reference == "LA-TXN-9"
    assert result.merchant_id == "MERCHANT-TEST"
    assert result.provider_status == "00"
    assert result.status == LinkAjaPaymentStatus.SUCCESS
    assert result.amount == Decimal("175840") and result.currency == "IDR"
    assert result.created_time == "20240326093200"
    assert result.finished_time == "20240326093200"
    assert result.approval_code == "D60232EF01"
    assert result.terminal_id == "snap applink" and result.terminal_name == "snap applink"
    assert [(i.id, i.name, i.unit_price, i.qty) for i in result.items] == [
        ("01", "Item 1", Decimal("1000"), "1"), ("02", "Item 2", Decimal("5000"), "1")]


@pytest.mark.parametrize("value", ["6000", "6000.00", "6000.0", 6000])
def test_notify_amount_accepts_with_and_without_decimals(value):
    result = _parse({**_SPEC_NOTIFY_BODY, "amount": {"currency": "IDR", "value": value}})
    assert result.amount == Decimal("6000.00")


@pytest.mark.parametrize("raw, expected", [
    ("00", LinkAjaPaymentStatus.SUCCESS), ("04", LinkAjaPaymentStatus.REFUNDED),
    ("06", LinkAjaPaymentStatus.FAILED), ("07", LinkAjaPaymentStatus.NOT_FOUND),
    ("42", LinkAjaPaymentStatus.UNKNOWN),
])
def test_notify_status_is_mapped_per_spec(raw, expected):
    assert _parse({**_SPEC_NOTIFY_BODY, "latestTransactionStatus": raw}).status == expected


@pytest.mark.parametrize("field", ["originalPartnerReferenceNo", "merchantId", "latestTransactionStatus"])
def test_notify_missing_mandatory_top_level_field_is_4005602(field):
    body = {k: v for k, v in _SPEC_NOTIFY_BODY.items() if k != field}
    with pytest.raises(LinkAjaCallbackInvalidException) as exc:
        _parse(body)
    assert exc.value.snap_response_code == "4005602"
    assert field in exc.value.snap_response_message


def test_notify_missing_amount_or_approval_code_is_4005602():
    with pytest.raises(LinkAjaCallbackInvalidException) as exc:
        _parse({**_SPEC_NOTIFY_BODY, "amount": {"currency": "IDR"}})
    assert exc.value.snap_response_message == "Invalid Mandatory Field amount.value"
    with pytest.raises(LinkAjaCallbackInvalidException) as exc:
        _parse({**_SPEC_NOTIFY_BODY, "additionalInfo": {"terminalId": "x"}})
    assert exc.value.snap_response_message == "Invalid Mandatory Field additionalInfo.approvalCode"


def test_notify_optional_fields_may_be_absent():
    body = {k: v for k, v in _SPEC_NOTIFY_BODY.items() if k not in ("originalReferenceNo", "createdTime",
                                                                     "finishedTime")}
    body["additionalInfo"] = {"approvalCode": "D60232EF01"}
    result = _parse(body)
    assert result.provider_reference is None and result.items == []


@pytest.mark.parametrize("value", ["abc", "-1", "0", ""])
def test_notify_invalid_amount_is_4005601(value):
    with pytest.raises(LinkAjaCallbackInvalidException) as exc:
        _parse({**_SPEC_NOTIFY_BODY, "amount": {"currency": "IDR", "value": value}})
    assert exc.value.snap_response_code in ("4005601", "4005602")


def test_notify_in_another_currency_is_rejected():
    with pytest.raises(LinkAjaCallbackInvalidException) as exc:
        _parse({**_SPEC_NOTIFY_BODY, "amount": {"currency": "USD", "value": "10.00"}})
    assert exc.value.snap_response_code == "4005601"


def test_notify_for_another_merchant_is_4045608():
    with pytest.raises(LinkAjaCallbackInvalidException) as exc:
        _parse({**_SPEC_NOTIFY_BODY, "merchantId": "someone_else"})
    assert exc.value.snap_response_code == "4045608"
    assert exc.value.snap_response_message == "Invalid Merchant"


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'"str"', b"\xff\xfe"])
def test_notify_non_object_body_is_4005600(raw):
    with pytest.raises(LinkAjaCallbackInvalidException) as exc:
        LinkAjaService(config=_notify_config())._parse_callback_body(raw)
    assert exc.value.snap_response_code == "4005600"


# ---------------------------------------------------------------------------
# 18. Access-token caching (spec p.10: 900 s lifetime)
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.mark.asyncio
async def test_token_is_fetched_once_and_reused_until_near_expiry():
    clock = _Clock()
    cache = TokenCache(clock=clock)
    fetch = AsyncMock(return_value=("tok-1", 899.0))

    assert await cache.get(fetch) == "tok-1"
    clock.now += 899 - REFRESH_MARGIN_SECONDS - 1          # still comfortably valid
    assert await cache.get(fetch) == "tok-1"
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_token_is_refreshed_shortly_before_expiry():
    clock = _Clock()
    cache = TokenCache(clock=clock)
    fetch = AsyncMock(side_effect=[("tok-1", 899.0), ("tok-2", 899.0)])

    assert await cache.get(fetch) == "tok-1"
    clock.now += 899 - REFRESH_MARGIN_SECONDS + 1          # inside the refresh margin
    assert await cache.get(fetch) == "tok-2"
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_token_without_a_lifetime_is_not_cached():
    cache = TokenCache(clock=_Clock())
    fetch = AsyncMock(side_effect=[("tok-1", None), ("tok-2", 0.0), ("tok-3", 899.0)])
    assert await cache.get(fetch) == "tok-1"
    assert await cache.get(fetch) == "tok-2"
    assert await cache.get(fetch) == "tok-3"
    assert await cache.get(fetch) == "tok-3"
    assert fetch.await_count == 3


@pytest.mark.asyncio
async def test_concurrent_callers_share_a_single_token_fetch():
    cache = TokenCache(clock=_Clock())
    started = asyncio.Event()

    async def slow_fetch():
        started.set()
        await asyncio.sleep(0.05)
        return "tok-1", 899.0
    fetch = AsyncMock(side_effect=slow_fetch)

    tokens = await asyncio.gather(*(cache.get(fetch) for _ in range(10)))
    assert tokens == ["tok-1"] * 10
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_cache_works_across_event_loops():
    """The project runs provisioning via asyncio.run in worker threads, so the
    same cache must be usable from a second loop (and still share the token)."""
    cache = TokenCache(clock=_Clock())
    fetch = AsyncMock(return_value=("tok-1", 899.0))
    assert await cache.get(fetch) == "tok-1"

    import threading
    seen = []
    thread = threading.Thread(target=lambda: seen.append(asyncio.run(cache.get(fetch))))
    thread.start(); thread.join(5)
    assert seen == ["tok-1"]
    assert fetch.await_count == 1


def test_invalidate_drops_the_token():
    cache = TokenCache(clock=_Clock())
    cache.store("tok-1", 899.0)
    assert cache.peek() == "tok-1"
    cache.invalidate()
    assert cache.peek() is None


@pytest.mark.asyncio
async def test_service_reuses_the_token_across_calls_and_instances():
    """Two payments from two service instances with the same credentials: one
    token call, two payment calls."""
    patcher, request = _transport((200, _SANDBOX_TOKEN_RESPONSE),
                                  (200, _SANDBOX_CREATE_RESPONSE),
                                  (200, _SANDBOX_CREATE_RESPONSE))
    try:
        await LinkAjaService(config=_full_config(**_DOC_PATHS)).create_payment(_payment_request("o1"))
        await LinkAjaService(config=_full_config(**_DOC_PATHS)).create_payment(_payment_request("o2"))
    finally:
        patcher.stop()
    urls = [call.args[1] for call in request.await_args_list]
    assert urls == ["https://linkaja.invalid/bi/v1.0/access-token/b2b",
                    "https://linkaja.invalid/bi/applink/v1.0/debit/payment-host-to-host",
                    "https://linkaja.invalid/bi/applink/v1.0/debit/payment-host-to-host"]
    assert request.await_args_list[2].kwargs["headers"]["Authorization"] == "Bearer tok-123"


@pytest.mark.asyncio
async def test_service_refreshes_the_token_when_the_sandbox_reports_it_expired():
    """A 401 on a service call (§5.2 4015501 Invalid Token) drops the cached
    token and retries once with a fresh one."""
    cache = TokenCache(clock=_Clock())
    cache.store("stale", 899.0)
    patcher, request = _transport((401, {"responseCode": "4015501", "responseMessage": "Invalid Token"}),
                                  (200, {**_SANDBOX_TOKEN_RESPONSE, "accessToken": "fresh"}),
                                  (200, _SANDBOX_CREATE_RESPONSE))
    try:
        result = await LinkAjaService(config=_full_config(**_DOC_PATHS), token_cache=cache) \
            .create_payment(_payment_request())
    finally:
        patcher.stop()
    assert result.provider_reference == _SANDBOX_CREATE_RESPONSE["referenceNo"]
    auth_headers = [c.kwargs["headers"].get("Authorization") for c in request.await_args_list]
    assert auth_headers == ["Bearer stale", None, "Bearer fresh"]
    assert cache.peek() == "fresh"


@pytest.mark.asyncio
async def test_service_does_not_retry_a_second_401():
    cache = TokenCache(clock=_Clock())
    cache.store("stale", 899.0)
    patcher, request = _transport((401, {"responseCode": "4015501"}),
                                  (200, _SANDBOX_TOKEN_RESPONSE),
                                  (401, {"responseCode": "4015501"}))
    try:
        with pytest.raises(LinkAjaProviderException):
            await LinkAjaService(config=_full_config(**_DOC_PATHS), token_cache=cache) \
                .create_payment(_payment_request())
    finally:
        patcher.stop()
    assert request.await_count == 3


@pytest.mark.asyncio
async def test_token_lifetime_is_taken_from_expires_in():
    clock = _Clock()
    cache = TokenCache(clock=clock)
    patcher, request = _transport((200, {**_SANDBOX_TOKEN_RESPONSE, "expiresIn": "120"}),
                                  (200, _SANDBOX_CREATE_RESPONSE),
                                  (200, _SANDBOX_TOKEN_RESPONSE),
                                  (200, _SANDBOX_CREATE_RESPONSE))
    try:
        service = LinkAjaService(config=_full_config(**_DOC_PATHS), token_cache=cache)
        await service.create_payment(_payment_request())
        clock.now += 120 - REFRESH_MARGIN_SECONDS + 1
        await service.create_payment(_payment_request())
    finally:
        patcher.stop()
    assert request.await_count == 4  # token, pay, token again, pay


# ---------------------------------------------------------------------------
# 19. Stored amount quote (order_data) and create_payment echo
# ---------------------------------------------------------------------------

def test_embed_and_read_order_data_round_trip():
    original = get_bundle_mock().model_dump_json()
    stored = embed_order_data(original, _stored_quote())

    assert read_order_data(stored) == _stored_quote()
    # The bundle snapshot is untouched apart from the reserved key...
    without = {k: v for k, v in _json.loads(stored).items() if k != ORDER_DATA_KEY}
    assert without == _json.loads(original)
    # ...and every existing reader still parses it (BundleDTO ignores extras).
    assert BundleDTO.model_validate_json(stored) == BundleDTO.model_validate_json(original)


def test_embed_replaces_an_existing_quote():
    once = embed_order_data(get_bundle_mock().model_dump_json(), _stored_quote(amount="1.00"))
    twice = embed_order_data(once, _stored_quote(amount="2.00"))
    assert read_order_data(twice).amount == "2.00"
    assert _json.loads(twice).keys() == _json.loads(once).keys()


@pytest.mark.parametrize("bad", [None, "", "-", "not json", "[]", '"str"'])
def test_embed_refuses_non_object_bundle_data(bad):
    with pytest.raises(ValueError):
        embed_order_data(bad, _stored_quote())


@pytest.mark.parametrize("bad", [None, "", "-", "not json", "{}", '{"linkaja_payment": "x"}',
                                 '{"linkaja_payment": {"amount": "1.00"}}'])
def test_read_order_data_is_none_when_absent_or_unreadable(bad):
    assert read_order_data(bad) is None


def test_quote_amount_uses_the_configured_rate_and_fails_closed_without_it():
    quote = LinkAjaService(config=_full_config(usd_to_idr_rate="16000")).quote_amount(1099, "USD")
    assert (quote.amount, quote.currency, quote.usd_to_idr_rate, quote.order_amount_minor_units) == \
        ("175840.00", "IDR", "16000", 1099)
    with pytest.raises(LinkAjaConfigurationException):
        LinkAjaService(config=_full_config(usd_to_idr_rate="")).quote_amount(1099, "USD")
    with pytest.raises(LinkAjaDisabledException):
        LinkAjaService(config=LinkAjaConfig(enabled=False)).quote_amount(1099, "USD")


@pytest.mark.asyncio
async def test_create_payment_result_echoes_the_amount_and_rate_sent():
    patcher, request = _transport((200, _SANDBOX_TOKEN_RESPONSE), (200, _SANDBOX_CREATE_RESPONSE))
    try:
        result = await LinkAjaService(config=_full_config(**_DOC_PATHS)).create_payment(_payment_request())
    finally:
        patcher.stop()
    sent = _json.loads(request.await_args_list[1].kwargs["content"])
    assert result.amount == sent["amount"]["value"] == "175840.00"
    assert result.usd_to_idr_rate == "16000"


# ---------------------------------------------------------------------------
# Reconciliation of pending orders (LinkAja does not retry the notify)
# ---------------------------------------------------------------------------

from datetime import datetime as _dt, timedelta as _td, timezone as _tz

from app.services.integration.link_aja import LinkAjaStatusResult
from app.services.link_aja_reconciliation_service import (
    LinkAjaReconciliationService,
    start_link_aja_reconciliation,
)

_NOW = _dt(2026, 10, 14, 12, 0, tzinfo=_tz.utc)


def _status(status=LinkAjaPaymentStatus.SUCCESS, reference="LA-TXN-9"):
    return LinkAjaStatusResult(merchant_order_id="order-1", provider_reference=reference, status=status,
                               provider_status="00", approval_code="D60153DB03")


def _make_reconciler(status=None, orders=None, age=_td(minutes=5), quote=_stored_quote()):
    callback, m = _make_callback_service(config=_full_config(**_DOC_PATHS), quote=quote)
    m["order"].created_at = (_NOW - age).isoformat()
    m["order"].payment_intent_code = "LA-TXN-9"
    m["order_repo"].list.return_value = [m["order"]] if orders is None else orders
    link_aja = MagicMock()
    link_aja.ensure_payments_available.return_value = None
    link_aja.get_payment_status = AsyncMock(return_value=status or _status())
    reconciler = LinkAjaReconciliationService(link_aja_service=link_aja, callback_service=callback,
                                              user_order_repo=m["order_repo"], clock=lambda: _NOW)
    return reconciler, {**m, "link_aja_mock": link_aja}


@pytest.fixture
def link_aja_enabled():
    with patch.dict(os.environ, {"LINKAJA_ENABLED": "true"}):
        yield


@pytest.mark.asyncio
async def test_reconciliation_does_nothing_when_link_aja_is_disabled():
    reconciler, m = _make_reconciler()
    with patch.dict(os.environ, {"LINKAJA_ENABLED": "false"}):
        assert await reconciler.reconcile_pending() == 0
    m["order_repo"].list.assert_not_called()
    m["link_aja_mock"].get_payment_status.assert_not_called()


def test_reconciliation_job_is_not_started_when_link_aja_is_disabled():
    with patch.dict(os.environ, {"LINKAJA_ENABLED": "false"}):
        assert start_link_aja_reconciliation() is None


@pytest.mark.asyncio
async def test_reconciliation_provisions_a_paid_order_whose_notify_was_lost(link_aja_enabled):
    reconciler, m = _make_reconciler()

    assert await reconciler.reconcile_pending() == 1

    m["link_aja_mock"].get_payment_status.assert_awaited_once_with("order-1")
    claim = m["order_repo"].update_by.call_args.kwargs
    assert claim["where"] == {"id": "order-1", "payment_status": OrderStatusEnum.PENDING}  # same atomic claim
    assert claim["data"]["payment_status"] == OrderStatusEnum.SUCCESS
    assert len(m["executor"].tasks) == 1
    m["bundle_service"].buy_bundle.assert_not_called()  # provisioning is queued, not inline
    m["executor"].run_all()
    m["bundle_service"].buy_bundle.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconciliation_after_the_notify_claimed_the_order_does_not_provision_again(link_aja_enabled):
    reconciler, m = _make_reconciler()
    m["order_repo"].update_by.return_value = []  # the notify already moved it off pending

    assert await reconciler.reconcile_pending() == 0
    assert m["executor"].tasks == []


@pytest.mark.asyncio
async def test_notify_and_reconciliation_together_provision_exactly_once(link_aja_enabled):
    reconciler, m = _make_reconciler()
    claimed = []

    def conditional_update(where, data, filters=None):
        if where.get("payment_status") == OrderStatusEnum.PENDING and not claimed:
            claimed.append(where["id"])
            return [{"id": where["id"]}]
        return []

    m["order_repo"].update_by.side_effect = conditional_update
    callback = reconciler._LinkAjaReconciliationService__callback_service

    ack = await callback.process_verified_callback(_verified())
    m["order"].payment_status = OrderStatusEnum.PENDING  # the job read the row before the notify won
    assert await reconciler.reconcile_pending() == 0

    assert ack.provisioning_scheduled is True
    assert len(m["executor"].tasks) == 1


@pytest.mark.parametrize("status", [LinkAjaPaymentStatus.NOT_FOUND, LinkAjaPaymentStatus.PENDING,
                                    LinkAjaPaymentStatus.UNKNOWN, LinkAjaPaymentStatus.REFUNDED])
@pytest.mark.asyncio
async def test_reconciliation_leaves_unpaid_orders_pending(link_aja_enabled, status):
    reconciler, m = _make_reconciler(status=_status(status=status, reference=""))

    assert await reconciler.reconcile_pending() == 0
    m["order_repo"].update_by.assert_not_called()
    assert m["executor"].tasks == []


@pytest.mark.parametrize("status", [LinkAjaPaymentStatus.FAILED, LinkAjaPaymentStatus.CANCELED,
                                    LinkAjaPaymentStatus.EXPIRED])
@pytest.mark.asyncio
async def test_reconciliation_records_a_final_failure_only_while_pending(link_aja_enabled, status):
    reconciler, m = _make_reconciler(status=_status(status=status))

    assert await reconciler.reconcile_pending() == 0
    recorded = m["order_repo"].update_by.call_args.kwargs
    assert recorded["where"] == {"id": "order-1", "payment_status": OrderStatusEnum.PENDING}
    assert recorded["data"]["payment_status"] == OrderStatusEnum.FAILURE
    assert m["executor"].tasks == []


@pytest.mark.asyncio
async def test_reconciliation_refuses_a_provider_reference_mismatch(link_aja_enabled):
    reconciler, m = _make_reconciler(status=_status(reference="SOMEONE-ELSE"))

    assert await reconciler.reconcile_pending() == 0
    m["order_repo"].update_by.assert_not_called()


@pytest.mark.asyncio
async def test_reconciliation_skips_orders_without_a_stored_quote(link_aja_enabled):
    reconciler, m = _make_reconciler(quote=None)

    assert await reconciler.reconcile_pending() == 0
    m["link_aja_mock"].get_payment_status.assert_not_called()


@pytest.mark.parametrize("age", [_td(seconds=10), _td(days=2)])
@pytest.mark.asyncio
async def test_reconciliation_only_polls_orders_inside_the_age_window(link_aja_enabled, age):
    reconciler, m = _make_reconciler(age=age)

    assert await reconciler.reconcile_pending() == 0
    m["link_aja_mock"].get_payment_status.assert_not_called()


@pytest.mark.asyncio
async def test_reconciliation_queries_only_pending_link_aja_orders(link_aja_enabled):
    reconciler, m = _make_reconciler()
    await reconciler.reconcile_pending()
    where = m["order_repo"].list.call_args.kwargs["where"]
    assert where == {"payment_type": PaymentTypeEnum.LINK_AJA, "payment_status": OrderStatusEnum.PENDING}


@pytest.mark.asyncio
async def test_one_failing_inquiry_does_not_stop_the_run(link_aja_enabled):
    reconciler, m = _make_reconciler()
    second = m["order"].model_copy(update={"id": "order-2"})
    m["order_repo"].list.return_value = [m["order"], second]
    m["link_aja_mock"].get_payment_status.side_effect = [RuntimeError("timeout"), _status()]

    assert await reconciler.reconcile_pending() == 1
    assert m["link_aja_mock"].get_payment_status.await_count == 2


@pytest.mark.asyncio
async def test_reconciliation_skips_the_run_when_configuration_is_incomplete(link_aja_enabled):
    reconciler, m = _make_reconciler()
    m["link_aja_mock"].ensure_payments_available.side_effect = LinkAjaConfigurationException(details="x")

    assert await reconciler.reconcile_pending() == 0
    m["order_repo"].list.assert_not_called()
