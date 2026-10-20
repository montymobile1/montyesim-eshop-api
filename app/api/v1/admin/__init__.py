"""Admin portal API, mounted at /api/v1/admin. Every route uses AdminRoute (see _route.py)."""
from app.api.v1.admin._route import admin_router
from app.api.v1.admin.auth import router as auth_router

router = admin_router()

router.include_router(auth_router, prefix="/auth")
