"""HTTP contract of /api/v1/admin/audit-log (MS-5048). The real AuditQueryService runs; repositories are mocked."""
import json
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN
from app.dependencies.admin_security import admin_user
from app.exceptions.admin import AdminUpstreamError
from app.repo.admin.audit_log_repo import AuditSearch
from app.services.admin.audit_query_service import AuditQueryService
from tests.admin.conftest import make_admin

URL = "/api/v1/admin/audit-log"
ACTOR = "11111111-1111-1111-1111-111111111111"
GONE = "44444444-4444-4444-4444-444444444444"

API_ROW = {
    "id": 20, "table_name": "bundle", "operation": "UPDATE", "changed_by": ACTOR,
    "changed_at": "2026-10-20T10:00:00", "record_id": None,
    "old_data": {"id": 7, "bundle_name": "Europe 5GB", "is_active": True},
    "new_data": {"id": 7, "bundle_name": "Europe 5GB", "is_active": False},
    "operation_details": {"ip_address": "8.8.8.8", "reason": "supplier issue", "client_reported": False,
                          "record_id": "7", "rows_affected": 1},
}
LEGACY_BAN = {
    "id": 10, "table_name": "auth.users", "operation": "Ban User", "changed_by": GONE,
    "changed_at": "2025-05-01T10:00:00", "record_id": "55555555-5555-5555-5555-555555555555",
    "old_data": None, "new_data": None,
    "operation_details": {"ip_address": "1.2.3.4", "reason": "fraud", "hours_banned": 24},
}
LEGACY_BULK = {
    "id": 11, "table_name": "bundle", "operation": "Deactivate Bundle", "changed_by": ACTOR,
    "changed_at": "2025-05-02T10:00:00", "record_id": None,
    "old_data": json.dumps({"affected_bundle_ids": ["a", "b"], "status": "active"}),
    "new_data": json.dumps({"affected_bundle_ids": ["a", "b"], "status": "inactive"}),
    "operation_details": "not json at all",
}


def _as(role):
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(role)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def as_super():
    yield from _as(ROLE_SUPER_ADMIN)


@pytest.fixture
def as_admin():
    yield from _as(ROLE_ADMIN)


@pytest.fixture
def repos(monkeypatch):
    from app.api.v1.admin import audit_log as routes
    audit, users = MagicMock(), MagicMock()
    users.by_ids.return_value = {ACTOR: {"id": ACTOR, "email": "boss@example.com", "msisdn": None}}
    monkeypatch.setattr(routes, "service", AuditQueryService(audit=audit, users=users))
    return {"audit": audit, "users": users}


def test_list_projects_rows_and_facets(api_client, as_super, repos):
    repos["audit"].search.return_value = AuditSearch(rows=[API_ROW, LEGACY_BAN, LEGACY_BULK], total=57,
                                                     facets_by_table={"bundle": 40, "auth.users": 17})

    response = api_client.get(URL)

    assert response.status_code == 200
    body = response.json()
    assert body["totalCount"] == 57
    data = body["data"]
    assert data["facets"] == {"by_table": {"bundle": 40, "auth.users": 17}}
    api_row, ban, bulk = data["data"]
    assert api_row["changed_by"] == {"id": ACTOR, "email": "boss@example.com"}
    assert api_row["ip_address"] == "8.8.8.8" and api_row["reason"] == "supplier issue"
    assert api_row["rows_affected"] == 1 and api_row["record_id"] == "7"
    assert api_row["client_reported"] is False
    assert api_row["summary"] == "Europe 5GB"
    assert ban["operation"] == "BAN" and ban["legacy_operation"] == "Ban User"
    assert ban["changed_by"] is None  # deleted actor
    assert ban["client_reported"] is True
    assert ban["summary"] == "Banned for 24 hours"
    assert bulk["operation"] == "UPDATE"  # rows_affected unknown (malformed details) -> UPDATE
    assert bulk["ip_address"] is None and bulk["reason"] is None and bulk["rows_affected"] is None
    page = repos["audit"].search.call_args.args[0]
    assert (page.page, page.page_size) == (1, 20)


def test_list_passes_the_filters(api_client, as_super, repos):
    repos["audit"].search.return_value = AuditSearch(rows=[], total=0, facets_by_table={})

    response = api_client.get(URL, params={"table": "auth.users", "operation": "BAN", "changed_by": ACTOR,
                                           "from": "2026-01-01", "to": "2026-01-31", "page": 4, "page_size": 50})

    assert response.status_code == 200
    assert response.json()["data"]["data"] == []
    assert response.json()["data"]["total_count"] == 0
    kwargs = repos["audit"].search.call_args.kwargs
    assert kwargs["table"] == "auth.users" and kwargs["operation"] == "BAN" and kwargs["changed_by"] == ACTOR
    assert kwargs["changed_from"].isoformat() == "2026-01-01T00:00:00"
    assert kwargs["changed_to"].isoformat() == "2026-02-01T00:00:00"


@pytest.mark.parametrize("params", [
    {"table": "users;drop"}, {"table": "orders"}, {"operation": "TRUNCATE"}, {"operation": "Ban User"},
    {"from": "2026-02-01", "to": "2026-01-01"}, {"changed_by": "x"}, {"page_size": 101}, {"search": "x"},
])
def test_list_bad_params_are_422(api_client, as_super, repos, params):
    repos["audit"].search.return_value = AuditSearch(rows=[], total=0)
    assert api_client.get(URL, params=params).status_code == 422


def test_detail_parses_and_redacts(api_client, as_super, repos):
    repos["audit"].get.return_value = {
        "id": 30, "table_name": "app_config", "operation": "UPDATE", "changed_by": ACTOR,
        "changed_at": "2026-10-20T10:00:00", "record_id": None,
        "old_data": json.dumps([{"key": "SMTP_PASSWORD", "value": "p@ss"}, {"key": "A", "value": "1"}]),
        "new_data": {"A": "2", "STRIPE_SECRET_KEY": "sk_live"},
        "operation_details": {"ip_address": "9.9.9.9", "client_reported": False, "api_token": "t"},
    }

    response = api_client.get(f"{URL}/30")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["old_data"] == [{"key": "SMTP_PASSWORD", "value": "***"}, {"key": "A", "value": "1"}]
    assert data["new_data"] == {"A": "2", "STRIPE_SECRET_KEY": "***"}
    assert data["operation_details"]["api_token"] == "***"
    assert data["parse_error"] is False
    for secret in ("p@ss", "sk_live"):
        assert secret not in response.text


def test_detail_of_a_ban_has_hours(api_client, as_super, repos):
    repos["audit"].get.return_value = LEGACY_BAN
    data = api_client.get(f"{URL}/10").json()["data"]
    assert data["hours_banned"] == 24 and data["old_data"] is None and data["client_reported"] is True


def test_detail_unparsable_data_is_flagged(api_client, as_super, repos):
    repos["audit"].get.return_value = dict(LEGACY_BULK, old_data="{oops")
    data = api_client.get(f"{URL}/11").json()["data"]
    assert data["parse_error"] is True and data["old_data"] is None
    assert data["new_data"]["status"] == "inactive"


def test_detail_unknown_is_404(api_client, as_super, repos):
    repos["audit"].get.return_value = None
    assert api_client.get(f"{URL}/999").status_code == 404


@pytest.mark.parametrize("bad_id", ["abc", "-1", "0"])
def test_detail_malformed_id_is_422(api_client, as_super, repos, bad_id):
    assert api_client.get(f"{URL}/{bad_id}").status_code == 422


def test_requires_a_session(api_client, repos):
    assert api_client.get(URL).status_code == 401
    assert api_client.get(f"{URL}/1").status_code == 401


def test_is_super_admin_only(api_client, as_admin, repos):
    response = api_client.get(URL)
    assert response.status_code == 403
    assert response.json()["developerMessage"] == "admin.audit.read"
    assert api_client.get(f"{URL}/1").status_code == 403


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_no_write_route(api_client, as_super, repos, method):
    assert getattr(api_client, method)(URL).status_code == 405
    assert getattr(api_client, method)(f"{URL}/1").status_code == 405


def test_database_error_is_502(api_client, as_super, repos):
    repos["audit"].search.side_effect = AdminUpstreamError("DATABASE_ERROR")
    assert api_client.get(URL).status_code == 502
