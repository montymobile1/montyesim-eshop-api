"""Fixtures for the MCP adapter tests.

Two guarantees are established here for every test in this package:

* **nothing reaches a real system.** ``block_all_network`` replaces ``socket.socket``
  with one that refuses to connect, so a repository, a Stripe call, a Supabase request or
  an eSIM hub request that slipped past a mock fails loudly rather than silently reaching
  the outside world. Nothing in this package writes to, reads from or migrates a database;
* **no service construction touches configuration storage.** ``get_config`` reads the
  ``app_config`` table in production, so it is stubbed here before any service is built.

Everything the tests then exercise is either a pure function or a service whose
collaborators are injected as mocks.
"""

import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config.db import OrderStatusEnum, PaymentTypeEnum
from app.models.user import UserModel, UserOrderModel, UserOrderType

#: Repository root, used to read the committed OpenAPI baseline and to grep the source
#: tree for forbidden database objects.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def executable_source(path: Path) -> str:
    """Return only the *executable* source of a module: no comments, no string literals.

    Several tests assert that a module contains no call to a particular function. Grepping
    raw text would make those assertions fail on a docstring that merely *names* the thing
    it promises not to call, so comments and strings are dropped before matching.
    """
    import io
    import tokenize

    kept = []
    with open(path, "rb") as handle:
        for token in tokenize.tokenize(io.BytesIO(handle.read()).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING, tokenize.FSTRING_START,
                              tokenize.FSTRING_MIDDLE, tokenize.FSTRING_END):
                continue
            kept.append(token.string)
    return "\n".join(kept)


@pytest.fixture(autouse=True)
def block_all_network(monkeypatch):
    """Make any outbound connection impossible for the duration of a test."""

    class _NoNetworkSocket(socket.socket):
        def connect(self, *args, **kwargs):  # pragma: no cover - only runs on a bug
            raise AssertionError("an MCP test attempted a real network connection")

        def connect_ex(self, *args, **kwargs):  # pragma: no cover - only runs on a bug
            raise AssertionError("an MCP test attempted a real network connection")

    monkeypatch.setattr(socket, "socket", _NoNetworkSocket)
    yield


@pytest.fixture(autouse=True)
def stub_app_config(monkeypatch):
    """Stop ``get_config`` reading the ``app_config`` table during construction."""
    import app.config.config as config_module
    import app.config.helper as helper_module

    def _stub(key, default_value=None):
        return default_value if default_value is not None else "stub"

    monkeypatch.setattr(helper_module, "get_config", _stub)
    monkeypatch.setattr(config_module, "get_config", _stub)
    yield


@pytest.fixture(autouse=True)
def mcp_environment(monkeypatch):
    """Both MCP flags on, and valid card redirect configuration.

    Tests that care about the *off* position turn a flag off explicitly, which is also how
    the fail-closed default is exercised.
    """
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", "true")
    monkeypatch.setenv("MCP_CARD_PURCHASE_ENABLED", "true")
    monkeypatch.setenv("MCP_CARD_SUCCESS_URL", "https://shop.example.test/mcp/paid")
    monkeypatch.setenv("MCP_CARD_CANCEL_URL", "https://shop.example.test/mcp/cancelled")
    monkeypatch.setenv("SYSTEM_CURRENCY", "USD")
    monkeypatch.setenv("ENVIRONMENT", "DEV")
    yield


@pytest.fixture
def user():
    """The authenticated caller. Always produced by the verified token, never by a body."""
    return UserModel(id="user-1", email="traveller@example.test", token="verified-token",
                     msisdn=None, is_verified=True, is_anonymous=False, anonymous_user_id=None)


@pytest.fixture
def other_user():
    return UserModel(id="user-2", email="somebody-else@example.test", token="verified-token",
                     msisdn=None, is_verified=True, is_anonymous=False, anonymous_user_id=None)


def make_order(order_id: str = "order-1", user_id: str = "user-1",
               payment_status=OrderStatusEnum.PENDING, order_status=OrderStatusEnum.PENDING,
               payment_type=PaymentTypeEnum.CARD, amount: float = 1000,
               bundle_id: str = "BUNDLE-1") -> UserOrderModel:
    """An in-memory ``user_order`` row. No database is involved at any point."""
    return UserOrderModel(id=order_id, user_id=user_id, bundle_id=bundle_id,
                          order_type=UserOrderType.ASSIGN, amount=amount, modified_amount=amount,
                          currency="USD", payment_status=payment_status, order_status=order_status,
                          payment_type=payment_type, bundle_data=None)


@pytest.fixture
def bundle():
    """An active, stockable bundle as the eSIM hub would return it."""
    from tests.mocks import get_bundle_mock

    plan = get_bundle_mock()
    plan.original_price = 10.0
    plan.is_active = True
    plan.is_stockable = True
    return plan


@pytest.fixture
def hub_service(bundle):
    return MagicMock(get_bundle_by_id=AsyncMock(return_value=bundle),
                     check_bundle_applicable=AsyncMock(return_value=True))


@pytest.fixture
def order_repo():
    repo = MagicMock()
    repo.create = MagicMock(return_value=make_order())
    repo.update_by = MagicMock()
    repo.get_first_by = MagicMock(return_value=None)
    return repo


@pytest.fixture
def profile_repo():
    return MagicMock(get_first_by=MagicMock(return_value=None))


@pytest.fixture
def currency_service():
    return MagicMock(get_currency_rate=MagicMock(return_value=1.0),
                     get_rate_by_currency=MagicMock(return_value=1.0),
                     convert=MagicMock(side_effect=lambda from_currency, to_currency, amount: amount))


@pytest.fixture
def stripe_session():
    """A Stripe Checkout Session as the provider returns it. Never a real one."""
    return SimpleNamespace(id="cs_test_1", url="https://checkout.stripe.test/pay/cs_test_1",
                           payment_intent="pi_test_1", expires_at=1893456000)


@pytest.fixture
def assign_flow():
    """A stand-in for ``UserBundleService.assign``, recording how it was called."""
    from app.schemas.bundle import PaymentIntentResponse
    from app.schemas.response import ResponseHelper

    return MagicMock(assign=AsyncMock(
        return_value=ResponseHelper.success_data_response(PaymentIntentResponse(order_id="order-1"), 0)))


@pytest.fixture
def purchase_service(assign_flow, order_repo, profile_repo):
    from app.services.mcp_purchase_service import McpPurchaseService

    return McpPurchaseService(user_bundle_service=assign_flow, user_order_repo=order_repo,
                              user_profile_repo=profile_repo)


@pytest.fixture
def card_service(hub_service, order_repo, profile_repo, currency_service):
    from app.services.mcp_card_service import McpCardCheckoutService

    return McpCardCheckoutService(esim_hub_service=hub_service, user_order_repo=order_repo,
                                  user_profile_repo=profile_repo, currency_service=currency_service)


@pytest.fixture(scope="session")
def legacy_openapi_baseline():
    """The OpenAPI paths and schemas as they were before the MCP adapter existed."""
    with open(Path(__file__).parent / "openapi_legacy_baseline.json", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="session")
def esim_app():
    """The live FastAPI application, imported without any configuration lookup.

    Importing ``app.main`` constructs every router's service at module scope, so
    ``get_config`` is stubbed for the duration of the import.
    """
    from unittest.mock import patch

    os.environ.setdefault("ENVIRONMENT", "DEV")
    import app.config.config as config_module
    import app.config.helper as helper_module

    def _stub(key, default_value=None):
        return default_value if default_value is not None else "stub"

    with patch.object(helper_module, "get_config", _stub), patch.object(config_module, "get_config", _stub):
        from app.main import esim_app as application

        return application


@pytest.fixture
def client(esim_app):
    """A transport-level client. The lifespan is deliberately not started, so the
    background scheduler never runs during a test."""
    from fastapi.testclient import TestClient

    return TestClient(esim_app, raise_server_exceptions=False)


@pytest.fixture(scope="session")
def openapi_spec():
    """The live OpenAPI document, imported without any configuration lookup.

    Importing ``app.main`` constructs every router's service at module scope, so
    ``get_config`` is stubbed for the duration of the import.
    """
    from unittest.mock import patch

    os.environ.setdefault("ENVIRONMENT", "DEV")
    import app.config.config as config_module
    import app.config.helper as helper_module

    def _stub(key, default_value=None):
        return default_value if default_value is not None else "stub"

    with patch.object(helper_module, "get_config", _stub), patch.object(config_module, "get_config", _stub):
        from app.main import esim_app

        return esim_app.openapi()
