"""Test harness for the MCP purchase feature.

Everything external is replaced before the FastAPI application is imported:
Supabase (in-process fake), the eSIM Hub (stub), SMTP (mock) and FCM (patched by
the root conftest). No test in this package can reach Supabase, Stripe, the eSIM
Hub, QA or production.
"""

import os
from datetime import datetime, timedelta, timezone
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

from app.api.v1 import callback, mcp_user_bundle, mcp_user_bundle_card, user_bundle  # noqa: E402
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


CARD_SUCCESS_URL = "https://checkout.example.test/success"
CARD_CANCEL_URL = "https://checkout.example.test/cancel"


class FakeStripeCheckoutGateway:
    """In-process stand-in for Stripe Checkout. No network, ever.

    Reproduces the two behaviours the design depends on:
      * idempotency - the same idempotency_key returns the SAME session rather than
        creating a second one;
      * failure modes - ambiguous (timeout) vs terminal, as typed gateway errors.
    """

    def __init__(self):
        self.created = []                 # every accepted create call
        self.sessions = {}                # idempotency_key -> CheckoutSessionResult
        self.by_id = {}                   # session_id -> CheckoutSessionResult
        self.fail_with = None             # StripeGatewayError to raise
        self.expired_sessions = []
        #: Tests set this negative to simulate an already-lapsed session.
        self.expiry_minutes = 30
        self._counter = 0

    def create_checkout_session(self, *, amount_minor, currency, product_name, metadata,
                                idempotency_key, client_reference_id, customer_email=None):
        from app.services.mcp_stripe_gateway import CheckoutSessionResult

        if self.fail_with is not None:
            error, self.fail_with = self.fail_with, None
            raise error

        if idempotency_key in self.sessions:
            # Stripe replays the original session for a repeated idempotency key.
            return self.sessions[idempotency_key]

        self._counter += 1
        session_id = f"cs_test_{self._counter:04d}"
        # mode=payment creates the PaymentIntent up front, so its id is known here -
        # which is what lets the webhook resolve a payment_intent.* event to a checkout.
        result = CheckoutSessionResult(
            session_id=session_id,
            checkout_url=f"https://checkout.stripe.com/c/pay/{session_id}",
            payment_intent_id=f"pi_test_{self._counter:04d}",
            amount_total_minor=int(amount_minor),
            currency=str(currency).lower(),
            # Relative, like Stripe's: a fixed literal would silently drift into the
            # past and make every checkout look expired.
            expires_at=(datetime.now(tz=timezone.utc)
                        + timedelta(minutes=self.expiry_minutes)).isoformat(),
            status="open",
            payment_status="unpaid")
        self.created.append({"amount_minor": amount_minor, "currency": currency,
                             "metadata": dict(metadata), "idempotency_key": idempotency_key,
                             "client_reference_id": client_reference_id,
                             "customer_email": customer_email, "product_name": product_name,
                             "session_id": session_id})
        self.sessions[idempotency_key] = result
        self.by_id[session_id] = result
        return result

    def retrieve_session(self, session_id):
        return self.by_id.get(session_id)

    def expire_session(self, session_id):
        self.expired_sessions.append(session_id)
        return self.by_id.get(session_id)


@pytest.fixture
def stripe_gateway(monkeypatch):
    """Install the fake gateway into the live card service instance."""
    gateway = FakeStripeCheckoutGateway()
    monkeypatch.setattr(mcp_user_bundle_card.service,
                        "_McpCardCheckoutService__gateway", gateway, raising=False)
    return gateway


@pytest.fixture
def card_hub(hub, monkeypatch):
    """Point the card service and the card webhook service at the eSIM Hub stub."""
    monkeypatch.setattr(mcp_user_bundle_card.service,
                        "_McpCardCheckoutService__esim_hub_service", hub, raising=False)
    webhook = callback.service._CallbackService__mcp_card_webhook
    bundle_service = webhook._McpCardWebhookService__bundle_service
    monkeypatch.setattr(bundle_service, "_BundleService__esim_hub_service", hub, raising=False)
    monkeypatch.setattr(bundle_service._BundleService__task_executor, "add_task", lambda task: True)
    monkeypatch.setattr("app.services.bundle_service.send_email", MagicMock(), raising=False)
    return hub


@pytest.fixture
def card_webhook():
    """The live MCP card webhook service used by the shared callback route."""
    return callback.service._CallbackService__mcp_card_webhook


@pytest.fixture
def mcp_card_enabled(monkeypatch, no_ambient_hash_secret):
    """Enable MCP card checkout with a complete, valid configuration."""
    monkeypatch.setenv("MCP_CARD_PURCHASE_ENABLED", "true")
    monkeypatch.setenv("MCP_IDEMPOTENCY_HASH_SECRET", VALID_HASH_SECRET)
    monkeypatch.setenv("SYSTEM_CURRENCY", "USD")
    monkeypatch.setenv("MCP_CARD_SUCCESS_URL", CARD_SUCCESS_URL)
    monkeypatch.setenv("MCP_CARD_CANCEL_URL", CARD_CANCEL_URL)
    monkeypatch.setenv("MCP_CARD_SESSION_EXPIRY_MINUTES", "30")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake_key_for_unit_tests_only")
    return True


def card_body(bundle_code: str = BUNDLE_CODE, **overrides) -> dict:
    body = {
        "bundle_code": bundle_code,
        "quote_reference": "mcp-quote-card-1",
        "related_search": {"region": None,
                           "countries": [{"iso3_code": "FRA", "country_name": "France"}]},
    }
    body.update(overrides)
    return body


def post_card(client, body: dict | None = None, **header_kwargs):
    return client.post("/api/v1/mcp/user/bundle/card/checkout",
                       json=body if body is not None else card_body(),
                       headers=mcp_headers(**header_kwargs))


def get_card_status(client, payment_reference: str, token: str = USER_A_TOKEN):
    return client.get(f"/api/v1/mcp/user/bundle/card/status/{payment_reference}",
                      headers={"Authorization": f"Bearer {token}", "X-Device-Id": "device-1"})


def stripe_event(event_type: str, session: dict, event_id: str = "evt_test_0001") -> dict:
    """Build a Stripe-shaped event. Used only with the verified-webhook entry point."""
    return {"id": event_id, "type": event_type, "data": {"object": session}}


def payment_intent(db, payment_reference: str, *, status: str = "succeeded",
                   amount: int | None = None, currency: str = "usd",
                   metadata_overrides: dict | None = None, intent_id: str | None = None) -> dict:
    """Build a PaymentIntent payload mirroring the stored checkout record.

    This is the object the events we consume actually carry - the same event type and
    metadata payload the legacy Card flow produces, plus the mcp_source marker.
    """
    record = next(row for row in db.tables["mcp_card_checkout"] if row["id"] == payment_reference)
    order = next((row for row in db.tables.get("user_order", [])
                  if row["id"] == record["order_id"]), {})
    metadata = {
        "mcp_source": "mcp_card_checkout_v1",
        "order_id": record["order_id"],
        "user_id": record["user_id"],
        "device_id": "device-1",
        "bundle_code": record["bundle_code"],
        "order_type": order.get("order_type", "Assign"),
        "env": os.getenv("ENVIRONMENT", "DEV"),
        "rule_id": "0",
        "amount": str(record["amount_minor"]),
        "checkout_id": record["id"],
        "quote_reference": record["quote_reference"],
    }
    if metadata_overrides:
        metadata.update(metadata_overrides)
    resolved_amount = record["amount_minor"] if amount is None else amount
    return {
        "id": intent_id or record.get("stripe_payment_intent_id") or "pi_test_0001",
        "object": "payment_intent",
        "status": status,
        "amount": resolved_amount,
        "amount_received": resolved_amount if status == "succeeded" else 0,
        "currency": currency,
        "metadata": metadata,
    }


#: Backwards-compatible alias: the tests speak in terms of "the object on the event".
checkout_session = payment_intent


def legacy_payment_intent(order_id: str = "order-1", user_id: str = USER_A_ID) -> dict:
    """A PaymentIntent shaped like the LEGACY flow's - no mcp_source marker."""
    return {
        "id": "pi_legacy_0001",
        "object": "payment_intent",
        "status": "succeeded",
        "amount": 1000,
        "currency": "usd",
        "metadata": {
            "order_id": order_id, "user_id": user_id, "device_id": "device-1",
            "bundle_code": BUNDLE_CODE, "order_type": "Assign",
            "env": os.getenv("ENVIRONMENT", "DEV"), "rule_id": "0", "amount": "1000",
        },
    }


@pytest.fixture
def client():
    # TestClient is not used as a context manager on purpose: that keeps the
    # application lifespan (and its background scheduler) from starting.
    return TestClient(esim_app, raise_server_exceptions=False)


#: A secret that satisfies every rule in ``hash_secret_problem`` (>= 32 chars, not a
#: placeholder, not one repeated character). Test-only value.
VALID_HASH_SECRET = "f3a91c7d5e2b48a06c1d9f7e3b5a2c84d6019fbe7a3c5d2e"


@pytest.fixture
def mcp_enabled(monkeypatch, no_ambient_hash_secret):
    """Enable MCP with a valid configuration: the flag AND a usable hash secret.

    Depends on ``no_ambient_hash_secret`` so the clean-slate delenv always runs first,
    regardless of pytest's autouse ordering.
    """
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", "true")
    monkeypatch.setenv("MCP_IDEMPOTENCY_HASH_SECRET", VALID_HASH_SECRET)
    monkeypatch.setenv("MCP_IDEMPOTENCY_PROCESSING_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("SYSTEM_CURRENCY", "USD")
    return True


@pytest.fixture(autouse=True)
def no_ambient_hash_secret(monkeypatch):
    """Start every test from a clean slate.

    ``app.config.config`` calls ``load_dotenv()`` at import, so whatever the developer
    happens to have in their local ``.env`` leaks into the test process. Every MCP flag
    and secret is therefore cleared here and must be opted into explicitly by a fixture,
    so a test can never pass (or fail) because of an ambient deployment value.
    """
    for name in ("MCP_IDEMPOTENCY_HASH_SECRET", "MCP_PURCHASE_ENABLED",
                 "MCP_CARD_PURCHASE_ENABLED", "MCP_CARD_SUCCESS_URL", "MCP_CARD_CANCEL_URL",
                 "MCP_CARD_SESSION_EXPIRY_MINUTES"):
        monkeypatch.delenv(name, raising=False)


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
