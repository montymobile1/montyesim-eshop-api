import os
from typing import List

import bleach
import stripe
from loguru import logger

from app.config.config import create_payment_intent, create_payment_ephemeral, stripe_get_payment_details, \
    esim_hub_service_instance, generate_otp, dcb_service_instance
from app.config.db import DatabaseTables, PaymentTypeEnum
from app.exceptions import BadRequestException, CustomException, DCBException
from app.models.user import UserModel, UserOrderType, OrderStatusEnum, UserOrderModel
from app.repo import NotificationRepo, UserOrderRepo, UserProfileRepo, UserProfileBundleRepo
from app.repo.bundle_repo import BundleRepo
from app.schemas.app import UserNotificationResponse
from app.schemas.bundle import AssignRequest, AssignTopUpRequest, PaymentIntentResponse, EsimBundleResponse, \
    ConsumptionResponse, UserOrderHistoryResponse, UpdateBundleLabelRequest, VerifyOtpRequestDto
from app.schemas.dto_mapper import DtoMapper
from app.schemas.home import BundleDTO
from app.schemas.promotion import PromotionValidationRequest
from app.schemas.response import Response, ResponseHelper
from app.services.bundle_service import BundleService
from app.services.promotion_service import PromotionService
from app.services.user_wallet_service import UserWalletService


class UserBundleService:

    def __init__(self):
        self.__esim_hub_service = esim_hub_service_instance()
        self.__notification_repo = NotificationRepo()
        self.__user_order_repo = UserOrderRepo()
        self.__user_profile_repo = UserProfileRepo()
        self.__user_profile_bundle_repo = UserProfileBundleRepo()
        self.__bundle_repo = BundleRepo()
        self.__user_wallet_service = UserWalletService()
        self.__promotion_service = PromotionService()
        self.__bundle_service = BundleService()
        self.__dcb_service = dcb_service_instance()

    async def assign(self, user: UserModel, device_id: str, assign_request: AssignRequest, x_currency: str,
                     locale: str) -> Response[
                                         PaymentIntentResponse] | \
                                     Response[
                                         bool]:
        # bundle = await self.__esim_hub_service.get_bundle_by_id(assign_request.bundle_code)
        bundle_response = await self.__bundle_service.get_bundle(bundle_id=assign_request.bundle_code,
                                                                 currency_name=x_currency, locale=locale)
        bundle = bundle_response.data

        if not bundle.is_stockable:
            check_bundle_available = await self.__esim_hub_service.check_bundle_applicable(bundle.bundle_info_code)
            if not check_bundle_available:
                raise CustomException(code=400, name="Buy Bundle", details="Bundle Not Available Now Try Again Later")

        rule_id = "0"
        amount = bundle.price
        modified_amount = bundle.price
        if assign_request.promo_code:
            promo_code_request = PromotionValidationRequest(promo_code=assign_request.promo_code,
                                                            bundle_code=assign_request.bundle_code)
            bundle = await self.__promotion_service.validate_promotion_code(promo_code_request, x_currency, user.id)
            bundle = bundle.data
            promo_code_details = self.__promotion_service.code_type_and_get_rule(assign_request.promo_code,
                                                                                 user.id).data
            rule_id = promo_code_details.rule_id
            modified_amount = await self.__promotion_service.add_reward(promo_code_details.rule_id, user.id,
                                                                        bundle.bundle_code,
                                                                        assign_request.promo_code, False)

        order = self.__user_order_repo.create(data={
            "user_id": user.id,
            "bundle_id": assign_request.bundle_code,
            "order_type": UserOrderType.ASSIGN,
            "amount": round(amount * 100),
            "modified_amount": round(modified_amount * 100),
            "currency": os.getenv("DEFAULT_CURRENCY"),
            "bundle_data": bundle.model_dump_json(),
            "searched_countries": assign_request.related_search.model_dump_json(),
            "anonymous_user_id": user.anonymous_user_id
        })
        payment_type = assign_request.payment_type
        if payment_type == PaymentTypeEnum.WALLET:
            return await self.__handle_wallet_payment(user=user, bundle=bundle, user_order=order)
        elif payment_type == PaymentTypeEnum.DCB:
            return await self.__handle_dcb_payment(user=user, bundle=bundle, user_order=order)

        payment_intent = create_payment_intent(user_bundle_order=order, user_email=user.email,
                                               metadata={
                                                   "order_id": order.id,
                                                   "user_id": order.user_id,
                                                   "device_id": device_id,
                                                   "bundle_code": order.bundle_id,
                                                   "order_type": order.order_type,
                                                   "env": os.environ.get("ENVIRONMENT", "DEV"),
                                                   "promo_code": assign_request.promo_code,
                                                   "rule_id": rule_id,
                                                   "amount": round(modified_amount * 100)
                                               })
        order.payment_intent_code = payment_intent.id
        logger.debug(payment_intent)
        self.__user_order_repo.update_by({"id": order.id}, data=order.model_dump(exclude={"id"}))
        ephemeral = create_payment_ephemeral(payment_intent.customer)
        response = PaymentIntentResponse(publishable_key=os.getenv("STRIPE_PUBLIC_KEY"),
                                         merchant_identifier=os.getenv("MERCHANT_ID"),
                                         payment_intent_client_secret=payment_intent.client_secret,
                                         customer_id=payment_intent.customer,
                                         customer_ephemeral_key_secret=ephemeral.secret,
                                         test_env=not payment_intent.livemode,
                                         merchant_display_name=os.getenv("MERCHANT_DISPLAY_NAME"),
                                         billing_country_code="GB",
                                         order_id=order.id)
        return ResponseHelper.success_data_response(response, 0)

    async def assign_top_up(self, user: UserModel, assign_top_up_request: AssignTopUpRequest, device_id) -> Response:
        # bundle = await self.__esim_hub_service.get_bundle_by_id(assign_top_up_request.bundle_code)
        bundle = self.__bundle_repo.get_bundle_by_id(bundle_id=assign_top_up_request.bundle_code)

        order = self.__user_order_repo.create({
            "user_id": user.id,
            "bundle_id": assign_top_up_request.bundle_code,
            "order_type": UserOrderType.BUNDLE_TOP_UP,
            "amount": round(bundle.price * 100),
            "currency": os.getenv("DEFAULT_CURRENCY"),
            "bundle_data": bundle.model_dump_json(),
            "searched_countries": None,
        })
        payment_type = assign_top_up_request.payment_type
        if payment_type == PaymentTypeEnum.WALLET:
            return await self.__handle_wallet_payment(user=user, bundle=bundle, user_order=order)
        elif payment_type == PaymentTypeEnum.DCB:
            return await self.__handle_dcb_payment(user=user, bundle=bundle, user_order=order)

        payment_intent = create_payment_intent(user_bundle_order=order, user_email=user.email, metadata={
            "order_id": order.id,
            "user_id": order.user_id,
            "device_id": device_id,
            "bundle_code": order.bundle_id,
            "order_type": order.order_type,
            "iccid": assign_top_up_request.iccid,
            "env": os.getenv("ENVIRONMENT", "DEV")
        })
        order.payment_intent_code = payment_intent.id
        order.modified_amount = order.amount
        self.__user_order_repo.update_by({"id": order.id}, data=order.model_dump(exclude={"id"}))

        ephemeral = create_payment_ephemeral(payment_intent.customer)
        response = PaymentIntentResponse(publishable_key=os.getenv("STRIPE_PUBLIC_KEY"),
                                         merchant_identifier=os.getenv("MERCHANT_ID"),
                                         payment_intent_client_secret=payment_intent.client_secret,
                                         customer_id=payment_intent.customer,
                                         customer_ephemeral_key_secret=ephemeral.secret,
                                         test_env=not payment_intent.livemode,
                                         merchant_display_name=os.getenv("MERCHANT_DISPLAY_NAME"),
                                         billing_country_code="GB", order_id=order.id)
        return ResponseHelper.success_data_response(response, 0)

    async def get_user_esims(self, user: UserModel) -> Response[List[EsimBundleResponse]]:
        user_profiles = self.__user_profile_repo.select(tables={DatabaseTables.TABLE_USER_PROFILE_BUNDLE: "*"},
                                                        where={"user_id": user.id})
        esim_bundle_response = []
        for profile in user_profiles:
            try:
                bundle = DtoMapper.to_esim_bundle_response(profile)
                if bundle is not None:
                    esim_bundle_response.append(bundle)
            except Exception as e:
                print(e)
                logger.error(e)
                logger.error(f"Failed to map profile {profile.id if hasattr(profile, 'id') else 'unknown'}: {e}")
        return ResponseHelper.success_data_response(esim_bundle_response, len(esim_bundle_response))

    async def get_user_esim(self, iccid: str, user: UserModel) -> Response[EsimBundleResponse | None]:
        user_profiles = self.__user_profile_repo.select(tables={DatabaseTables.TABLE_USER_PROFILE_BUNDLE: "*"},
                                                        where={"user_id": user.id, "iccid": iccid})
        if len(user_profiles) == 0:
            raise CustomException(code=404, name="Not Found", details="user profile not found")
        return ResponseHelper.success_data_response(DtoMapper.to_esim_bundle_response(user_profiles[0]), 0)

    async def consumption(self, user: UserModel, iccid: str) -> Response[ConsumptionResponse]:
        profile = self.__user_profile_repo.get_first_by({"user_id": user.id, "iccid": iccid})
        consumption = await self.__esim_hub_service.get_bundle_consumption(profile.esim_hub_order_id)
        return ResponseHelper.success_data_response(consumption, 0)

    async def user_notifications(self, user: UserModel, page_index: int, page_size: int) -> Response[
        List[UserNotificationResponse]]:
        notifications = self.__notification_repo.list(where={"user_id": user.id}, limit=page_size,
                                                      offset=((page_index - 1) * page_size), order_by="created_at",
                                                      desc=True)
        return ResponseHelper.success_data_response(
            [DtoMapper.to_user_notification_response(data) for data in notifications], 1)

    async def read_user_notification(self, user: UserModel, device_id) -> Response:
        logger.info("read user notification for user {}".format(user.email))
        self.__notification_repo.update_by(where={"user_id": user.id}, data={"status": True})
        return ResponseHelper.success_response()

    async def bundle_exists(self, user_id: str, bundle_id: str) -> Response[bool]:
        orders = self.__user_order_repo.select(tables={DatabaseTables.TABLE_USER_PROFILE: "*"}, where={
            "user_id": user_id,
            "bundle_id": bundle_id,
            "payment_status": "success",
            "order_status": "success",
            f"{DatabaseTables.TABLE_USER_PROFILE}.allow_topup": True
        }, as_model=False)
        if len(orders) == 0:
            return ResponseHelper.success_data_response(False, 0)
        if any(len(item.get("user_profile", [])) > 0 for item in orders):
            logger.info("At least one profile list is non-empty")
            return ResponseHelper.success_data_response(True, 0)
        return ResponseHelper.success_data_response(False, 0)

    async def update_bundle_name(self, code: str, bundle_label_request: UpdateBundleLabelRequest, user: UserModel):
        user_profile_bundle = self.__user_profile_bundle_repo.get_first_by(where={"user_id": user.id},
                                                                           filters={
                                                                               "bundle_data ->> bundle_code": code})
        if user_profile_bundle is None:
            raise CustomException(code=400, name="DB Exception", details="Bundle Not Found")
        bundle = BundleDTO.model_validate(user_profile_bundle.bundle_data)
        bundle.label = bleach.clean(bundle_label_request.label)
        self.__user_profile_bundle_repo.update_by(
            where={"user_id": user.id}, filters={"bundle_data ->> bundle_code ": code},
            data={"bundle_data": bundle.model_dump()})
        return ResponseHelper.success_response()

    async def update_bundle_name_by_iccid(self, iccid: str, bundle_label_request: UpdateBundleLabelRequest,
                                          user: UserModel):
        user_profile_bundle = self.__user_profile_bundle_repo.get_first_by(where={"user_id": user.id, "iccid": iccid})
        if user_profile_bundle is None:
            raise CustomException(code=400, name="DB Exception", details="Bundle Not Found")
        bundle = BundleDTO.model_validate(user_profile_bundle.bundle_data)
        bundle.label = bleach.clean(bundle_label_request.label)
        self.__user_profile_bundle_repo.update_by(
            where={"user_id": user.id, "iccid": iccid},
            data={"bundle_data": bundle.model_dump()})
        return ResponseHelper.success_response()

    async def get_topup_related_bundle(self, bundle_code: str, iccid: str, user: UserModel, accept_language: str = "en",
                                       currency_code: str = os.getenv("DEFAULT_CURRENCY")) -> Response[
        List[BundleDTO]]:
        profile = self.__user_profile_repo.get_first_by({"user_id": user.id, "iccid": iccid})
        if not profile:
            raise BadRequestException(details="This ICCID is not linked to this user")
        bundles = await self.__esim_hub_service.get_topup_related_bundles(bundle_code=bundle_code,
                                                                          order_id=profile.esim_hub_order_id)
        all_bundles = []
        for bundle in bundles:
            local_bundle = await self.__bundle_service.get_bundle(bundle_id=bundle.bundle_code,
                                                                  currency_name=currency_code,
                                                                  locale=accept_language)
            logger.debug(f"local bundle {local_bundle.data}")
            all_bundles.append(local_bundle.data)

        return ResponseHelper.success_data_response(all_bundles, len(all_bundles))

    async def get_user_esim_by_order_id(self, order_id: str, user: UserModel) -> Response[EsimBundleResponse]:
        user_order = self.__user_order_repo.get_first_by({"user_id": user.id, "id": order_id})
        if not user_order:
            raise CustomException(code=404, name=f"Order Not Found", details="Order not found")
        if user_order.payment_status != OrderStatusEnum.SUCCESS:
            raise CustomException(code=400, name=f"Payment {user_order.payment_status}",
                                  details="Payment Failed Please try again")
        if user_order.order_status != OrderStatusEnum.SUCCESS:
            raise CustomException(code=400, name=f"Order {user_order.order_status}",
                                  details="Order Failed Please try again")
        profiles = self.__user_profile_repo.select(tables={DatabaseTables.TABLE_USER_PROFILE_BUNDLE: "*"},
                                                   where={"user_id": user.id, "user_order_id": order_id})
        if len(profiles) == 0:
            raise CustomException(code=404, name="Not Found", details="Order Not Found")
        return ResponseHelper.success_data_response(DtoMapper.to_esim_bundle_response(profiles[0]), 0)

    async def get_order_history(self, user_id: str, page_index: int, page_size: int) -> Response[
        List[UserOrderHistoryResponse]]:
        user_orders = self.__user_order_repo.list(
            where={"user_id": user_id, "payment_status": OrderStatusEnum.SUCCESS,
                   "order_status": OrderStatusEnum.SUCCESS}, limit=page_size,
            offset=((page_index - 1) * page_size))
        return ResponseHelper.success_data_response([DtoMapper.to_user_order_history(data) for data in user_orders],
                                                    len(user_orders))

    async def get_order_history_by_id(self, user_id: str, order_id: str) -> Response[UserOrderHistoryResponse]:
        order = self.__user_order_repo.get_first_by({"user_id": user_id, "id": order_id})
        payment_details = stripe_get_payment_details(order.payment_intent_code)
        user_order_history = DtoMapper.to_user_order_history(order)
        user_order_history.payment_details = payment_details
        return ResponseHelper.success_data_response(user_order_history, 1)

    async def cancel_order(self, order_id: str, user: UserModel) -> Response[None]:
        try:
            order = self.__user_order_repo.get_first_by({"user_id": user.id, "id": order_id})
            if not order:
                raise CustomException(code=404, name=f"Order Not Found", details="Order not found")
            self.__user_order_repo.update(order_id, {"order_status": OrderStatusEnum.CANCELED})
            stripe.PaymentIntent.cancel(order.payment_intent_code)
            return ResponseHelper.success_response()
        except Exception as e:
            raise CustomException(code=400, name=f" Error While Canceling Order {order_id}", details=str(e))

    async def resend_order_otp(self, user: UserModel, order_id: str) -> Response[None]:
        order = self.__user_order_repo.get_first_by({"user_id": user.id, "id": order_id})
        if not order:
            raise BadRequestException(f"Order {order_id} not found")
        await self.__dcb_service.resend_otp(msisdn=user.msisdn, transaction_id=order.id)
        return ResponseHelper.success_response()

    async def verify_order_otp(self, user: UserModel, request: VerifyOtpRequestDto) -> Response[None]:
        logger.info(f"receiving verification otp request {request}")
        user_order: UserOrderModel = self.__user_order_repo.get_by_id(record_id=request.order_id)
        if not user_order:
            raise BadRequestException("Order not found")

        bundle = BundleDTO.model_validate_json(user_order.bundle_data)

        try:
            await self.__dcb_service.verify_otp(msisdn=user.msisdn, order_id=user_order.id, otp=request.otp)
            if user_order.order_type == UserOrderType.ASSIGN:
                return await self.__bundle_service.buy_bundle(user_order=user_order, bundle=bundle, user_id=user.id,
                                                              payment_status=OrderStatusEnum.SUCCESS, user=user)
            elif user_order.order_type == UserOrderType.BUNDLE_TOP_UP:
                return await self.__bundle_service.top_up_bundle(bundle=bundle, user_id=user.id,
                                                                 payment_status=OrderStatusEnum.SUCCESS,
                                                                 user_order=user_order,
                                                                 iccid=request.iccid, user=user)
            else:
                raise BadRequestException("Invalid Order Type")
        except Exception as e:
            logger.error("Failed to get content tag: {}".format(e))
            if isinstance(e, DCBException):
                raise e
            raise BadRequestException(f"failed to verify otp: {e}")

    async def __handle_wallet_payment(self, user: UserModel, bundle: BundleDTO, user_order: UserOrderModel) -> Response[
        PaymentIntentResponse]:
        wallet = await self.__user_wallet_service.get_user_wallet_by_user_id(user_id=user.id)
        if wallet.balance < bundle.price:
            raise BadRequestException("You don't have enough funds to pay")
        try:
            await self.__user_wallet_service.add_wallet_transaction(amount=(bundle.price * -1), user_id=user.id,
                                                                    source="Assign_Bundle")
            await self.__bundle_service.buy_bundle(user_order=user_order, bundle=bundle, user_id=user.id,
                                                   payment_status=OrderStatusEnum.SUCCESS, user=user)
            response = PaymentIntentResponse(order_id=user_order.id)
            return ResponseHelper.success_data_response(response, 0)
        except Exception as e:
            raise CustomException(code=400, name="Error Creating Order", details=f"Error while creating order: {e}")

    async def __handle_dcb_payment(self, user: UserModel, user_order: UserOrderModel, bundle: BundleDTO) -> Response[
        PaymentIntentResponse]:
        otp = generate_otp()
        self.__user_order_repo.update_by(where={"id": user_order.id}, data={"otp": otp})
        msisdn = user.msisdn
        logger.info(f"requesting new otp for msisdn: {msisdn}")
        await self.__dcb_service.payment_request(user_msisdn=user.msisdn,
                                                 merchant_msisdn="0992164444",
                                                 order_id=user_order.id, amount=bundle.price)

        response = PaymentIntentResponse(order_id=user_order.id)
        return ResponseHelper.success_data_response(response, 0)
