from fastapi import APIRouter, Request
from fastapi.params import Query

from app.schemas.response import ResponseHelper
from app.services.callback_service import CallbackService
from app.services.link_aja_callback_service import LinkAjaCallbackService

router = APIRouter()

service = CallbackService()

# Constructed lazily rather than at import: on the overwhelming majority of
# deployments LinkAja is disabled, and building the provisioning collaborators
# for a route that will only ever reject requests is pure waste.
__link_aja_service: LinkAjaCallbackService | None = None


def get_link_aja_callback_service() -> LinkAjaCallbackService:
    global __link_aja_service
    if __link_aja_service is None:
        __link_aja_service = LinkAjaCallbackService()
    return __link_aja_service


@router.post("/payment-webhook")
async def payment_webhook(request: Request):
    await service.handle_payment_webhook(request)


@router.post("/linkaja")
async def link_aja_callback(request: Request):
    """LinkAja Inform Payment (notify) callback.

    Deliberately separate from `/payment-webhook`, which belongs to Stripe.

    Answers in LinkAja's own SNAP format (2005600 + approvalCode on success),
    not the API envelope. Fails closed: until LinkAja's notify public key is
    configured every request is rejected and no provisioning occurs.
    """
    return await get_link_aja_callback_service().handle_callback(request)


# LinkAja's spec names the notify URI "…/bi/applink/v1.0/debit/notify" (v1.1
# §4.3). This router is mounted at the application root, with no /api/v1
# prefix, so that exact path is served by the same handler as /callback/linkaja.
link_aja_notify_router = APIRouter()


@link_aja_notify_router.post("/bi/applink/v1.0/debit/notify")
async def link_aja_notify(request: Request):
    """Alias of `POST /api/v1/callback/linkaja` at the spec's own path."""
    return await get_link_aja_callback_service().handle_callback(request)


@router.post("/payment-webhook-fake")
async def payment_webhook(request: Request):
    await service.handle_payment_webhook_fake(request)


@router.post("/plan_status_callback")
async def consumption_limit(request: Request):
    await service.handle_plan_event_callback(callback_request=request)
    return ResponseHelper.success_response()


@router.post("/bundle/sync-all")
def bundle_sync_all(request: Request, page_index=Query(default=1, description="Page Index")):
    return service.handle_sync_all_bundles(page_index=page_index)


@router.post("/bundle/sync-one")
async def bundle_sync_all(request: Request):
    return await service.handle_sync_one_bundle(request=request)


@router.post("/bundle/sync-one-by-id/{id}")
def bundle_sync_all(request: Request, id: str):
    return service.handle_sync_one_bundle_by_id(request=request, id=id)


@router.post("/currency/exchange_rate")
async def currency_exchange_rate(request: Request):
    return await service.handle_exchange_rate_update(request=request)
