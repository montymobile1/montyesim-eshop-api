from fastapi import APIRouter, Request
from fastapi.params import Query

from app.schemas.response import ResponseHelper
from app.services.callback_service import CallbackService

router = APIRouter()

service = CallbackService()


@router.post("/payment-webhook")
async def payment_webhook(request: Request):
    await service.handle_payment_webhook(request)


@router.post("/payment-webhook-fake")
async def payment_webhook(request: Request):
    await service.handle_payment_webhook_fake(request)


@router.post("/plan_status_callback")
async def consumption_limit(request: Request):
    await service.handle_plan_event_callback(callback_request=request)
    return ResponseHelper.success_response()


@router.post("/bundle/sync-all")
async def bundle_sync_all(request: Request, page_index=Query(default=1, description="Page Index")):
    return await service.handle_sync_all_bundles(page_index=page_index)


@router.post("/bundle/sync-one/{id}")
async def bundle_sync_all(request: Request, id: str):
    return await service.handle_sync_one_bundle(id)


@router.post("/bundle-webhook/sync")
async def bundle_sync_all(request: Request):
    return await service.handle_sync_bundle(request)

# @router.post("/notify")
# async def consumption_limit():
#     # notification_data = send_buy_bundle_notification(amount="10", iccid= "892200660703814891")
#     # notification_data=send_buy_topup_notification(amount= "10", iccid= "892200660703814891")
#     notification_data = send_consumption_bundle_notification(iccid="892200660703814891",consumption=80)
#     fcm_service.send_notification_to_user_from_template(content_template=notification_data, user_id="48f46de0-d318-47ea-9370-626bb81bd968")
#     return ResponseHelper.success_response()
