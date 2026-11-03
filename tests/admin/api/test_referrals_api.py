"""HTTP contract of /api/v1/admin/referrals (MS-5056)."""
import json
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN
from app.dependencies.admin_security import AdminUser, admin_user
from app.exceptions.admin import AdminUpstreamError, AdminValidationError
from app.schemas.admin.common import AdminPage
from app.services.admin.referral_service import XLSX_MEDIA_TYPE, ReferralExport
from tests.admin.conftest import make_admin

BASE = "/api/v1/admin/referrals"
USER_ID = "33333333-3333-3333-3333-333333333333"

ROW = {
    "id": "0a0a0a0a-0000-0000-0000-000000000001", "created_at": "2026-10-20T10:00:00",
    "payment_time": "2026-10-20T10:05:00", "referral_code": "ABC123", "amount": 2.5, "currency": "EUR",
    "modified_amount": 9.5, "order_status": "success", "status": "completed",
    "referrer": {"id": "r1", "email": "owner@example.com"},
    "referred": {"id": USER_ID, "email": "buyer@example.com"},
}


@pytest.fixture
def service(monkeypatch):
    from app.api.v1.admin import referrals as routes
    fake = MagicMock(name="referral-service")
    monkeypatch.setattr(routes, "service", fake)
    return fake


def _as(role):
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(role)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def as_admin():
    yield from _as(ROLE_ADMIN)


@pytest.fixture
def as_super():
    yield from _as(ROLE_SUPER_ADMIN)


@pytest.fixture
def as_no_read():
    """An admin without admin.referrals.read."""
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: AdminUser(
        id="22222222-2222-2222-2222-222222222222", email="other@example.com", role=ROLE_ADMIN,
        permissions=frozenset({"admin.orders.read"}), token="t")
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


def _failed(response, status):
    body = response.json()
    assert response.status_code == status
    assert body["status"] == "failed"
    assert body["responseCode"] == status
    return body


def _code(response):
    return json.loads(response.json()["developerMessage"])["code"]


# ---------- list ----------

def test_list_passes_filters_and_page_and_returns_envelope(api_client, service, as_admin):
    service.list.return_value = AdminPage(data=[ROW], total_count=1)

    response = api_client.get(BASE, params={"user_id": USER_ID, "search": "abc_%", "from_email": "buyer@",
                                            "date": "2026-10-20", "page": 2, "page_size": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["totalCount"] == 1
    assert body["data"]["total_count"] == 1
    row = body["data"]["data"][0]
    assert row["referrer"] == {"id": "r1", "email": "owner@example.com"}
    assert row["referred"] == {"id": USER_ID, "email": "buyer@example.com"}
    assert row["amount"] == 2.5 and row["referral_code"] == "ABC123"
    filters, page = service.list.call_args.args
    assert str(filters.user_id) == USER_ID
    assert (filters.search, filters.from_email, filters.date.isoformat()) == ("abc_%", "buyer@", "2026-10-20")
    assert (page.page, page.page_size) == (2, 5)


def test_list_defaults(api_client, service, as_admin):
    service.list.return_value = AdminPage(data=[], total_count=0)

    assert api_client.get(BASE).status_code == 200
    filters, page = service.list.call_args.args
    assert (filters.user_id, filters.search, filters.from_email, filters.date) == (None, None, None, None)
    assert (page.page, page.page_size) == (1, 20)


@pytest.mark.parametrize("params", [
    {"page_size": 101}, {"page": 0}, {"foo": "bar"}, {"date": "2026-02-30"}, {"date": "yesterday"},
    {"user_id": "not-a-uuid"}, {"search": "x" * 101},
])
def test_list_rejects_bad_query(api_client, service, as_admin, params):
    _failed(api_client.get(BASE, params=params), 422)
    service.list.assert_not_called()


def test_list_without_session_is_401(api_client, service):
    _failed(api_client.get(BASE), 401)
    service.list.assert_not_called()


def test_list_without_read_permission_is_403(api_client, service, as_no_read):
    _failed(api_client.get(BASE), 403)
    service.list.assert_not_called()


def test_list_database_error_is_502(api_client, service, as_admin):
    service.list.side_effect = AdminUpstreamError("DATABASE_ERROR")
    _failed(api_client.get(BASE), 502)


# ---------- export ----------

def test_export_streams_xlsx_attachment_with_audit_context(api_client, service, as_super):
    service.export.return_value = ReferralExport(content=b"PK\x03\x04fake", row_count=3,
                                                 file_name="referrals-20261021-120000.xlsx")

    response = api_client.post(f"{BASE}/export", json={"search": "abc", "date": "2026-10-20"},
                               headers={"X-Forwarded-For": "9.9.9.9"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(XLSX_MEDIA_TYPE)
    assert response.headers["content-disposition"] == 'attachment; filename="referrals-20261021-120000.xlsx"'
    assert response.headers["x-export-row-count"] == "3"
    assert response.content == b"PK\x03\x04fake"
    body, ctx = service.export.call_args.args
    assert (body.search, body.date.isoformat(), body.user_id, body.from_email) == ("abc", "2026-10-20", None, None)
    assert ctx.actor_id == "11111111-1111-1111-1111-111111111111"
    assert ctx.ip == "9.9.9.9"


def test_export_with_empty_body_exports_everything(api_client, service, as_super):
    service.export.return_value = ReferralExport(content=b"x", row_count=0, file_name="referrals.xlsx")

    assert api_client.post(f"{BASE}/export", json={}).status_code == 200
    body = service.export.call_args.args[0]
    assert body.audit_filters() == {}


def test_export_over_cap_is_422_too_many_rows(api_client, service, as_super):
    service.export.side_effect = AdminValidationError(json.dumps({"code": "too_many_rows", "total": 60000,
                                                                  "cap": 50000}))

    response = api_client.post(f"{BASE}/export", json={})

    _failed(response, 422)
    assert _code(response) == "too_many_rows"
    assert json.loads(response.json()["developerMessage"])["cap"] == 50000


@pytest.mark.parametrize("payload", [
    {"date": "2026-02-30"}, {"user_id": "nope"}, {"page": 1}, {"search": "x" * 101}, {"extra": True},
])
def test_export_invalid_body_is_422(api_client, service, as_super, payload):
    _failed(api_client.post(f"{BASE}/export", json=payload), 422)
    service.export.assert_not_called()


def test_export_by_a_regular_admin_is_403(api_client, service, as_admin):
    """admin.referrals.export is super admin only; the list permission is not enough."""
    _failed(api_client.post(f"{BASE}/export", json={}), 403)
    service.export.assert_not_called()


def test_export_without_session_is_401(api_client, service):
    _failed(api_client.post(f"{BASE}/export", json={}), 401)
    service.export.assert_not_called()


def test_export_audit_failure_is_502_without_file(api_client, service, as_super):
    service.export.side_effect = AdminUpstreamError("DATABASE_ERROR")

    response = api_client.post(f"{BASE}/export", json={})

    _failed(response, 502)
    assert "content-disposition" not in response.headers
