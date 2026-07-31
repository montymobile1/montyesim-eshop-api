"""Test harness for the MCP purchase feature.

Everything external is replaced before the FastAPI application is imported:
Supabase (in-process fake), the eSIM Hub (stub), SMTP (mock) and FCM (patched by
the root conftest). No test in this package can reach Supabase, Stripe, the eSIM
Hub, QA or production.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy.dummy")
os.environ.setdefault("STRIPE_PUBLIC_KEY", "dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "dummy")
os.environ.setdefault("STRIPE_SECRET_KEY", "dummy")
os.environ.setdefault("SYSTEM_CURRENCY", "USD")
os.environ.setdefault("DEFAULT_CURRENCY", "USD")

import app.config.config as config_module  # noqa: E402
from tests.support import fake_supabase  # noqa: E402

# Must be installed before any repository is constructed (routers build services at import).
_CLIENT_PATCHER = patch.object(config_module, "create_client", fake_supabase.fake_create_client)
_CLIENT_PATCHER.start()

from fastapi.testclient import TestClient  # noqa: E402

from app.api.v1 import mcp_user_bundle, user_bundle  # noqa: E402
from app.main import esim_app  # noqa: E402
from app.schemas.esim_hub import EsimHubOrderResponse  # noqa: E402
from app.schemas.home import BundleCategoryDTO, BundleDTO, CountryDTO  # noqa: E402

# The patch had to be live while the application was imported so every repository
# built at import time captured the fake client permanently. Past this point it is
# re-installed per test (see ``fake_supabase_client``) so that collecting or running
# this package can never change how the rest of the suite behaves.
_CLIENT_PATCHER.stop()

USER_A_TOKEN = "test-token-user-a"
USER_B_TOKEN = "test-token-user-b"
ANONYMOUS_TOKEN = "test-token-anonymous"
USER_A_ID = "11111111-1111-1111-1111-111111111111"
USER_B_ID = "22222222-2222-2222-2222-222222222222"
ANONYMOUS_ID = "33333333-3333-3333-3333-333333333333"
BUNDLE_CODE = "bundle-0000-0000-0000-000000000001"
VALID_KEY = "a" * 40
OTHER_VALID_KEY = "b" * 40


def build_bundle(price: float = 10.0, is_active: bool = True, bundle_code: str = BUNDLE_CODE) -> BundleDTO:
    return BundleDTO(
        display_title="Test Bundle", display_subtitle="Subtitle", bundle_code=bundle_code,
        bundle_category=BundleCategoryDTO(code="COUNTRY", title="Country", type="COUNTRY"),
        bundle_marketing_name="Marketing", bundle_name="Bundle Name", count_countries=1,
        currency_code="USD", gprs_limit_display="1GB", original_price=price, price=price,
        price_display=f"{price} USD", unlimited=False, validity=30, validity_display="30 days",
        countries=[CountryDTO(country="France", country_code="FR", iso3_code="FRA", zone_name="",
                              id="france-id", alternative_country="france")],
        icon="https://placehold.co/400x400", is_active=is_active, is_stockable=True,
        bundle_info_code="info-code")


class EsimHubStub:
    """Stub eSIM Hub. Counts calls so tests can prove the price was re-fetched."""

    def __init__(self):
        self.bundle = build_bundle()
        self.applicable = True
        self.get_bundle_calls = 0
        self.create_order_calls = 0
        self.fail_provisioning = False
        self.raise_on_provisioning = False
        self.raise_on_get_bundle = False

    async def get_bundle_by_id(self, bundle_id: str, currency_code: str = "USD"):
        self.get_bundle_calls += 1
        if self.raise_on_get_bundle:
            raise OSError("hub unreachable")
        if self.bundle is None or self.bundle.bundle_code != bundle_id:
            return None
        return self.bundle

    async def check_bundle_applicable(self, bundle_id: str) -> bool:
        return self.applicable

    async def create_reseller_order(self, **kwargs):
        self.create_order_calls += 1
        if self.raise_on_provisioning:
            raise RuntimeError("hub exploded")
        if self.fail_provisioning:
            return None
        return EsimHubOrderResponse(
            orderId=f"hub-order-{self.create_order_calls}", totalAmount=10.0, title="Bundle",
            createdDate="2026-01-01T00:00:00", uniqueIdentifier="uid", displaySubTitle="sub",
            price=10.0, quantity=1, bundleGuid=BUNDLE_CODE, allowTopup=True, orderStatus="Completed",
            activationCode="TEST-ACTIVATION", smdpAdress="smdp.example.test", validityData="30",
            iccid="8900000000000000000")

    async def create_reseller_topup(self, **kwargs):  # pragma: no cover - not used by MCP
        return None


@pytest.fixture(autouse=True)
def fake_supabase_client():
    """Keep the fake Supabase client installed for the duration of one MCP test.

    Repositories captured the fake at import time, but code that resolves a client
    per call (token introspection) needs the patch live while the request runs.
    """
    with patch.object(config_module, "create_client", fake_supabase.fake_create_client):
        yield


@pytest.fixture(autouse=True)
def token_introspection(monkeypatch):
    """Use Supabase token introspection (our fake) rather than local JWT decoding."""
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)


@pytest.fixture
def db():
    """A clean in-memory database seeded with two users and their wallets."""
    database = fake_supabase.reset()
    database.auth_users[USER_A_TOKEN] = {"id": USER_A_ID, "email": "user-a@example.test",
                                         "user_metadata": {"email_verified": True}, "is_anonymous": False}
    database.auth_users[USER_B_TOKEN] = {"id": USER_B_ID, "email": "user-b@example.test",
                                         "user_metadata": {"email_verified": True}, "is_anonymous": False}
    database.auth_users[ANONYMOUS_TOKEN] = {"id": ANONYMOUS_ID, "email": None, "user_metadata": {},
                                            "is_anonymous": True}
    for user_id, email in ((USER_A_ID, "user-a@example.test"), (USER_B_ID, "user-b@example.test")):
        database.seed("users_copy", {"id": user_id, "email": email,
                                     "metadata": {"first_name": "Test", "last_name": "User",
                                                  "email": email, "language": "en"}})
        database.seed("user_wallet", {"id": f"wallet-{user_id}", "user_id": user_id, "amount": 100.0,
                                      "currency": "USD"})
    return database


@pytest.fixture
def hub(monkeypatch):
    """Replace every eSIM Hub client reachable from the MCP and legacy purchase paths."""
    stub = EsimHubStub()
    mcp_service = mcp_user_bundle.service
    bundle_service = mcp_service._McpPurchaseService__user_bundle_service
    monkeypatch.setattr(mcp_service, "_McpPurchaseService__esim_hub_service", stub, raising=False)
    for service in (bundle_service, user_bundle.service):
        monkeypatch.setattr(service, "_UserBundleService__esim_hub_service", stub, raising=False)
        monkeypatch.setattr(service._UserBundleService__bundle_service,
                            "_BundleService__esim_hub_service", stub, raising=False)
    # No SMTP and no background email/notification work leaking out of a test.
    monkeypatch.setattr("app.services.bundle_service.send_email", MagicMock(), raising=False)
    for service in (bundle_service, user_bundle.service):
        monkeypatch.setattr(service._UserBundleService__task_executor, "add_task", lambda task: True)
        monkeypatch.setattr(service._UserBundleService__bundle_service._BundleService__task_executor,
                            "add_task", lambda task: True)
    return stub


@pytest.fixture
def client():
    # TestClient is not used as a context manager on purpose: that keeps the
    # application lifespan (and its background scheduler) from starting.
    return TestClient(esim_app, raise_server_exceptions=False)


@pytest.fixture
def mcp_enabled(monkeypatch):
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", "true")
    monkeypatch.setenv("MCP_IDEMPOTENCY_PROCESSING_TIMEOUT_SECONDS", "120")
    return True


def mcp_headers(token: str = USER_A_TOKEN, idempotency_key: str | None = VALID_KEY,
                device_id: str = "device-1") -> dict:
    headers = {"Authorization": f"Bearer {token}", "X-Device-Id": device_id}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def mcp_body(bundle_code: str = BUNDLE_CODE, **overrides) -> dict:
    body = {
        "bundle_code": bundle_code,
        "payment_type": "Wallet",
        "related_search": {"region": None,
                           "countries": [{"iso3_code": "FRA", "country_name": "France"}]},
        "quote_reference": "mcp-quote-reference-1",
    }
    body.update(overrides)
    return body


def post_mcp(client: TestClient, body: dict | None = None, **header_kwargs):
    return client.post("/api/v1/mcp/user/bundle/assign", json=body if body is not None else mcp_body(),
                       headers=mcp_headers(**header_kwargs))
