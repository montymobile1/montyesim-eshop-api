"""Admin portal roles and the permissions each role grants.

The role is stored on the Supabase auth user in ``app_metadata.role`` (writable with the
service-role key only). Every admin route declares the one permission it needs with
``require_permission("admin.<area>.<action>")``; this file is the single place that decides
which role holds which permission. Moving a permission between roles is a one-line change here.
"""
from typing import FrozenSet, Optional

ROLE_ADMIN = "admin"
ROLE_SUPER_ADMIN = "super_admin"

# Areas every admin can view.
_READ_AREAS = (
    "users", "orders", "bundles", "groups", "tags", "rules", "promotions",
    "vouchers", "banners", "devices", "contact_us", "referrals",
)
# Areas every admin can create, edit and delete in.
_WRITE_AREAS = ("bundles", "groups", "tags", "rules", "promotions", "vouchers", "banners")

READ: FrozenSet[str] = frozenset(f"admin.{area}.read" for area in _READ_AREAS)
WRITE: FrozenSet[str] = frozenset(f"admin.{area}.write" for area in _WRITE_AREAS)

# Super admin only. Settings and the audit log stay super-admin only, as in the portal today.
SUPER_ONLY: FrozenSet[str] = frozenset({
    "admin.settings.read",
    "admin.settings.write",
    "admin.audit.read",
    "admin.users.ban",
    "admin.bundles.bulk",
    "admin.referrals.export",
})

ROLE_PERMISSIONS: dict[str, FrozenSet[str]] = {
    ROLE_ADMIN: READ | WRITE,
    ROLE_SUPER_ADMIN: READ | WRITE | SUPER_ONLY,
}

ALL_PERMISSIONS: FrozenSet[str] = READ | WRITE | SUPER_ONLY


def permissions_for(role: Optional[str]) -> FrozenSet[str]:
    """Permissions granted to ``role``; empty for a missing or unknown role (no portal access)."""
    if not isinstance(role, str):
        return frozenset()
    return ROLE_PERMISSIONS.get(role, frozenset())
