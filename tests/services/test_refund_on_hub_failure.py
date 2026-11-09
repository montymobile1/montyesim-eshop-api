"""Refund when the eSIM Hub order / top-up fails after the customer paid.

Card -> Stripe refund (+ Stripe Tax reversal), Wallet -> credit back; ambiguous Hub failures, DCB and
LinkAja -> manual review + alert; never two refunds for one order; successful purchases unchanged.
No test performs a network call: Stripe, the Hub and the database are mocked or faked.
"""
import asyncio
import json
import os
import threading
from types import SimpleNamespace
from typing import Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import stripe

from app.config.constants import ErrorMessages, PaymentStatusEnum, UserWalletTransactionSource
from app.config.db import OrderRefundStatusEnum, OrderStatusEnum, PaymentTypeEnum, UserOrderType
from app.exceptions import BadRequestException, EsimHubException, HubOrderFailedException, \
    OrderNotFulfilledException
from app.models.order_refund import UserOrderRefundModel
from app.models.user import UserOrderModel
from app.schemas.bundle import BundleCategoryDTO
from app.schemas.home import BundleDTO
from app.schemas.response import ResponseHelper
from app.services.integration.esim_hub_outcome import HubCallOutcome, HubFailureKind
from app.services.order_refund_service import OrderRefundService, RefundResult

ORDER_ID = "11111111-1111-1111-1111-111111111111"
USER_ID = "22222222-2222-2222-2222-222222222222"
PI_ID = "pi_test_123"
TAX_TXN_ID = "tax_txn_123"


def _bundle() -> BundleDTO:
    return BundleDTO(
        bundle_code="TEST123", original_price=10.0, price=10.0, currency_code="USD", price_display="10.00 USD",
        display_title="Test Bundle", display_subtitle="Subtitle",
        bundle_category=BundleCategoryDTO(type="data", title="Data", code="CAT1"),
        bundle_marketing_name="Marketing", bundle_name="Bundle Name", count_countries=1,
        gprs_limit_display="1GB", unlimited=False, validity=30, validity_display="30 days", countries=[])


def _order(**overrides) -> UserOrderModel:
    data = dict(id=ORDER_ID, user_id=USER_ID, amount=1000, currency="USD", modified_amount=1000,
                order_type=UserOrderType.ASSIGN, payment_type=PaymentTypeEnum.CARD,
                payment_intent_code=PI_ID, bundle_id="TEST123", bundle_data=_bundle().model_dump_json())
    data.update(overrides)
    return UserOrderModel(**data)


def _failure(definite: bool) -> HubOrderFailedException:
    return HubOrderFailedException("Payment failed", definite=definite,
                                   reason="definite: refused" if definite else "ambiguous: timeout")


# --------------------------------------------------------------------------- fakes


class FakeRefundRepo:
    """In-memory user_order_refund with the same conditional semantics (and a lock, like the database)."""

    def __init__(self):
        self.rows: Dict[str, dict] = {}
        self.lock = threading.Lock()

    def insert_if_absent(self, data: dict) -> Optional[UserOrderRefundModel]:
        with self.lock:
            if data["order_id"] in self.rows:
                return None
            self.rows[data["order_id"]] = dict(data)
            return UserOrderRefundModel(**self.rows[data["order_id"]])

    def transition(self, order_id: str, from_status: str, data: dict) -> Optional[UserOrderRefundModel]:
        with self.lock:
            row = self.rows.get(order_id)
            if row is None or row["status"] != from_status:
                return None
            row.update(data)
            return UserOrderRefundModel(**row)

    def get_by_order_id(self, order_id: str) -> Optional[UserOrderRefundModel]:
        row = self.rows.get(order_id)
        return UserOrderRefundModel(**row) if row else None

    def status(self, order_id: str = ORDER_ID) -> Optional[str]:
        row = self.rows.get(order_id)
        return row["status"] if row else None


class FakeWallet:
    """Tracks the balance the way add_wallet_transaction applies it (no FX when order_currency is set)."""

    def __init__(self, balance: float = 100.0, currency: str = "USD"):
        self.balance = balance
        self.currency = currency
        self.calls = []
        self.fail_on_credit = False

    def add_wallet_transaction(self, amount, user_id, source="TopUp", order_currency=None,
                               payment_reference=None, send_push=True):
        self.calls.append(dict(amount=amount, user_id=user_id, source=source, order_currency=order_currency,
                               send_push=send_push))
        if amount > 0 and self.fail_on_credit:
            raise RuntimeError("wallet down")
        self.balance = round(self.balance + amount, 2)

    def get_user_wallet(self, user_id):
        return SimpleNamespace(id="wallet-1", amount=self.balance, currency=self.currency)


class ImmediateExecutor:
    def __init__(self):
        self.tasks = []

    def add_task(self, task):
        self.tasks.append(task)
        return True


def _refund_service(order: UserOrderModel = None, provisioned_bundle=None, wallet: FakeWallet = None):
    repo = FakeRefundRepo()
    order_repo = MagicMock()
    order_repo.get_by_id.return_value = order or _order()
    profile_bundle_repo = MagicMock()
    profile_bundle_repo.get_first_by.return_value = provisioned_bundle
    executor = ImmediateExecutor()
    service = OrderRefundService(refund_repo=repo, user_order_repo=order_repo,
                                 user_profile_bundle_repo=profile_bundle_repo, user_repo=MagicMock(),
                                 user_wallet_service=wallet or FakeWallet(), task_executor=executor)
    return service, repo, executor


def _stripe_refund(amount=1180, currency="usd", refund_id="re_123"):
    return SimpleNamespace(id=refund_id, amount=amount, currency=currency)


# =========================================================================== 1. Hub classification


def _hub_service():
    from app.services.integration.esim_hub_service import EsimHubService
    return EsimHubService(base_url="https://hub.test", api_key="k", tenant_key="t")


def _user():
    return SimpleNamespace(email="u@test.com", metadata={"msisdn": "1", "first_name": "A", "last_name": "B"})


@pytest.mark.asyncio
async def test_hub_order_success_false_is_definite():
    hub = _hub_service()
    hub._EsimHubService__do_request = AsyncMock(return_value={"success": False, "message": "no stock"})
    outcome = HubCallOutcome()
    assert await hub.create_reseller_order("B", "o", _user(), outcome=outcome) is None
    assert outcome.kind == HubFailureKind.DEFINITE and outcome.definite


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    EsimHubException("eSIM Hub API request failed: 400"),   # 4xx: manual review, not refunded
    EsimHubException("eSIM Hub API request failed: 503"),   # 5xx
    EsimHubException("ReadTimeout"),                        # timeout / network
])
async def test_hub_order_http_errors_and_timeouts_are_ambiguous(error):
    hub = _hub_service()
    hub._EsimHubService__do_request = AsyncMock(side_effect=error)
    outcome = HubCallOutcome()
    assert await hub.create_reseller_order("B", "o", _user(), outcome=outcome) is None
    assert outcome.kind == HubFailureKind.AMBIGUOUS


@pytest.mark.asyncio
async def test_hub_order_accepted_then_follow_up_failed_is_ambiguous():
    """The Hub created the order; only the activation-code lookup failed. Refunding would give away an eSIM."""
    hub = _hub_service()
    hub._EsimHubService__do_request = AsyncMock(return_value={"success": True, "data": {"orderId": "hub-1"}})
    hub.get_activation_code = AsyncMock(side_effect=RuntimeError("timeout"))
    outcome = HubCallOutcome()
    assert await hub.create_reseller_order("B", "o", _user(), outcome=outcome) is None
    assert outcome.kind == HubFailureKind.AMBIGUOUS
    assert "accepted" in outcome.detail


@pytest.mark.asyncio
async def test_hub_order_without_success_flag_is_ambiguous():
    hub = _hub_service()
    hub._EsimHubService__do_request = AsyncMock(return_value={"message": "?"})
    outcome = HubCallOutcome()
    assert await hub.create_reseller_order("B", "o", _user(), outcome=outcome) is None
    assert outcome.kind == HubFailureKind.AMBIGUOUS


@pytest.mark.asyncio
async def test_hub_topup_success_false_is_definite_and_missing_flag_is_ambiguous():
    hub = _hub_service()
    hub._EsimHubService__do_request = AsyncMock(return_value={"success": False, "message": "no", "data": None})
    refused = HubCallOutcome()
    with pytest.raises(Exception):  # unchanged: the parse of a refused top-up raises, the caller sees an error
        await hub.create_reseller_topup("B", "hub-1", "o", _user(), outcome=refused)
    assert refused.definite

    hub._EsimHubService__do_request = AsyncMock(return_value={"message": "?"})
    unknown = HubCallOutcome()
    assert await hub.create_reseller_topup("B", "hub-1", "o", _user(), outcome=unknown) is None
    assert unknown.kind == HubFailureKind.AMBIGUOUS


def test_first_classification_wins():
    outcome = HubCallOutcome()
    outcome.mark(HubFailureKind.DEFINITE, "refused")
    outcome.mark(HubFailureKind.AMBIGUOUS, "parse error that followed")
    assert outcome.definite and outcome.detail == "refused"
    assert HubCallOutcome().reason.startswith("eSIM Hub failure was not classified")


# =========================================================================== 2. BundleService failure value


@pytest.fixture
def bundle_service():
    with patch('app.services.bundle_service.esim_hub_service_instance'), \
            patch('app.services.bundle_service.GroupingService'), \
            patch('app.services.bundle_service.BundleRepo'), patch('app.services.bundle_service.TagRepo'), \
            patch('app.services.bundle_service.BundleTagRepo'), \
            patch('app.services.bundle_service.CurrencyService'), patch('app.services.bundle_service.UserRepo'), \
            patch('app.services.bundle_service.UserOrderRepo'), \
            patch('app.services.bundle_service.UserProfileRepo'), \
            patch('app.services.bundle_service.UserProfileBundleRepo'), \
            patch('app.services.bundle_service.PromotionService'), \
            patch('app.services.bundle_service.TaskExecutor'), patch('app.services.bundle_service.UserWalletRepo'):
        from app.services.bundle_service import BundleService
        service = BundleService()
    hub = MagicMock()
    service._BundleService__esim_hub_service = hub
    service._BundleService__currency_service.get_currency_rate.return_value = 1.0
    service._BundleService__user_repo.get_by_id.return_value = _user()
    service._BundleService__promotion_service.update_promotion_usage = AsyncMock()
    service._BundleService__promotion_service.apply_promotion_code_after_purchase = AsyncMock()
    return service, hub


def _refusing(kind: HubFailureKind):
    async def call(*args, outcome=None, **kwargs):
        outcome.mark(kind, "test")
        return None
    return call


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,definite", [(HubFailureKind.DEFINITE, True), (HubFailureKind.AMBIGUOUS, False)])
async def test_buy_bundle_failure_returns_classified_bad_request_and_fails_promo(bundle_service, kind, definite):
    service, hub = bundle_service
    hub.create_reseller_order = AsyncMock(side_effect=_refusing(kind))
    order = _order(promo_code="PROMO10")
    result = await service.buy_bundle(order, _bundle(), USER_ID, OrderStatusEnum.SUCCESS, PaymentTypeEnum.CARD,
                                      rule_id="r1")
    assert isinstance(result, HubOrderFailedException) and isinstance(result, BadRequestException)
    assert (result.code, result.details, result.definite) == (400, "Payment failed", definite)
    service._BundleService__promotion_service.update_promotion_usage.assert_awaited_once_with(
        user_id=USER_ID, code="PROMO10", status="failed", rule_id="r1", order_id=ORDER_ID)
    service._BundleService__promotion_service.apply_promotion_code_after_purchase.assert_not_called()  # no cashback


@pytest.mark.asyncio
async def test_buy_bundle_unclassified_none_is_ambiguous(bundle_service):
    service, hub = bundle_service
    hub.create_reseller_order = AsyncMock(return_value=None)
    result = await service.buy_bundle(_order(), _bundle(), USER_ID, OrderStatusEnum.SUCCESS, PaymentTypeEnum.CARD)
    assert isinstance(result, HubOrderFailedException) and not result.definite


@pytest.mark.asyncio
async def test_top_up_bundle_failure_is_classified(bundle_service):
    service, hub = bundle_service
    service._BundleService__user_profile_repo.get_first_by.return_value = SimpleNamespace(esim_hub_order_id="h")
    hub.create_reseller_topup = AsyncMock(side_effect=_refusing(HubFailureKind.DEFINITE))
    result = await service.top_up_bundle(_bundle(), _order(order_type=UserOrderType.BUNDLE_TOP_UP), "iccid",
                                         USER_ID, OrderStatusEnum.SUCCESS)
    assert isinstance(result, HubOrderFailedException) and result.definite

    hub.create_reseller_topup = AsyncMock(side_effect=EsimHubException("eSIM Hub API request failed: 502"))
    result = await service.top_up_bundle(_bundle(), _order(order_type=UserOrderType.BUNDLE_TOP_UP), "iccid",
                                         USER_ID, OrderStatusEnum.SUCCESS)
    assert isinstance(result, HubOrderFailedException) and not result.definite


@pytest.mark.asyncio
async def test_buy_bundle_success_is_unchanged(bundle_service):
    service, hub = bundle_service
    hub_order = SimpleNamespace(orderId="hub-1", iccid="8900", validityData="30", smdpAdress="smdp",
                                activationCode="AC", allowTopup=True)
    hub.create_reseller_order = AsyncMock(return_value=hub_order)
    service._BundleService__user_profile_repo.create.return_value = SimpleNamespace(id="p1")
    with patch('threading.Thread'), patch('app.services.bundle_service.fcm_service'):
        result = await service.buy_bundle(_order(), _bundle(), USER_ID, OrderStatusEnum.SUCCESS,
                                          PaymentTypeEnum.CARD)
    assert result.status == "success"
    assert isinstance(hub.create_reseller_order.await_args.kwargs["outcome"], HubCallOutcome)
    assert hub.create_reseller_order.await_args.kwargs["outcome"].kind is None   # untouched on success
    service._BundleService__promotion_service.update_promotion_usage.assert_not_called()


# =========================================================================== 3. Card refunds


def test_card_definite_failure_refunds_once_with_idempotency_key_and_reverses_tax():
    service, repo, executor = _refund_service()
    with patch.object(stripe.Refund, "create", return_value=_stripe_refund()) as refund, \
            patch.object(stripe.tax.Transaction, "create_reversal", return_value=SimpleNamespace(id="taxrev_1")) as rev:
        result = service.handle_card_hub_failure(_order(), _failure(True), PI_ID, TAX_TXN_ID)
    assert result == RefundResult.REFUNDED
    refund.assert_called_once()
    kwargs = refund.call_args.kwargs
    assert kwargs["payment_intent"] == PI_ID
    assert kwargs["idempotency_key"] == f"refund-{ORDER_ID}"
    assert "amount" not in kwargs            # full captured amount: after promo, including tax
    rev.assert_called_once_with(mode="full", original_transaction=TAX_TXN_ID, reference=f"refund-{ORDER_ID}")
    row = repo.rows[ORDER_ID]
    assert row["status"] == OrderRefundStatusEnum.REFUNDED
    assert (row["amount"], row["currency"], row["reference"]) == (11.80, "USD", "re_123")
    assert row["tax_reversal_id"] == "taxrev_1"
    assert len(executor.tasks) == 1           # customer push + email


def test_customer_is_notified_by_push_and_email(monkeypatch):
    monkeypatch.setenv("EMAIL_TEMPLATES_PATH", "app/email_templates")
    service, _, executor = _refund_service()
    user = SimpleNamespace(email="c@test.com", metadata={"email": "c@test.com", "language": "en"})
    service._OrderRefundService__user_repo.get_by_id.return_value = user
    with patch.object(stripe.Refund, "create", return_value=_stripe_refund()):
        service.handle_card_hub_failure(_order(), _failure(True), PI_ID)
    with patch('app.services.order_refund_service.fcm_service') as fcm, \
            patch('app.services.order_refund_service.send_email') as email:
        executor.tasks[0]()
    content = fcm.send_notification_to_user_from_template.call_args.args[0]
    assert content.data["category"] == "11" and "11.80 USD" in content.message
    assert email.call_args.kwargs["recipients"] == "c@test.com"
    assert "11.80 USD" in email.call_args.kwargs["html_content"]


def test_webhook_redelivery_or_retry_still_refunds_once():
    service, repo, _ = _refund_service()
    with patch.object(stripe.Refund, "create", return_value=_stripe_refund()) as refund:
        first = service.handle_card_hub_failure(_order(), _failure(True), PI_ID)
        second = service.handle_card_hub_failure(_order(), _failure(True), PI_ID)
        retry = service.retry_failed_refund(ORDER_ID)       # not in 'failed': nothing to retry
    assert (first, second, retry) == (RefundResult.REFUNDED, RefundResult.ALREADY_HANDLED,
                                      RefundResult.ALREADY_HANDLED)
    assert refund.call_count == 1


def test_concurrent_workers_refund_once():
    service, repo, _ = _refund_service()
    results = []
    with patch.object(stripe.Refund, "create", return_value=_stripe_refund()) as refund:
        threads = [threading.Thread(target=lambda: results.append(
            service.handle_card_hub_failure(_order(), _failure(True), PI_ID))) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert refund.call_count == 1
    assert results.count(RefundResult.REFUNDED) == 1
    assert results.count(RefundResult.ALREADY_HANDLED) == 7


def test_ambiguous_hub_error_is_not_refunded_and_flagged(monkeypatch):
    monkeypatch.setenv("EMAIL_TEMPLATES_PATH", "app/email_templates")
    service, repo, executor = _refund_service()
    with patch.object(stripe.Refund, "create") as refund:
        result = service.handle_card_hub_failure(_order(), _failure(False), PI_ID)
        raised = service.handle_card_hub_failure(_order(id="33333333-3333-3333-3333-333333333333"), None, PI_ID,
                                                 error=RuntimeError("boom"))
    assert (result, raised) == (RefundResult.MANUAL_REVIEW, RefundResult.MANUAL_REVIEW)
    refund.assert_not_called()
    assert repo.status() == OrderRefundStatusEnum.MANUAL_REVIEW
    assert len(executor.tasks) == 2       # admin alerts
    with patch('app.services.order_refund_service.get_config', return_value="ops@test.com"), \
            patch('app.services.order_refund_service.send_email') as email:
        executor.tasks[0]()
    assert email.call_args.kwargs["recipients"] == "ops@test.com"
    assert "manual review" in email.call_args.kwargs["subject"].lower()


@pytest.mark.parametrize("order_overrides,profile_bundle", [
    ({"esim_order_id": "hub-1"}, None),           # the Hub order id was recorded
    ({}, SimpleNamespace(id=1)),                  # a bundle was provisioned for the order
])
def test_definite_failure_on_a_provisioned_order_is_not_refunded(order_overrides, profile_bundle):
    service, repo, _ = _refund_service(order=_order(**order_overrides), provisioned_bundle=profile_bundle)
    with patch.object(stripe.Refund, "create") as refund:
        assert service.handle_card_hub_failure(_order(), _failure(True), PI_ID) == RefundResult.MANUAL_REVIEW
    refund.assert_not_called()


def test_stripe_refund_failure_is_flagged_logged_and_safe_to_retry():
    service, repo, executor = _refund_service()
    with patch.object(stripe.Refund, "create", side_effect=stripe.error.APIConnectionError("down")) as refund:
        assert service.handle_card_hub_failure(_order(), _failure(True), PI_ID) == RefundResult.FAILED
    assert repo.status() == OrderRefundStatusEnum.FAILED
    assert "Stripe refund failed" in repo.rows[ORDER_ID]["reason"]
    assert len(executor.tasks) == 1                          # admin alert
    with patch.object(stripe.Refund, "create", return_value=_stripe_refund()) as refund:
        assert service.retry_failed_refund(ORDER_ID) == RefundResult.REFUNDED
        assert service.retry_failed_refund(ORDER_ID) == RefundResult.ALREADY_HANDLED
    refund.assert_called_once()
    assert refund.call_args.kwargs["idempotency_key"] == f"refund-{ORDER_ID}"   # same key: Stripe dedupes
    assert repo.status() == OrderRefundStatusEnum.REFUNDED


def test_charge_already_refunded_counts_as_refunded():
    service, repo, _ = _refund_service()
    error = stripe.error.InvalidRequestError("already refunded", param=None, code="charge_already_refunded")
    with patch.object(stripe.Refund, "create", side_effect=error):
        assert service.handle_card_hub_failure(_order(), _failure(True), PI_ID) == RefundResult.REFUNDED
    assert repo.status() == OrderRefundStatusEnum.REFUNDED


def test_tax_reversal_failure_keeps_the_refund_and_alerts():
    service, repo, executor = _refund_service()
    with patch.object(stripe.Refund, "create", return_value=_stripe_refund()), \
            patch.object(stripe.tax.Transaction, "create_reversal", side_effect=RuntimeError("tax down")):
        assert service.handle_card_hub_failure(_order(), _failure(True), PI_ID, TAX_TXN_ID) == RefundResult.REFUNDED
    assert repo.status() == OrderRefundStatusEnum.REFUNDED
    assert len(executor.tasks) == 2                          # customer notification + tax alert


# =========================================================================== 4. Wallet refunds (service level)


def test_wallet_definite_failure_credits_back_once():
    wallet = FakeWallet(balance=90.0)          # 10.00 already debited
    service, repo, _ = _refund_service(order=_order(payment_type=PaymentTypeEnum.WALLET), wallet=wallet)
    order = _order(payment_type=PaymentTypeEnum.WALLET)
    assert service.handle_wallet_hub_failure(order, _failure(True), 10.0, "USD") == RefundResult.REFUNDED
    assert service.handle_wallet_hub_failure(order, _failure(True), 10.0, "USD") == RefundResult.ALREADY_HANDLED
    assert wallet.balance == 100.0
    assert wallet.calls == [dict(amount=10.0, user_id=USER_ID, source=UserWalletTransactionSource.REFUND_BUNDLE,
                                 order_currency="USD", send_push=False)]
    assert repo.status() == OrderRefundStatusEnum.REFUNDED


def test_wallet_credit_error_goes_to_manual_review_not_retry():
    wallet = FakeWallet(balance=90.0)
    wallet.fail_on_credit = True
    service, repo, _ = _refund_service(order=_order(payment_type=PaymentTypeEnum.WALLET), wallet=wallet)
    assert service.handle_wallet_hub_failure(_order(), _failure(True), 10.0, "USD") == RefundResult.MANUAL_REVIEW
    assert repo.status() == OrderRefundStatusEnum.MANUAL_REVIEW
    assert service.retry_failed_refund(ORDER_ID) == RefundResult.ALREADY_HANDLED   # never auto-retried


# =========================================================================== 5. DCB / LinkAja: manual review


@pytest.mark.parametrize("payment_type", [PaymentTypeEnum.DCB, PaymentTypeEnum.LINK_AJA])
def test_dcb_and_linkaja_are_flagged_never_refunded(payment_type):
    service, repo, executor = _refund_service()
    with patch.object(stripe.Refund, "create") as refund:
        assert service.flag_hub_failure_for_review(_order(), payment_type, failure=_failure(True)) \
               == RefundResult.MANUAL_REVIEW
    refund.assert_not_called()
    assert repo.status() == OrderRefundStatusEnum.MANUAL_REVIEW
    assert repo.rows[ORDER_ID]["payment_type"] == payment_type
    assert len(executor.tasks) == 1


def test_refund_handling_never_raises_to_the_caller():
    service, repo, _ = _refund_service()
    service._OrderRefundService__refund_repo = MagicMock(insert_if_absent=MagicMock(side_effect=RuntimeError("db")))
    assert service.handle_card_hub_failure(_order(), _failure(True), PI_ID) == RefundResult.MANUAL_REVIEW


def test_has_refund_record_fails_safe():
    service, repo, _ = _refund_service()
    assert not service.has_refund_record(ORDER_ID)
    repo.insert_if_absent({"order_id": ORDER_ID, "user_id": USER_ID, "payment_type": "Card", "status": "refunded"})
    assert service.has_refund_record(ORDER_ID)
    service._OrderRefundService__refund_repo = MagicMock(get_by_order_id=MagicMock(side_effect=RuntimeError("db")))
    assert service.has_refund_record(ORDER_ID)               # unknown -> do not re-process


# =========================================================================== 6. Stripe webhook


@pytest.fixture
def callback_service():
    names = ["esim_hub_service_instance", "UserRepo", "UserOrderRepo", "UserProfileRepo", "UserProfileBundleRepo",
             "SyncService", "UserWalletService", "PromotionService", "BundleService", "TaskExecutor"]
    patchers = [patch(f"app.services.callback_service.{n}") for n in names]
    for p in patchers:
        p.start()
    try:
        from app.services.callback_service import CallbackService
        service = CallbackService()
    finally:
        for p in patchers:
            p.stop()
    refunds = MagicMock()
    refunds.has_refund_record.return_value = False
    service._CallbackService__order_refund_service = refunds
    return service, refunds


def _event(order_type=UserOrderType.ASSIGN, iccid=None, tax=True):
    metadata = {"env": os.getenv("ENVIRONMENT", "DEV"), "order_id": ORDER_ID, "user_id": USER_ID,
                "bundle_code": "TEST123", "order_type": order_type, "rule_id": "0"}
    if iccid:
        metadata["iccid"] = iccid
    if tax:
        metadata["tax_calculation"] = "taxcalc_1"
    return {"type": "payment_intent.succeeded", "data": {"object": {"id": PI_ID, "metadata": metadata}}}


def _run_webhook(service, event):
    return service._CallbackService__handle_payment_webhook_data(event=event)


@pytest.mark.parametrize("order_type,method,iccid", [(UserOrderType.ASSIGN, "buy_bundle", None),
                                                     (UserOrderType.BUNDLE_TOP_UP, "top_up_bundle", "8900")])
def test_webhook_hub_failure_hands_the_order_to_the_refund_service(callback_service, order_type, method, iccid):
    service, refunds = callback_service
    order = _order(order_type=order_type)
    service._CallbackService__user_order_repo.get_by_id.return_value = order
    failure = _failure(True)
    setattr(service._CallbackService__bundle_service, method, AsyncMock(return_value=failure))
    with patch.object(stripe.tax.Transaction, "create_from_calculation", return_value=SimpleNamespace(id=TAX_TXN_ID)):
        result = _run_webhook(service, _event(order_type, iccid))
    assert result is failure                                   # same value returned as before
    refunds.handle_card_hub_failure.assert_called_once_with(order=order, failure=failure, payment_intent_id=PI_ID,
                                                            tax_transaction_id=TAX_TXN_ID)


def test_webhook_raise_after_payment_is_flagged_and_re_raised(callback_service):
    service, refunds = callback_service
    service._CallbackService__user_order_repo.get_by_id.return_value = _order()
    service._CallbackService__bundle_service.buy_bundle = AsyncMock(side_effect=RuntimeError("db down"))
    with pytest.raises(RuntimeError):
        _run_webhook(service, _event(tax=False))
    assert refunds.handle_card_hub_failure.call_args.kwargs["failure"] is None
    assert isinstance(refunds.handle_card_hub_failure.call_args.kwargs["error"], RuntimeError)


def test_webhook_success_is_unchanged(callback_service):
    service, refunds = callback_service
    service._CallbackService__user_order_repo.get_by_id.return_value = _order()
    success = ResponseHelper.success_response()
    service._CallbackService__bundle_service.buy_bundle = AsyncMock(return_value=success)
    with patch.object(stripe.tax.Transaction, "create_from_calculation", return_value=SimpleNamespace(id=TAX_TXN_ID)):
        assert _run_webhook(service, _event()) is success
    refunds.handle_card_hub_failure.assert_not_called()
    refunds.has_refund_record.assert_not_called()             # no extra read for a pending order


def test_webhook_redelivery_for_a_fulfilled_order_is_skipped(callback_service):
    service, refunds = callback_service
    service._CallbackService__user_order_repo.get_by_id.return_value = _order(order_status=OrderStatusEnum.SUCCESS)
    service._CallbackService__bundle_service.buy_bundle = AsyncMock()
    with patch.object(stripe.tax.Transaction, "create_from_calculation") as tax:
        assert _run_webhook(service, _event()).status == "success"
    service._CallbackService__bundle_service.buy_bundle.assert_not_called()
    tax.assert_not_called()


def test_webhook_redelivery_for_a_refunded_order_is_skipped(callback_service):
    service, refunds = callback_service
    service._CallbackService__user_order_repo.get_by_id.return_value = _order(order_status=OrderStatusEnum.FAILURE)
    refunds.has_refund_record.return_value = True
    service._CallbackService__bundle_service.buy_bundle = AsyncMock()
    assert _run_webhook(service, _event(tax=False)).status == "success"
    service._CallbackService__bundle_service.buy_bundle.assert_not_called()
    refunds.handle_card_hub_failure.assert_not_called()


def test_webhook_redelivery_end_to_end_refunds_once(callback_service):
    """Two deliveries of the same event with a real refund service: one Stripe refund."""
    service, _ = callback_service
    refund_service, repo, _ = _refund_service()
    service._CallbackService__order_refund_service = refund_service
    state = {"order": _order()}
    service._CallbackService__user_order_repo.get_by_id.side_effect = lambda _id: state["order"]

    async def failing_buy_bundle(**kwargs):
        state["order"] = _order(order_status=OrderStatusEnum.FAILURE)
        return _failure(True)

    service._CallbackService__bundle_service.buy_bundle = AsyncMock(side_effect=failing_buy_bundle)
    with patch.object(stripe.Refund, "create", return_value=_stripe_refund()) as refund:
        _run_webhook(service, _event(tax=False))
        _run_webhook(service, _event(tax=False))
    assert refund.call_count == 1
    assert service._CallbackService__bundle_service.buy_bundle.await_count == 1   # second delivery skipped


# =========================================================================== 7. Wallet API response


@pytest.fixture
def wallet_purchase():
    names = ["esim_hub_service_instance", "NotificationRepo", "UserOrderRepo", "UserProfileRepo",
             "UserProfileBundleRepo", "UserWalletService", "PromotionService", "BundleService",
             "dcb_service_instance", "link_aja_service_instance", "CurrencyService", "UserRepo", "TaskExecutor",
             "BundleTranslationRepo", "TagRepo"]
    patchers = [patch(f"app.services.user_service.{n}") for n in names]
    for p in patchers:
        p.start()
    try:
        from app.services.user_service import UserBundleService
        service = UserBundleService()
    finally:
        for p in patchers:
            p.stop()
    wallet = FakeWallet(balance=100.0)
    service._UserBundleService__user_wallet_service = wallet
    service._UserBundleService__currency_service.get_currency_rate.return_value = 1.0
    refund_service, repo, _ = _refund_service(order=_order(payment_type=PaymentTypeEnum.WALLET), wallet=wallet)
    service._UserBundleService__order_refund_service = refund_service
    return service, wallet, repo


async def _pay_with_wallet(service, order_type=UserOrderType.ASSIGN):
    order = _order(payment_type=PaymentTypeEnum.WALLET, order_type=order_type)
    return await service._UserBundleService__handle_wallet_payment(
        user=SimpleNamespace(id=USER_ID), bundle=_bundle(), user_order=order, rule_id="0", modified_amount=10.0,
        iccid="8900" if order_type == UserOrderType.BUNDLE_TOP_UP else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type,method", [(UserOrderType.ASSIGN, "buy_bundle"),
                                               (UserOrderType.BUNDLE_TOP_UP, "top_up_bundle")])
async def test_wallet_hub_failure_credits_back_and_returns_the_failure(wallet_purchase, order_type, method):
    service, wallet, repo = wallet_purchase
    setattr(service._UserBundleService__bundle_service, method, AsyncMock(return_value=_failure(True)))
    with pytest.raises(OrderNotFulfilledException) as raised:
        await _pay_with_wallet(service, order_type)
    assert raised.value.code == 400 and raised.value.name == ErrorMessages.ORDER_FAILED_REFUNDED   # not COMPLETED
    assert wallet.balance == 100.0                                  # debit 10, credit 10
    assert [c["amount"] for c in wallet.calls] == [-10.0, 10.0]
    assert repo.status() == OrderRefundStatusEnum.REFUNDED


@pytest.mark.asyncio
async def test_wallet_ambiguous_failure_is_under_review_and_not_credited(wallet_purchase):
    service, wallet, repo = wallet_purchase
    service._UserBundleService__bundle_service.buy_bundle = AsyncMock(return_value=_failure(False))
    with pytest.raises(OrderNotFulfilledException) as raised:
        await _pay_with_wallet(service)
    assert raised.value.name == ErrorMessages.ORDER_UNDER_REVIEW
    assert wallet.balance == 90.0
    assert repo.status() == OrderRefundStatusEnum.MANUAL_REVIEW


@pytest.mark.asyncio
async def test_wallet_raise_after_debit_is_under_review(wallet_purchase):
    service, wallet, repo = wallet_purchase
    service._UserBundleService__bundle_service.buy_bundle = AsyncMock(side_effect=RuntimeError("db down"))
    with pytest.raises(OrderNotFulfilledException) as raised:
        await _pay_with_wallet(service)
    assert raised.value.name == ErrorMessages.ORDER_UNDER_REVIEW
    assert repo.status() == OrderRefundStatusEnum.MANUAL_REVIEW


@pytest.mark.asyncio
async def test_wallet_debit_failure_is_unchanged(wallet_purchase):
    service, wallet, repo = wallet_purchase
    wallet.add_wallet_transaction = MagicMock(side_effect=RuntimeError("wallet down"))
    service._UserBundleService__bundle_service.buy_bundle = AsyncMock()
    from app.exceptions import CustomException
    with pytest.raises(CustomException) as raised:
        await _pay_with_wallet(service)
    assert raised.value.name == ErrorMessages.REQUEST_FAILED
    service._UserBundleService__bundle_service.buy_bundle.assert_not_called()
    assert repo.rows == {}


@pytest.mark.asyncio
async def test_wallet_success_is_unchanged(wallet_purchase):
    service, wallet, repo = wallet_purchase
    service._UserBundleService__bundle_service.buy_bundle = AsyncMock(return_value=ResponseHelper.success_response())
    response = await _pay_with_wallet(service)
    assert response.data.payment_status == PaymentStatusEnum.COMPLETED
    assert wallet.balance == 90.0
    assert repo.rows == {}


# =========================================================================== 8. DCB and LinkAja wiring


@pytest.mark.asyncio
async def test_dcb_hub_failure_is_flagged_for_review(wallet_purchase):
    service, _, _ = wallet_purchase
    refunds = MagicMock()
    service._UserBundleService__order_refund_service = refunds
    order = _order(payment_type=PaymentTypeEnum.DCB, otp="1234")
    service._UserBundleService__user_order_repo.get_by_id.return_value = order
    service._UserBundleService__is_order_otp_expired = MagicMock(return_value=False)
    service._UserBundleService__currency_service.convert.return_value = 10.0
    service._UserBundleService__user_repo.get_by_id.return_value = SimpleNamespace(metadata={"language": "en"})
    service._UserBundleService__dcb_service.deduct_balance = AsyncMock(return_value=True)
    failure = _failure(True)
    service._UserBundleService__bundle_service.buy_bundle = AsyncMock(return_value=failure)
    result = await service.verify_order_otp(SimpleNamespace(id=USER_ID, msisdn="961"),
                                            SimpleNamespace(order_id=ORDER_ID, otp="1234", iccid=None))
    assert result is failure                                   # response unchanged
    refunds.flag_hub_failure_for_review.assert_called_once_with(order=order, payment_type=PaymentTypeEnum.DCB,
                                                                failure=failure)


@pytest.mark.asyncio
async def test_linkaja_hub_failure_is_flagged_for_review():
    from app.services.link_aja_callback_service import LinkAjaCallbackService
    failure = _failure(True)
    bundle_service = MagicMock(buy_bundle=AsyncMock(return_value=failure))
    refunds = MagicMock()
    service = LinkAjaCallbackService(link_aja_service=MagicMock(), user_order_repo=MagicMock(),
                                     bundle_service=bundle_service, task_executor=MagicMock(),
                                     order_refund_service=refunds)
    order = _order(payment_type=PaymentTypeEnum.LINK_AJA)
    assert await service.provision(order) is failure
    refunds.flag_hub_failure_for_review.assert_called_once_with(order=order, payment_type=PaymentTypeEnum.LINK_AJA,
                                                                failure=failure)
