"""GET /api/v1/user/my-esim-by-order/{order_id}: on-demand LinkAja status check.

The repository serves every client, so most of these tests prove what must NOT
change: Card / Wallet / DCB orders, and LinkAja orders with the flag off, get
byte-identical responses and never reach LinkAja.
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config.db import OrderStatusEnum, PaymentTypeEnum, UserOrderType
from app.models.user import UserOrderModel
from app.schemas.bundle import EsimBundleResponse
from app.services import link_aja_status_on_read as on_read
from app.services.integration.link_aja import LinkAjaPaymentStatus, LinkAjaStatusResult, embed_order_data
from app.services.integration.link_aja import LinkAjaOrderData
from tests.mocks import get_bundle_mock, get_user_model_mock

ORDER_ID = "6269b209-c4c0-4137-9a1e-dd79d22747f1"
PATH = f"/api/v1/user/my-esim-by-order/{ORDER_ID}"
USER = get_user_model_mock()

NON_LINK_AJA = [PaymentTypeEnum.CARD, PaymentTypeEnum.WALLET, PaymentTypeEnum.DCB]

# Every (payment_status, order_status) the endpoint can see, success included.
STATES = [
    (OrderStatusEnum.SUCCESS, OrderStatusEnum.SUCCESS),
    (OrderStatusEnum.PENDING, OrderStatusEnum.PENDING),
    (OrderStatusEnum.SUCCESS, OrderStatusEnum.PENDING),
    (OrderStatusEnum.FAILURE, OrderStatusEnum.FAILURE),
    (OrderStatusEnum.CANCELED, OrderStatusEnum.CANCELED),
    (OrderStatusEnum.SUCCESS, OrderStatusEnum.FAILURE),
]

FLAGS_OFF = [
    {"LINKAJA_ENABLED": "true", "LINKAJA_STATUS_ON_READ_ENABLED": "false"},
    {"LINKAJA_ENABLED": "true"},  # LINKAJA_STATUS_ON_READ_ENABLED unset: default off
    {"LINKAJA_ENABLED": "false", "LINKAJA_STATUS_ON_READ_ENABLED": "true"},
]
FLAG_ON = {"LINKAJA_ENABLED": "true", "LINKAJA_STATUS_ON_READ_ENABLED": "true"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def esim_app():
    """The live application (real CustomException handler), imported with get_config stubbed."""
    os.environ.setdefault("ENVIRONMENT", "DEV")
    import app.config.config as config_module
    import app.config.helper as helper_module

    def _stub(key, default_value=None):
        return default_value if default_value is not None else "stub"

    with patch.object(helper_module, "get_config", _stub), patch.object(config_module, "get_config", _stub):
        from app.main import esim_app as application
    return application


def _esim_response() -> EsimBundleResponse:
    bundle = get_bundle_mock().model_dump()
    fields = {k: v for k, v in bundle.items() if k in EsimBundleResponse.model_fields}
    fields.update(is_topup_allowed=True, plan_started=False, bundle_expired=False, order_number=ORDER_ID,
                  order_status="success", qr_code_value="LPA:1$smdp.invalid$X", activation_code="X",
                  smdp_address="smdp.invalid", validity_date="2026-12-01", iccid="8900000000000000001",
                  payment_date="2026-11-18T10:00:00", display_title="t", display_subtitle="s",
                  validity_label="Day", bundle_message=[], transaction_history=[])
    return EsimBundleResponse.model_validate(fields)


def _order(payment_type, payment_status, order_status) -> UserOrderModel:
    bundle_data = get_bundle_mock().model_dump_json()
    if payment_type == PaymentTypeEnum.LINK_AJA:
        bundle_data = embed_order_data(bundle_data, LinkAjaOrderData(
            amount="175840.00", currency="IDR", usd_to_idr_rate="16000", order_amount_minor_units=1099,
            quoted_at="2026-11-18T10:00:00+07:00"))
    return UserOrderModel(id=ORDER_ID, user_id=USER.id, amount=1099, modified_amount=1099, currency="USD",
                          bundle_id="B123", order_type=UserOrderType.ASSIGN, payment_type=payment_type,
                          payment_status=payment_status, order_status=order_status, bundle_data=bundle_data,
                          tax_amount=0)


def _user_service(order: UserOrderModel):
    """A UserBundleService with only the collaborators this endpoint reads."""
    from app.services.user_service import UserBundleService

    service = UserBundleService.__new__(UserBundleService)
    repo = MagicMock()
    repo.get_first_by.return_value = order
    repo.get_by_id.return_value = order
    profiles = MagicMock()
    profiles.select.return_value = [SimpleNamespace(user_order_id=ORDER_ID)]
    currency = MagicMock()
    currency.get_rate_by_currency.return_value = 1.0
    translations = MagicMock()
    translations.get_first_by.return_value = None
    service._UserBundleService__user_order_repo = repo
    service._UserBundleService__user_profile_repo = profiles
    service._UserBundleService__currency_service = currency
    service._UserBundleService__bundle_translation_repo = translations
    return service, repo


@pytest.fixture
def call(esim_app, monkeypatch):
    """GET the endpoint for `order` under `env`. LinkAja's status inquiry and the checker
    are spied on, so a test can prove they were never reached."""
    from fastapi.testclient import TestClient

    import app.api.v1.user_bundle as routes
    from app.dependencies.security import bearer_token_anonymous, device_token
    from app.schemas.dto_mapper import DtoMapper

    esim_app.dependency_overrides[bearer_token_anonymous] = lambda: USER
    esim_app.dependency_overrides[device_token] = lambda: "device-1"
    monkeypatch.setattr(DtoMapper, "get_profile_current_bundle",
                        staticmethod(lambda profile: SimpleNamespace(bundle_data=get_bundle_mock().model_dump())))
    monkeypatch.setattr(DtoMapper, "to_esim_bundle_response", staticmethod(lambda **kwargs: _esim_response()))

    def _call(order, env, inquiry=None, throttle=None, callback_service=None):
        service, repo = _user_service(order)
        monkeypatch.setattr(routes, "service", service)
        inquiry = inquiry or AsyncMock(side_effect=AssertionError("LinkAja must not be called"))
        monkeypatch.setattr("app.services.integration.link_aja.service.LinkAjaService.get_payment_status",
                            inquiry)
        if callback_service is None:
            from app.services.link_aja_callback_service import LinkAjaCallbackService
            callback_service = LinkAjaCallbackService(user_order_repo=repo, bundle_service=MagicMock(),
                                                      task_executor=MagicMock())
        checker = on_read.LinkAjaStatusOnRead(callback_service=callback_service, throttle=throttle)
        checker_spy = MagicMock(return_value=checker)
        monkeypatch.setattr(on_read, "checker", checker_spy)
        with patch.dict(os.environ, env):
            if "LINKAJA_STATUS_ON_READ_ENABLED" not in env:
                os.environ.pop("LINKAJA_STATUS_ON_READ_ENABLED", None)
            response = TestClient(esim_app, raise_server_exceptions=False).get(
                PATH, headers={"x-device-id": "device-1"})
        return SimpleNamespace(response=response, inquiry=inquiry, checker=checker_spy, repo=repo)

    yield _call
    esim_app.dependency_overrides.pop(bearer_token_anonymous, None)
    esim_app.dependency_overrides.pop(device_token, None)


def _reference(call, order_args):
    """Today's behaviour: LinkAja disabled entirely, so no new code can run."""
    return call(_order(*order_args), {"LINKAJA_ENABLED": "false",
                                      "LINKAJA_STATUS_ON_READ_ENABLED": "false"}).response


@pytest.mark.parametrize("payment_type", [*NON_LINK_AJA, PaymentTypeEnum.LINK_AJA])
def test_reference_success_is_a_real_200(call, payment_type):
    """Guards the comparisons below: a paid, provisioned order really answers 200 with data."""
    response = _reference(call, (payment_type, OrderStatusEnum.SUCCESS, OrderStatusEnum.SUCCESS))
    assert response.status_code == 200
    assert response.json()["data"]["order_number"] == ORDER_ID


# ---------------------------------------------------------------------------
# Card / Wallet / DCB: byte-identical, flag on or off, LinkAja never reached
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env", [FLAG_ON, *FLAGS_OFF])
@pytest.mark.parametrize("payment_status, order_status", STATES)
@pytest.mark.parametrize("payment_type", NON_LINK_AJA)
def test_other_payment_types_are_byte_identical(call, payment_type, payment_status, order_status, env):
    expected = _reference(call, (payment_type, payment_status, order_status))
    result = call(_order(payment_type, payment_status, order_status), env)

    assert result.response.status_code == expected.status_code
    assert result.response.content == expected.content
    assert result.response.headers.get("content-type") == expected.headers.get("content-type")
    result.inquiry.assert_not_called()
    result.checker.assert_not_called()


@pytest.mark.parametrize("payment_type", NON_LINK_AJA)
def test_other_payment_types_still_get_400_payment_pending(call, payment_type):
    result = call(_order(payment_type, OrderStatusEnum.PENDING, OrderStatusEnum.PENDING), FLAG_ON)
    assert result.response.status_code == 400
    assert result.response.json()["developerMessage"] == "PAYMENT_FAILED"
    assert result.response.json()["title"] == "Payment is still pending."


@pytest.mark.parametrize("payment_type", NON_LINK_AJA)
def test_non_link_aja_orders_do_not_even_read_the_flags(payment_type):
    order = _order(payment_type, OrderStatusEnum.PENDING, OrderStatusEnum.PENDING)
    with patch.object(on_read, "is_link_aja_enabled") as link_aja_flag, \
            patch.object(on_read, "is_status_on_read_enabled") as on_read_flag:
        assert on_read.applies_to(order) is False
    link_aja_flag.assert_not_called()
    on_read_flag.assert_not_called()


# ---------------------------------------------------------------------------
# LinkAja with a flag off: exactly today's behaviour
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env", FLAGS_OFF)
@pytest.mark.parametrize("payment_status, order_status", STATES)
def test_link_aja_with_a_flag_off_is_byte_identical(call, payment_status, order_status, env):
    expected = _reference(call, (PaymentTypeEnum.LINK_AJA, payment_status, order_status))
    result = call(_order(PaymentTypeEnum.LINK_AJA, payment_status, order_status), env)

    assert result.response.status_code == expected.status_code
    assert result.response.content == expected.content
    result.inquiry.assert_not_called()
    result.checker.assert_not_called()


def test_link_aja_with_the_flag_off_still_gets_400_payment_pending(call):
    result = call(_order(PaymentTypeEnum.LINK_AJA, OrderStatusEnum.PENDING, OrderStatusEnum.PENDING),
                  {"LINKAJA_ENABLED": "true"})
    assert result.response.status_code == 400
    assert result.response.json()["developerMessage"] == "PAYMENT_FAILED"
    assert result.response.json()["title"] == "Payment is still pending."


# ---------------------------------------------------------------------------
# LinkAja with the flag on
# ---------------------------------------------------------------------------

def _status(status):
    return LinkAjaStatusResult(merchant_order_id=ORDER_ID, provider_reference="LA-TXN-9", status=status,
                               approval_code="D60232EF01")


@pytest.mark.parametrize("status", [LinkAjaPaymentStatus.NOT_FOUND, LinkAjaPaymentStatus.PENDING,
                                    LinkAjaPaymentStatus.UNKNOWN, LinkAjaPaymentStatus.FAILED])
def test_link_aja_still_pending_gets_a_retryable_202(call, status):
    result = call(_order(PaymentTypeEnum.LINK_AJA, OrderStatusEnum.PENDING, OrderStatusEnum.PENDING), FLAG_ON,
                  inquiry=AsyncMock(return_value=_status(status)))

    assert result.response.status_code == 202
    assert result.response.headers["retry-after"] == "5"
    body = result.response.json()
    assert body == {"status": "pending", "totalCount": 0, "data": None, "title": "Payment is still pending.",
                    "message": "Payment is still pending.", "developerMessage": "PAYMENT_PENDING",
                    "responseCode": 202}
    result.inquiry.assert_awaited_once()
    result.repo.update_by.assert_not_called()  # only 00 changes state


def test_link_aja_paid_is_claimed_once_and_reported_as_being_prepared(call):
    from app.services.link_aja_callback_service import LinkAjaCallbackService

    executor = MagicMock()
    executor.add_task.return_value = True
    order = _order(PaymentTypeEnum.LINK_AJA, OrderStatusEnum.PENDING, OrderStatusEnum.PENDING)
    repo = MagicMock()
    repo.update_by.return_value = [{"id": ORDER_ID}]
    callbacks = LinkAjaCallbackService(user_order_repo=repo, bundle_service=MagicMock(), task_executor=executor)

    result = call(order, FLAG_ON, inquiry=AsyncMock(return_value=_status(LinkAjaPaymentStatus.SUCCESS)),
                  callback_service=callbacks)

    assert result.response.status_code == 202
    assert result.response.json()["developerMessage"] == "ORDER_PENDING"
    claim = repo.update_by.call_args.kwargs
    assert claim["where"] == {"id": ORDER_ID, "payment_status": OrderStatusEnum.PENDING}  # same atomic claim
    assert claim["data"]["payment_status"] == OrderStatusEnum.SUCCESS
    executor.add_task.assert_called_once()  # provisioning queued, not inline


def test_link_aja_claim_lost_to_the_job_does_not_provision(call):
    from app.services.link_aja_callback_service import LinkAjaCallbackService

    executor = MagicMock()
    repo = MagicMock()
    repo.update_by.return_value = []  # the job / a notify claimed it first
    callbacks = LinkAjaCallbackService(user_order_repo=repo, bundle_service=MagicMock(), task_executor=executor)

    result = call(_order(PaymentTypeEnum.LINK_AJA, OrderStatusEnum.PENDING, OrderStatusEnum.PENDING), FLAG_ON,
                  inquiry=AsyncMock(return_value=_status(LinkAjaPaymentStatus.SUCCESS)), callback_service=callbacks)

    assert result.response.status_code == 202
    executor.add_task.assert_not_called()


def test_link_aja_paid_and_provisioned_gets_the_normal_response(call):
    expected = _reference(call, (PaymentTypeEnum.LINK_AJA, OrderStatusEnum.SUCCESS, OrderStatusEnum.SUCCESS))
    result = call(_order(PaymentTypeEnum.LINK_AJA, OrderStatusEnum.SUCCESS, OrderStatusEnum.SUCCESS), FLAG_ON)
    assert result.response.status_code == 200
    assert result.response.content == expected.content
    result.inquiry.assert_not_called()


def test_link_aja_paid_while_provisioning_gets_202_without_asking_linkaja(call):
    result = call(_order(PaymentTypeEnum.LINK_AJA, OrderStatusEnum.SUCCESS, OrderStatusEnum.PENDING), FLAG_ON)
    assert result.response.status_code == 202
    assert result.response.json()["developerMessage"] == "ORDER_PENDING"
    result.inquiry.assert_not_called()


@pytest.mark.parametrize("payment_status, order_status", [
    (OrderStatusEnum.FAILURE, OrderStatusEnum.FAILURE),
    (OrderStatusEnum.CANCELED, OrderStatusEnum.CANCELED),
    (OrderStatusEnum.SUCCESS, OrderStatusEnum.FAILURE),
])
def test_link_aja_final_failures_keep_todays_error(call, payment_status, order_status):
    expected = _reference(call, (PaymentTypeEnum.LINK_AJA, payment_status, order_status))
    result = call(_order(PaymentTypeEnum.LINK_AJA, payment_status, order_status), FLAG_ON)
    assert result.response.status_code == expected.status_code == 400
    assert result.response.content == expected.content
    result.inquiry.assert_not_called()


def test_link_aja_inquiry_timeout_still_answers_202(call):
    async def slow(_self, _order_id):
        await asyncio.sleep(10)

    with patch.object(on_read, "INQUIRY_TIMEOUT_SECONDS", 0.05):
        result = call(_order(PaymentTypeEnum.LINK_AJA, OrderStatusEnum.PENDING, OrderStatusEnum.PENDING),
                      FLAG_ON, inquiry=slow)
    assert result.response.status_code == 202
    result.repo.update_by.assert_not_called()


def test_link_aja_inquiry_error_still_answers_202(call):
    result = call(_order(PaymentTypeEnum.LINK_AJA, OrderStatusEnum.PENDING, OrderStatusEnum.PENDING), FLAG_ON,
                  inquiry=AsyncMock(side_effect=RuntimeError("down")))
    assert result.response.status_code == 202


def test_link_aja_inquiry_is_throttled_per_order(call):
    clock = [100.0]
    throttle = on_read.InquiryThrottle(clock=lambda: clock[0])
    inquiry = AsyncMock(return_value=_status(LinkAjaPaymentStatus.NOT_FOUND))
    order_args = (PaymentTypeEnum.LINK_AJA, OrderStatusEnum.PENDING, OrderStatusEnum.PENDING)

    for _ in range(3):  # three polls within 5 s: one inquiry
        assert call(_order(*order_args), FLAG_ON, inquiry=inquiry, throttle=throttle).response.status_code == 202
    assert inquiry.await_count == 1
    clock[0] += 5.0
    call(_order(*order_args), FLAG_ON, inquiry=inquiry, throttle=throttle)
    assert inquiry.await_count == 2


def test_throttle_is_per_order():
    throttle = on_read.InquiryThrottle(clock=lambda: 0.0)
    assert throttle.allow("a") and throttle.allow("b")
    assert not throttle.allow("a")


@pytest.mark.parametrize("value, enabled", [(None, False), ("", False), ("false", False), ("no", False),
                                            ("true", True), ("1", True), ("YES", True), ("on", True)])
def test_status_on_read_flag_defaults_to_off(value, enabled):
    env = {} if value is None else {"LINKAJA_STATUS_ON_READ_ENABLED": value}
    with patch.dict(os.environ, env, clear=True):
        assert on_read.is_status_on_read_enabled() is enabled
