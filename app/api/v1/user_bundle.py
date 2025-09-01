from typing import List

from fastapi import APIRouter, Depends, Request
from fastapi.params import Query

from app.dependencies.currency import x_currency_header
from app.dependencies.language import accept_language_header
from app.dependencies.security import bearer_token, device_token, bearer_token_anonymous
from app.schemas.app import UserNotificationResponse
from app.schemas.bundle import AssignRequest, AssignTopUpRequest, PaymentIntentResponse, EsimBundleResponse, \
    ConsumptionResponse, UserOrderHistoryResponse, VerifyOtpRequestDto
from app.schemas.bundle import UpdateBundleLabelRequest
from app.schemas.home import BundleDTO
from app.schemas.response import Response
from app.services.user_service import UserBundleService

router = APIRouter()

service = UserBundleService()


@router.get("/consumption/{iccid}", response_model=Response[ConsumptionResponse],
            dependencies=[Depends(bearer_token), Depends(device_token), Depends(accept_language_header),
                          Depends(x_currency_header)])
async def consumption(iccid: str):
    return await service.consumption(iccid)


@router.post("/bundle/assign", response_model=Response[PaymentIntentResponse] | Response[bool],
             dependencies=[Depends(bearer_token_anonymous), Depends(device_token), Depends(accept_language_header),
                           Depends(x_currency_header)])
async def assign(assign_request: AssignRequest, request: Request):
    return await service.assign(assign_request=assign_request, request=request)


@router.post("/bundle/verify_order_otp", response_model=Response[bool],
             dependencies=[Depends(bearer_token), Depends(device_token), Depends(accept_language_header),
                           Depends(x_currency_header)])
async def verify_order_otp(request: VerifyOtpRequestDto):
    return await service.verify_order_otp(request)


@router.post("/bundle/assign-top-up", response_model=Response[PaymentIntentResponse],
             dependencies=[Depends(bearer_token), Depends(device_token), Depends(accept_language_header),
                           Depends(x_currency_header)])
async def assign_top_up(assign_top_up_request: AssignTopUpRequest, request: Request):
    return await service.assign_top_up(assign_top_up_request=assign_top_up_request, request=request)


@router.delete("/order/cancel/{id}", response_model=Response,
               dependencies=[Depends(bearer_token_anonymous), Depends(device_token), Depends(accept_language_header),
                             Depends(x_currency_header)])
async def cancel_order(id: str):
    return await service.cancel_order(order_id=id)


@router.get("/my-esim", response_model=Response[List[EsimBundleResponse]],
            dependencies=[Depends(bearer_token), Depends(device_token), Depends(accept_language_header),
                          Depends(x_currency_header)])
async def get_order_details():
    return await service.get_user_esims()


@router.get("/my-esim/{iccid}", response_model=Response[EsimBundleResponse],
            dependencies=[Depends(bearer_token), Depends(device_token), Depends(accept_language_header),
                          Depends(x_currency_header)])
async def get_order_details(iccid: str):
    return await service.get_user_esim(iccid=iccid)


@router.get("/my-esim-by-order/{order_id}", response_model=Response[EsimBundleResponse],
            dependencies=[Depends(bearer_token_anonymous), Depends(device_token), Depends(accept_language_header),
                          Depends(x_currency_header)])
async def get_order_details(order_id: str):
    return await service.get_user_esim_by_order_id(order_id=order_id)


@router.get("/user-notification", response_model=Response[List[UserNotificationResponse]],
            dependencies=[Depends(bearer_token), Depends(device_token), Depends(accept_language_header),
                          Depends(x_currency_header)])
async def user_notification(page_index: int = Query(1, description="Page Index"),
                            page_size: int = Query(10, description="Page Size")):
    return await service.user_notifications(page_index=page_index, page_size=page_size)


@router.post("/read-user-notification/", response_model=Response[dict],
             dependencies=[Depends(bearer_token), Depends(device_token)])
async def read_user_notification():
    return await service.read_user_notification()


@router.get("/bundle-exists/{code}", response_model=Response[bool],
            dependencies=[Depends(bearer_token), Depends(device_token)])
async def bundle_exists(code: str):
    return await service.bundle_exists(bundle_id=code)


@router.post("/bundle-label/{code}", response_model=Response[dict],
             dependencies=[Depends(bearer_token), Depends(device_token)])
async def update_bundle_label(code: str, bundle_label_request: UpdateBundleLabelRequest):
    return await service.update_bundle_name(code, bundle_label_request)


@router.post("/bundle-label-by-iccid/{iccid}", response_model=Response[dict],
             dependencies=[Depends(bearer_token), Depends(device_token)])
async def update_bundle_label(iccid: str, bundle_label_request: UpdateBundleLabelRequest):
    return await service.update_bundle_name_by_iccid(iccid=iccid, bundle_label_request=bundle_label_request)


@router.get("/related-topup/{bundle_code}/{iccid}", response_model=Response[List[BundleDTO]],
            dependencies=[Depends(bearer_token), Depends(device_token), Depends(accept_language_header),
                          Depends(x_currency_header)])
async def get_related_topup(bundle_code: str, iccid: str) -> Response[List[BundleDTO]]:
    return await service.get_topup_related_bundle(bundle_code=bundle_code, iccid=iccid)


@router.get("/order-history", response_model=Response[List[UserOrderHistoryResponse]],
            dependencies=[Depends(bearer_token), Depends(device_token)])
async def get_order_history(page_index: int = Query(1, description="Page Index"),
                            page_size: int = Query(10, description="Page Size")) -> Response[
    List[UserOrderHistoryResponse]]:
    return await service.get_order_history(page_index=page_index, page_size=page_size)


@router.get("/order-history/{order_id}", response_model=Response[UserOrderHistoryResponse],
            dependencies=[Depends(bearer_token), Depends(device_token)])
async def get_order_history_by_id(order_id: str) -> Response[
    UserOrderHistoryResponse]:
    return await service.get_order_history_by_id(order_id=order_id)
