import os
from typing import Annotated, List

from fastapi import APIRouter, Depends, Header
from fastapi.params import Query

from app.dependencies.security import bearer_token, device_token, bearer_token_anonymous
from app.models.user import UserModel
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
            dependencies=[Depends(bearer_token), Depends(device_token)])
async def consumption(iccid: str, user: Annotated[UserModel, Depends(bearer_token)]):
    return await service.consumption(user, iccid)


@router.post("/bundle/assign", response_model=Response[PaymentIntentResponse] | Response[bool],
             dependencies=[Depends(bearer_token_anonymous), Depends(device_token)])
async def assign(assign_request: AssignRequest, user: Annotated[UserModel, Depends(bearer_token_anonymous)],
                 x_device_id: str = Header(None), x_currency: str = Header(os.getenv("DEFAULT_CURRENCY")),
                 accept_language: str = Header("en")):
    return await service.assign(user, x_device_id, assign_request, x_currency, accept_language)


@router.post("/bundle/verify_order_otp", response_model=Response[bool],
             dependencies=[Depends(bearer_token), Depends(device_token)])
async def verify_order_otp(request: VerifyOtpRequestDto, user: Annotated[UserModel, Depends(bearer_token)]):
    return await service.verify_order_otp(user, request)


@router.post("/bundle/resend_order_otp/{order_id}", response_model=Response,
             dependencies=[Depends(bearer_token), Depends(device_token)])
async def resend_order_otp(order_id: str, user: Annotated[UserModel, Depends(bearer_token)]):
    return await service.resend_order_otp(user=user, order_id=order_id)


@router.post("/bundle/assign-top-up", response_model=Response[PaymentIntentResponse],
             dependencies=[Depends(bearer_token), Depends(device_token)])
async def assign_top_up(assign_top_up_request: AssignTopUpRequest, user: Annotated[UserModel, Depends(bearer_token)],
                        x_device_id: str = Header(None)):
    return await service.assign_top_up(user, assign_top_up_request, x_device_id)


@router.delete("/order/cancel/{id}", response_model=Response,
               dependencies=[Depends(bearer_token), Depends(device_token)])
async def cancel_order(id: str, user: Annotated[UserModel, Depends(bearer_token)]):
    return await service.cancel_order(order_id=id, user=user)


@router.get("/my-esim", response_model=Response[List[EsimBundleResponse]],
            dependencies=[Depends(bearer_token), Depends(device_token)])
async def get_order_details(user: Annotated[UserModel, Depends(bearer_token)],
                            x_device_id: str = Header(None)):
    return await service.get_user_esims(user)


@router.get("/my-esim/{iccid}", response_model=Response[EsimBundleResponse],
            dependencies=[Depends(bearer_token), Depends(device_token)])
async def get_order_details(iccid: str, user: Annotated[UserModel, Depends(bearer_token)],
                            x_device_id: str = Header(None)):
    return await service.get_user_esim(iccid, user)


@router.get("/my-esim-by-order/{order_id}", response_model=Response[EsimBundleResponse],
            dependencies=[Depends(bearer_token_anonymous), Depends(device_token)])
async def get_order_details(order_id: str, user: Annotated[UserModel, Depends(bearer_token_anonymous)],
                            x_device_id: str = Header(None)):
    return await service.get_user_esim_by_order_id(order_id, user)


@router.get("/user-notification", response_model=Response[List[UserNotificationResponse]],
            dependencies=[Depends(bearer_token), Depends(device_token)])
async def user_notification(user: Annotated[UserModel, Depends(bearer_token)],
                            page_index: int = Query(1, description="Page Index"),
                            page_size: int = Query(10, description="Page Size")):
    return await service.user_notifications(user=user, page_index=page_index, page_size=page_size)


@router.post("/read-user-notification/", response_model=Response[dict],
             dependencies=[Depends(bearer_token), Depends(device_token)])
async def read_user_notification(user: Annotated[UserModel, Depends(bearer_token)], x_device_id: str = Header(None)):
    return await service.read_user_notification(user, x_device_id)


@router.get("/bundle-exists/{code}", response_model=Response[bool],
            dependencies=[Depends(bearer_token), Depends(device_token)])
async def bundle_exists(code: str, user: Annotated[UserModel, Depends(bearer_token)]):
    # return service.bundle_exists(user_id=user.id, bundle_id=code)
    return await service.bundle_exists(user_id=user.id, bundle_id=code)


@router.post("/bundle-label/{code}", response_model=Response[dict],
             dependencies=[Depends(bearer_token), Depends(device_token)])
async def update_bundle_label(code: str, bundle_label_request: UpdateBundleLabelRequest,
                              user: Annotated[UserModel, Depends(bearer_token)]):
    return await service.update_bundle_name(code, bundle_label_request, user)


@router.post("/bundle-label-by-iccid/{iccid}", response_model=Response[dict],
             dependencies=[Depends(bearer_token), Depends(device_token)])
async def update_bundle_label(iccid: str, bundle_label_request: UpdateBundleLabelRequest,
                              user: Annotated[UserModel, Depends(bearer_token)]):
    return await service.update_bundle_name_by_iccid(iccid=iccid, bundle_label_request=bundle_label_request, user=user)


@router.get("/related-topup/{bundle_code}/{iccid}", response_model=Response[List[BundleDTO]],
            dependencies=[Depends(bearer_token), Depends(device_token)])
async def get_related_topup(bundle_code: str, iccid: str, user: Annotated[UserModel, Depends(bearer_token)]
                            , x_device_id: str = Header(None),
                            x_currency: str = Header(os.getenv("DEFAULT_CURRENCY")),
                            accept_language: str = Header("en")) -> Response[List[BundleDTO]]:
    return await service.get_topup_related_bundle(bundle_code=bundle_code, iccid=iccid, user=user,
                                                  accept_language=accept_language, currency_code=x_currency)


@router.get("/order-history", response_model=Response[List[UserOrderHistoryResponse]],
            dependencies=[Depends(bearer_token), Depends(device_token)])
async def get_order_history(user: Annotated[UserModel, Depends(bearer_token)],
                            page_index: int = Query(1, description="Page Index"),
                            page_size: int = Query(10, description="Page Size"), x_device_id: str = Header(None),
                            accept_language: str = Header("en"), ) -> Response[
    List[UserOrderHistoryResponse]]:
    return await service.get_order_history(user_id=user.id, page_index=page_index, page_size=page_size)


@router.get("/order-history/{order_id}", response_model=Response[UserOrderHistoryResponse],
            dependencies=[Depends(bearer_token), Depends(device_token)])
async def get_order_history_by_id(user: Annotated[UserModel, Depends(bearer_token)], order_id: str,
                                  x_device_id: str = Header(None)) -> Response[
    UserOrderHistoryResponse]:
    return await service.get_order_history_by_id(order_id=order_id, user_id=user.id)
