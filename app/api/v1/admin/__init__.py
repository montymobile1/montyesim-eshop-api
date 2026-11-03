"""Admin portal API, mounted at /api/v1/admin. Every route uses AdminRoute (see _route.py).

Each area has its own router file, owned by the story named in its docstring. Story work only
fills its own router file; this file only gains one include line per new area.

``ADMIN_ERROR_RESPONSES`` documents in the OpenAPI spec the errors every admin route can answer
(TEOS-163): they come from shared dependencies and ``AdminRoute``, so no route declares them itself.
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
from app.api.v1.admin.promotions import router as promotions_router
from app.api.v1.admin.referrals import router as referrals_router
from app.api.v1.admin.settings import router as settings_router
from app.api.v1.admin.tags import router as tags_router
from app.api.v1.admin.users import router as users_router
from app.api.v1.admin.vouchers import router as vouchers_router
from app.schemas.admin.common import ERROR_ENVELOPE as _ENVELOPE

#: Errors shared by every admin route. Route-specific 404/409 stay in the route docs.
ADMIN_ERROR_RESPONSES = {
    401: {"content": _ENVELOPE, "description": "No session, or the access token is invalid or expired"},
    403: {"content": _ENVELOPE, "description": "The signed-in admin lacks the permission of this route"},
    422: {"content": _ENVELOPE, "description": "Invalid request: body, path or query parameter (an unknown "
                                               "query parameter as well); the cause in `developerMessage`"},
    502: {"content": _ENVELOPE, "description": "The database, storage or Supabase Auth failed"},
}
#: The auth routes have no permission check: login, refresh and me answer 401, never 403.
AUTH_ERROR_RESPONSES = {code: ADMIN_ERROR_RESPONSES[code] for code in (401, 422, 502)}

router = admin_router()

router.include_router(auth_router, prefix="/auth", responses=AUTH_ERROR_RESPONSES)
router.include_router(users_router, prefix="/users", responses=ADMIN_ERROR_RESPONSES)
router.include_router(bundles_router, prefix="/bundles", responses=ADMIN_ERROR_RESPONSES)
router.include_router(groups_router, prefix="/groups", responses=ADMIN_ERROR_RESPONSES)
router.include_router(tags_router, prefix="/tags", responses=ADMIN_ERROR_RESPONSES)
router.include_router(promotion_rules_router, prefix="/promotion-rules", responses=ADMIN_ERROR_RESPONSES)
router.include_router(promotions_router, prefix="/promotions", responses=ADMIN_ERROR_RESPONSES)
router.include_router(orders_router, prefix="/orders", responses=ADMIN_ERROR_RESPONSES)
router.include_router(devices_router, prefix="/devices", responses=ADMIN_ERROR_RESPONSES)
router.include_router(contact_us_router, prefix="/contact-us", responses=ADMIN_ERROR_RESPONSES)
router.include_router(vouchers_router, prefix="/vouchers", responses=ADMIN_ERROR_RESPONSES)
router.include_router(banners_router, prefix="/banners", responses=ADMIN_ERROR_RESPONSES)
router.include_router(settings_router, prefix="/settings", responses=ADMIN_ERROR_RESPONSES)
router.include_router(audit_log_router, prefix="/audit-log", responses=ADMIN_ERROR_RESPONSES)
router.include_router(referrals_router, prefix="/referrals", responses=ADMIN_ERROR_RESPONSES)
