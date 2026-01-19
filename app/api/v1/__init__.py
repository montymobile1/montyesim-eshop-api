from fastapi import APIRouter

from app.api.v1.application import router as app_routes
from app.api.v1.authentication import router as auth_routes
from app.api.v1.bundles import router as bundle_routes
from app.api.v1.callback import router as notification_routes
from app.api.v1.health_check import router as health_check_router
from app.api.v1.home import router as home_routes
from app.api.v1.payment import router as payment_router
from app.api.v1.promotion import router as promotion_router
from app.api.v1.user_bundle import router as user_bundle_routes
from app.api.v1.user_wallet import router as user_wallet_router
from app.api.v1.voucher import router as voucher_router
from app.api.v1.popup_easypaisa import router as popup_easypaisa_router

router = APIRouter()

router.include_router(health_check_router, tags=["healthcheck"])
router.include_router(home_routes, prefix="/home", tags=["Home"])
router.include_router(app_routes, prefix="/app", tags=["App"])
router.include_router(auth_routes, prefix="/auth", tags=["Auth"])
router.include_router(bundle_routes, prefix="/bundles", tags=["Bundles"])
router.include_router(notification_routes, prefix="/callback", tags=["Callback"])
router.include_router(user_bundle_routes, prefix="/user", tags=["User"])
router.include_router(user_wallet_router, prefix="/wallet", tags=["Wallet"])
router.include_router(voucher_router, prefix="/voucher", tags=["Voucher"])
router.include_router(promotion_router, prefix="/promotion", tags=["Promotion"])
router.include_router(payment_router, prefix="/payment", tags=["Payment"])
router.include_router(popup_easypaisa_router, prefix="/popup", tags=["Popup"])
