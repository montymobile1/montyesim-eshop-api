"""Admin portal API, mounted at /api/v1/admin. Every route uses AdminRoute (see _route.py).

Each area has its own router file, owned by the story named in its docstring. Story work only
fills its own router file; this file only gains one include line per new area.
"""
from app.api.v1.admin._route import admin_router
from app.api.v1.admin.audit_log import router as audit_log_router
from app.api.v1.admin.auth import router as auth_router
from app.api.v1.admin.banners import router as banners_router
from app.api.v1.admin.bundles import router as bundles_router
from app.api.v1.admin.contact_us import router as contact_us_router
from app.api.v1.admin.devices import router as devices_router
from app.api.v1.admin.groups import router as groups_router
from app.api.v1.admin.orders import router as orders_router
from app.api.v1.admin.promotion_rules import router as promotion_rules_router
from app.api.v1.admin.settings import router as settings_router
from app.api.v1.admin.tags import router as tags_router
from app.api.v1.admin.users import router as users_router
from app.api.v1.admin.vouchers import router as vouchers_router

router = admin_router()

router.include_router(auth_router, prefix="/auth")
router.include_router(users_router, prefix="/users")
router.include_router(bundles_router, prefix="/bundles")
router.include_router(groups_router, prefix="/groups")
router.include_router(tags_router, prefix="/tags")
router.include_router(promotion_rules_router, prefix="/promotion-rules")
router.include_router(orders_router, prefix="/orders")
router.include_router(devices_router, prefix="/devices")
router.include_router(contact_us_router, prefix="/contact-us")
router.include_router(vouchers_router, prefix="/vouchers")
router.include_router(banners_router, prefix="/banners")
router.include_router(settings_router, prefix="/settings")
router.include_router(audit_log_router, prefix="/audit-log")
