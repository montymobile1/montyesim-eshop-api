from tests.mcp.conftest import post_mcp, mcp_body


def test_route_registered(client):
    paths = [r.path for r in client.app.routes]
    assert "/api/v1/mcp/user/bundle/assign" in paths
    assert "/api/v1/user/bundle/assign" in paths


def test_disabled_by_default(db, hub, client):
    response = post_mcp(client)
    print(response.status_code, response.json())
    assert response.status_code == 503


def test_happy_path(db, hub, client, mcp_enabled):
    response = post_mcp(client)
    print(response.status_code, response.json())
    assert response.status_code == 200


def test_dump(db, hub, client, mcp_enabled):
    r = post_mcp(client)
    print("STATUS", r.status_code)
    print("BODY", r.json())
    print("HEADERS", dict(r.headers))
    print("ORDERS", db.tables.get("user_order"))
    print("IDEMP", db.tables.get("mcp_purchase_idempotency"))
    print("TX", db.tables.get("user_wallet_transaction"))
    print("WALLET", db.tables.get("user_wallet"))
    print("PROFILE", db.tables.get("user_profile"))
