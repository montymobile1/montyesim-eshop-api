"""The MCP routes at the transport boundary.

What is pinned here is what an unauthenticated or dishonest caller actually gets back from
the running application: authentication is required on all three routes, the caller's
identity comes from the verified token and cannot be supplied in a body, and the contracts
are the ones the external MCP service already speaks.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

MCP_ASSIGN = "/api/v1/mcp/user/bundle/assign"
MCP_CHECKOUT = "/api/v1/mcp/user/bundle/card/checkout"
MCP_STATUS = "/api/v1/mcp/user/bundle/card/status/order-1"

DEVICE_HEADERS = {"X-Device-Id": "device-1", "X-Currency": "USD", "Accept-Language": "en"}


@pytest.fixture
def authenticated(esim_app, user):
    """Override the bearer dependency with a verified user, then restore it."""
    from app.dependencies.security import bearer_token

    esim_app.dependency_overrides[bearer_token] = lambda: user
    yield user
    esim_app.dependency_overrides.pop(bearer_token, None)


@pytest.fixture
def stub_route_services(monkeypatch):
    """Replace the route modules' service singletons so no work is actually done."""
    import app.api.v1.mcp_user_bundle as routes

    purchase = MagicMock(assign_wallet_bundle=AsyncMock())
    card = MagicMock(create_checkout=AsyncMock(), get_status=MagicMock())
    monkeypatch.setattr(routes, "purchase_service", purchase)
    monkeypatch.setattr(routes, "card_service", card)
    return purchase, card


# --- (6)(7) Authentication is required ------------------------------------------------


@pytest.mark.parametrize("method,url,body", [
    ("post", MCP_ASSIGN, {"bundle_code": "BUNDLE-1"}),
    ("post", MCP_CHECKOUT, {"bundle_code": "BUNDLE-1"}),
    ("get", MCP_STATUS, None),
])
def test_mcp_routes_require_authentication(client, stub_route_services, method, url, body):
    """(6)(7) No bearer token, no purchase -- and no service is reached."""
    purchase, card = stub_route_services

    response = client.request(method.upper(), url, json=body, headers=DEVICE_HEADERS)

    assert response.status_code == 401
    purchase.assign_wallet_bundle.assert_not_awaited()
    card.create_checkout.assert_not_awaited()
    card.get_status.assert_not_called()


@pytest.mark.parametrize("method,url,body", [
    ("post", MCP_ASSIGN, {"bundle_code": "BUNDLE-1"}),
    ("post", MCP_CHECKOUT, {"bundle_code": "BUNDLE-1"}),
    ("get", MCP_STATUS, None),
])
def test_mcp_routes_reject_a_malformed_bearer_token(client, stub_route_services, method, url, body):
    purchase, card = stub_route_services

    response = client.request(method.upper(), url, json=body,
                              headers={**DEVICE_HEADERS, "Authorization": "Bearer not-a-real-token"})

    assert response.status_code == 401
    purchase.assign_wallet_bundle.assert_not_awaited()
    card.create_checkout.assert_not_awaited()


# --- (8) Identity comes from the token, never from the request ------------------------


def test_wallet_route_passes_the_token_user_to_the_service(client, authenticated, stub_route_services):
    """(8) Whatever the body says, the service receives the token's user."""
    from app.schemas.mcp import McpAssignResponse
    from app.schemas.response import ResponseHelper

    purchase, _ = stub_route_services
    purchase.assign_wallet_bundle.return_value = ResponseHelper.success_data_response(
        McpAssignResponse(status="COMPLETED", order_id="order-1"), 0)

    response = client.post(MCP_ASSIGN, json={"bundle_code": "BUNDLE-1"},
                           headers={**DEVICE_HEADERS, "Authorization": "Bearer token",
                                    "Idempotency-Key": "key-1"})

    assert response.status_code == 200
    assert purchase.assign_wallet_bundle.await_args.kwargs["user"] is authenticated


@pytest.mark.parametrize("url", [MCP_ASSIGN, MCP_CHECKOUT])
def test_mcp_routes_refuse_a_body_carrying_an_identity(client, authenticated, stub_route_services, url):
    """(8) ``extra=forbid`` turns a smuggled ``user_id`` into a refusal, not a field."""
    purchase, card = stub_route_services

    response = client.post(url, json={"bundle_code": "BUNDLE-1", "user_id": "somebody-else"},
                           headers={**DEVICE_HEADERS, "Authorization": "Bearer token"})

    assert response.status_code == 400
    purchase.assign_wallet_bundle.assert_not_awaited()
    card.create_checkout.assert_not_awaited()


@pytest.mark.parametrize("smuggled", [
    {"card_number": "4242424242424242"},
    {"cvv": "123"},
    {"amount": 1},
    {"paid": True},
    {"checkout_url": "https://evil.test"},
    {"payment_type": "Card"},
])
def test_card_route_refuses_card_data_and_asserted_outcomes(client, authenticated,
                                                             stub_route_services, smuggled):
    """(16) None of these can reach the service; the whole request is refused."""
    _, card = stub_route_services

    response = client.post(MCP_CHECKOUT, json={"bundle_code": "BUNDLE-1", **smuggled},
                           headers={**DEVICE_HEADERS, "Authorization": "Bearer token"})

    assert response.status_code == 400
    card.create_checkout.assert_not_awaited()


def test_wallet_route_refuses_a_non_wallet_payment_type(client, authenticated, stub_route_services):
    purchase, _ = stub_route_services

    response = client.post(MCP_ASSIGN, json={"bundle_code": "BUNDLE-1", "payment_type": "Card"},
                           headers={**DEVICE_HEADERS, "Authorization": "Bearer token"})

    assert response.status_code == 400
    purchase.assign_wallet_bundle.assert_not_awaited()


# --- The contracts the external MCP service already speaks ---------------------------


def test_card_route_returns_the_documented_checkout_fields(client, authenticated, stub_route_services):
    from app.schemas.mcp_card import McpCardCheckoutResponse
    from app.schemas.response import ResponseHelper

    _, card = stub_route_services
    card.create_checkout.return_value = ResponseHelper.success_data_response(
        McpCardCheckoutResponse(payment_reference="order-1", order_id="order-1",
                                checkout_url="https://checkout.stripe.test/pay/cs_1",
                                amount="10.00", currency="USD",
                                expires_at="2030-01-01T00:00:00+00:00"), 0)

    body = client.post(MCP_CHECKOUT, json={"bundle_code": "BUNDLE-1", "quote_reference": "q-1"},
                       headers={**DEVICE_HEADERS, "Authorization": "Bearer token",
                                "Idempotency-Key": "key-1"}).json()

    data = body["data"]
    assert data["checkout_url"] == "https://checkout.stripe.test/pay/cs_1"
    assert data["payment_reference"] == "order-1"
    assert data["status"] == "PENDING"
    assert data["amount"] == "10.00"
    assert data["currency"] == "USD"
    assert data["expires_at"] == "2030-01-01T00:00:00+00:00"
    # Nothing sensitive travels in the envelope.
    for leaked in ("client_secret", "publishable_key", "session_id", "customer_id", "token"):
        assert leaked not in body["data"]


def test_status_route_returns_the_documented_status_fields(client, authenticated, stub_route_services):
    from app.schemas.mcp_card import McpCardStatusResponse
    from app.schemas.response import ResponseHelper

    _, card = stub_route_services
    card.get_status.return_value = ResponseHelper.success_data_response(
        McpCardStatusResponse(payment_reference="order-1", status="COMPLETED", order_id="order-1",
                              amount="10.00", currency="USD", bundle_code="BUNDLE-1",
                              paid=True, provisioned=True), 0)

    data = client.get(MCP_STATUS, headers={**DEVICE_HEADERS, "Authorization": "Bearer token"}).json()["data"]

    assert data["status"] == "COMPLETED"
    assert data["paid"] is True
    assert data["provisioned"] is True
    assert data["order_id"] == "order-1"
    card.get_status.assert_called_once()
    assert card.get_status.call_args.kwargs["payment_reference"] == "order-1"


def test_escalation_is_delivered_with_its_own_http_status(client, authenticated, stub_route_services):
    """A 424 escalation is not delivered as an HTTP 200."""
    from app.schemas.mcp import McpAssignResponse
    from app.schemas.response import ResponseHelper

    purchase, _ = stub_route_services
    envelope = ResponseHelper.success_data_response(
        McpAssignResponse(status="MANUAL_INTERVENTION_REQUIRED", order_id="order-1"), 0)
    envelope.status = "failed"
    envelope.responseCode = 424
    purchase.assign_wallet_bundle.return_value = envelope

    response = client.post(MCP_ASSIGN, json={"bundle_code": "BUNDLE-1"},
                           headers={**DEVICE_HEADERS, "Authorization": "Bearer token"})

    assert response.status_code == 424
    assert response.json()["data"]["status"] == "MANUAL_INTERVENTION_REQUIRED"


def test_disabled_flag_answers_503_on_mcp_routes_only(client, authenticated, monkeypatch):
    """(5) The flag reaches the MCP route and nothing else in the application."""
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", "false")

    response = client.post(MCP_ASSIGN, json={"bundle_code": "BUNDLE-1"},
                           headers={**DEVICE_HEADERS, "Authorization": "Bearer token"})

    assert response.status_code == 503
    # A legacy route is entirely unaffected: it still reaches its own dependencies and
    # answers from the legacy contract rather than from an MCP flag.
    legacy = client.post("/api/v1/user/bundle/assign", json={"bundle_code": "BUNDLE-1"},
                         headers=DEVICE_HEADERS)
    assert legacy.status_code != 503


def test_mcp_errors_never_leak_internals(client, authenticated, monkeypatch):
    """(9) A safe code and safe prose; no trace, token or provider message."""
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", "false")

    body = client.post(MCP_ASSIGN, json={"bundle_code": "BUNDLE-1"},
                       headers={**DEVICE_HEADERS, "Authorization": "Bearer token"}).json()

    serialized = str(body).lower()
    for leaked in ("traceback", "stripe_secret", "sk_live", "sk_test", "whsec_", "supabase_key",
                   "bearer token", "file \"", "/home/"):
        assert leaked not in serialized
