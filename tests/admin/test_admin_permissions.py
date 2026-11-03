import pytest

from app.config.admin_permissions import (
    ALL_PERMISSIONS, ROLE_ADMIN, ROLE_SUPER_ADMIN, SUPER_ONLY, permissions_for,
)


def test_super_admin_has_every_permission():
    assert permissions_for(ROLE_SUPER_ADMIN) == ALL_PERMISSIONS


def test_admin_has_none_of_the_super_admin_only_permissions():
    assert permissions_for(ROLE_ADMIN).isdisjoint(SUPER_ONLY)


@pytest.mark.parametrize("permission", ["admin.settings.read", "admin.settings.write", "admin.audit.read"])
def test_settings_and_audit_log_stay_super_admin_only(permission):
    assert permission not in permissions_for(ROLE_ADMIN)
    assert permission in permissions_for(ROLE_SUPER_ADMIN)


@pytest.mark.parametrize("permission", ["admin.users.read", "admin.promotions.write", "admin.vouchers.write"])
def test_admin_reads_pages_and_writes_catalog(permission):
    assert permission in permissions_for(ROLE_ADMIN)


@pytest.mark.parametrize("permission", ["admin.banners.read", "admin.banners.write"])
def test_banners_are_super_admin_only(permission):
    assert permission not in permissions_for(ROLE_ADMIN)
    assert permission in permissions_for(ROLE_SUPER_ADMIN)


@pytest.mark.parametrize("role", [None, "", "user", "authenticated", "SUPER_ADMIN", 1, {"role": "admin"}])
def test_missing_or_unknown_role_has_no_access(role):
    assert permissions_for(role) == frozenset()
