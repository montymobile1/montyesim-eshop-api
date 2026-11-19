"""2026-11-06: GET /api/v1/admin/audit-log/lookup, the Audit log "Admin" filter: admin accounts only."""
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN
from app.dependencies.admin_security import admin_user
from app.schemas.admin.common import AdminPage, LookupItem
from tests.admin.conftest import make_admin

URL = "/api/v1/admin/audit-log/lookup"


@pytest.fixture
def lookup_service(monkeypatch):
    fake = MagicMock(name="lookup-service")
    fake.admins.return_value = AdminPage[LookupItem](
        data=[LookupItem(id="a1", label="boss@x.com", email="boss@x.com", role="super_admin")], total_count=1)
    monkeypatch.setattr("app.services.admin.lookup_service.service", fake)
    return fake


@pytest.fixture
def as_role(api_client):
    from app.main import esim_app

    def set_role(role):
        esim_app.dependency_overrides[admin_user] = lambda: make_admin(role)

    yield set_role
    esim_app.dependency_overrides.pop(admin_user, None)


def test_super_admin_gets_admin_accounts_only(api_client, as_role, lookup_service):
    as_role(ROLE_SUPER_ADMIN)

    response = api_client.get(URL, params={"search": "bo", "page": 1, "page_size": 20})

    assert response.status_code == 200
    assert response.json()["data"]["data"] == [
        {"id": "a1", "label": "boss@x.com", "email": "boss@x.com", "role": "super_admin"}]
    search, page, record_id = lookup_service.admins.call_args.args
    assert (search, page.page, page.page_size, record_id) == ("bo", 1, 20, None)
    lookup_service.users.assert_not_called()  # no longer every account


def test_restoring_a_label_passes_the_id(api_client, as_role, lookup_service):
    as_role(ROLE_SUPER_ADMIN)
    api_client.get(URL, params={"id": "11111111-1111-1111-1111-111111111111"})
    assert lookup_service.admins.call_args.args[2] == "11111111-1111-1111-1111-111111111111"


def test_same_permission_as_the_audit_log(api_client, as_role, lookup_service):
    as_role(ROLE_ADMIN)  # admin.audit.read is super admin only
    assert api_client.get(URL).status_code == 403
    lookup_service.admins.assert_not_called()
