import asyncio
import os
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import List

import bleach
import stripe
from fastapi import Request
from loguru import logger

from app.config.config import esim_hub_service_instance, generate_otp, dcb_service_instance
from app.config.constants import ErrorMessages, PaymentStatusEnum, UserWalletTransactionSource
from app.config.db import DatabaseTables, PaymentTypeEnum
from app.config.utils import create_payment_intent, create_payment_ephemeral, stripe_get_payment_details
from app.exceptions import BadRequestException, CustomException
from app.models.user import UserModel, UserOrderType, OrderStatusEnum, UserOrderModel, UsersCopyModel
from app.repo import NotificationRepo, UserOrderRepo, UserProfileRepo, UserProfileBundleRepo, UserRepo
from app.repo.bundle_repo import BundleRepo
from app.schemas.app import UserNotificationResponse
from app.schemas.bundle import AssignRequest, AssignTopUpRequest, PaymentIntentResponse, EsimBundleResponse, \
    ConsumptionResponse, UserOrderHistoryResponse, UpdateBundleLabelRequest, VerifyOtpRequestDto
from app.schemas.dto_mapper import DtoMapper
from app.schemas.home import BundleDTO
from app.schemas.response import Response, ResponseHelper
from app.services.bundle_service import BundleService
from app.services.currency_service import CurrencyService
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
        self.__currency_service = CurrencyService()
        self.__user_repo = UserRepo()

    async def assign(self, user: UserModel, device_id: str, assign_request: AssignRequest, x_currency: str,
                     locale: str, request: Request) -> Response[PaymentIntentResponse] | Response[bool]:

        bundle = await self.__esim_hub_service.get_bundle_by_id(bundle_id=assign_request.bundle_code)
        if not bundle or not bundle.is_active:
            raise CustomException(code=400, name=ErrorMessages.BUNDLE_NOT_AVAILABLE,
                                  details=ErrorMessages.BUNDLE_NOT_AVAILABLE)
        if not bundle.is_stockable:
            check_bundle_available = await self.__esim_hub_service.check_bundle_applicable(bundle.bundle_info_code)
            if not check_bundle_available:
                raise CustomException(code=400, name=ErrorMessages.BUNDLE_NOT_AVAILABLE,
                                      details=ErrorMessages.BUNDLE_NOT_AVAILABLE)
        rate = self.__currency_service.get_rate_by_currency(x_currency)
        modified_amount = bundle.price
        amount = bundle.price
        rule_id = "0"

        data = {
            "user_id": user.id,
            "bundle_id": assign_request.bundle_code,
            "order_type": UserOrderType.ASSIGN,
            "amount": int(round(amount * 100)),
            "modified_amount": int(round(modified_amount * 100)),
            "currency": os.getenv("DEFAULT_CURRENCY"),
            "bundle_data": bundle.model_dump_json(),
            "searched_countries": assign_request.related_search.model_dump_json(),
            "anonymous_user_id": user.anonymous_user_id,
            "promo_code": assign_request.promo_code or None,
        }
        if assign_request.promo_code and self.__promotion_service.is_referral_code(assign_request.promo_code):
            data.setdefault("referral_code", assign_request.promo_code)
            data.pop("promo_code")

        order = self.__user_order_repo.create(data)

        if assign_request.promo_code:
            if self.__promotion_service.is_referral_code(assign_request.promo_code):
                self.__check_if_user_eligible_for_referral(user=user, promo_code=assign_request.promo_code)
            validation_response = await self.__promotion_service.validate_promo_code(code=assign_request.promo_code,
                                                                                     user_id=user.id, bundle=bundle,
                                                                                     device_id=device_id,
                                                                                     currency=x_currency,
                                                                                     apply_usage=True,
                                                                                     order_id=order.id)
            logger.info(f"applying promo code {assign_request.promo_code} with {validation_response.message}")
            bundle = validation_response.bundle
            modified_amount = bundle.original_price * rate
            amount = bundle.original_price * rate
            rule_id = validation_response.rule_id
            logger.info(f"scheduling background update for order {order.id}")
            # Create background task for order update
            asyncio.create_task(
                self.__update_order_with_delay(
                    order_id=order.id,
                    bundle=bundle,
                    rate=rate
                )
            )

        payment_type = assign_request.payment_type

        if modified_amount == 0:
            await self.__bundle_service.buy_bundle(user_order=order, bundle=bundle, user_id=user.id,
                                                   payment_status=OrderStatusEnum.SUCCESS, user=user,
                                                   promo_code=assign_request.promo_code, rule_id=rule_id)
            response = PaymentIntentResponse(order_id=order.id, payment_status=PaymentStatusEnum.COMPLETED)
            return ResponseHelper.success_data_response(response, 0)

        if payment_type == PaymentTypeEnum.WALLET:
            return await self.__handle_wallet_payment(user=user, bundle=bundle, user_order=order)
        elif payment_type == PaymentTypeEnum.DCB:
            return await self.__handle_dcb_payment(user=user, bundle=bundle, user_order=order)
        elif payment_type == PaymentTypeEnum.CARD:
            return await self.__handle_card_payment(user=user, order=order, device_id=device_id,
                                                    assign_request=assign_request, rule_id=rule_id,
                                                    modified_amount=modified_amount, request=request)
        else:
            raise CustomException(code=400, name=ErrorMessages.INVALID_PAYMENT_TYPE,
                                  details=f"Payment type {payment_type} is not supported")

    async def assign_top_up(self, user: UserModel, assign_top_up_request: AssignTopUpRequest, device_id: str,
                            request: Request, x_currency: str, locale: str) -> Response:
        bundle_response = await self.__bundle_service.get_bundle(bundle_id=assign_top_up_request.bundle_code,
                                                                 currency_name=x_currency, locale=locale)
        bundle = bundle_response.data

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
            return await self.__handle_wallet_payment(user=user, bundle=bundle, user_order=order,
                                                      iccid=assign_top_up_request.iccid)
        elif payment_type == PaymentTypeEnum.DCB:
            return await self.__handle_dcb_payment(user=user, bundle=bundle, user_order=order)
        elif payment_type == PaymentTypeEnum.CARD:
            return await self.__handle_card_payment(user=user, order=order, device_id=device_id,
                                                    assign_request=None, rule_id="0",
                                                    modified_amount=bundle.price, request=request,
                                                    iccid=assign_top_up_request.iccid)
        else:
            raise CustomException(code=400, name=ErrorMessages.INVALID_PAYMENT_TYPE,
                                  details=f"Payment type {payment_type} is not supported")

    async def get_user_esims(self, user: UserModel, x_currency: str) -> Response[List[EsimBundleResponse]]:
        user_profiles = self.__user_profile_repo.select(tables={DatabaseTables.TABLE_USER_PROFILE_BUNDLE: "*"},
                                                        where={"user_id": user.id})
        esim_bundle_response = []
        rate = self.__currency_service.get_rate_by_currency(x_currency)
        for profile in user_profiles:
            try:
                bundle = DtoMapper.to_esim_bundle_response(user_profile=profile, x_currency=x_currency, rate=rate)
                if bundle is not None:
                    esim_bundle_response.append(bundle)
            except Exception as e:
                logger.error(e)
                logger.error(f"Failed to map profile {profile.id if hasattr(profile, 'id') else 'unknown'}: {e}")
        return ResponseHelper.success_data_response(esim_bundle_response, len(esim_bundle_response))

    async def get_user_esim(self, iccid: str, user: UserModel, x_currency: str) -> Response[EsimBundleResponse | None]:
        user_profiles = self.__user_profile_repo.select(tables={DatabaseTables.TABLE_USER_PROFILE_BUNDLE: "*"},
                                                        where={"user_id": user.id, "iccid": iccid})
        if len(user_profiles) == 0:
            raise CustomException(code=404, name=ErrorMessages.USER_PROFILE_NOT_FOUND, details="user profile not found")
        rate = self.__currency_service.get_rate_by_currency(x_currency)
        return ResponseHelper.success_data_response(
            DtoMapper.to_esim_bundle_response(user_profiles[0], rate, x_currency), 0)

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
        logger.info(f"read user notification for user {user.email=} {device_id=}")
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
            raise CustomException(code=400, name=ErrorMessages.USER_PROFILE_BUNDLE_NOT_FOUND,
                                  details="Bundle Not Found")
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
            raise CustomException(code=400, name=ErrorMessages.USER_PROFILE_BUNDLE_NOT_FOUND,
                                  details="Bundle Not Found")
        bundle = BundleDTO.model_validate(user_profile_bundle.bundle_data)
        bundle.label = bleach.clean(bundle_label_request.label)
        self.__user_profile_bundle_repo.update_by(
            where={"user_id": user.id, "iccid": iccid},
            data={"bundle_data": bundle.model_dump()})
        return ResponseHelper.success_response()

    async def get_topup_related_bundle(self, bundle_code: str, iccid: str, user: UserModel, accept_language: str = "en",
                                       currency_code: str = os.getenv("DEFAULT_CURRENCY")) -> Response[
        List[BundleDTO]]:
        logger.info(f"get_topup_related_bundle {bundle_code=} {iccid=} {user=}")
        profile = self.__user_profile_repo.get_first_by({"user_id": user.id, "iccid": iccid})
        if not profile:
            raise BadRequestException(details="This ICCID is not linked to this user")
        bundles = await self.__esim_hub_service.get_topup_related_bundles(order_id=profile.esim_hub_order_id)
        all_bundles = []
        for bundle in bundles:
            if await self.__bundle_service.bundle_exists(bundle.bundle_code):
                local_bundle = await self.__bundle_service.get_bundle(bundle_id=bundle.bundle_code,
                                                                      currency_name=currency_code,
                                                                      locale=accept_language)
                logger.debug(f"local bundle {local_bundle.data}")
                all_bundles.append(local_bundle.data)

        return ResponseHelper.success_data_response(all_bundles, len(all_bundles))

    async def get_user_esim_by_order_id(self, order_id: str, user: UserModel, x_currency: str) -> Response[
        EsimBundleResponse]:
        user_order = self.__user_order_repo.get_first_by({"user_id": user.id, "id": order_id})
        if not user_order:
            raise CustomException(code=404, name=ErrorMessages.ORDER_NOT_FOUND, details=ErrorMessages.ORDER_NOT_FOUND)
        if user_order.payment_status != OrderStatusEnum.SUCCESS:
            raise CustomException(code=400, name=ErrorMessages.PAYMENT_FAILED,
                                  details=ErrorMessages.PAYMENT_FAILED)
        if user_order.order_status != OrderStatusEnum.SUCCESS:
            raise CustomException(code=400, name=ErrorMessages.ORDER_FAILED,
                                  details=ErrorMessages.ORDER_FAILED)
        profiles = self.__user_profile_repo.select(tables={DatabaseTables.TABLE_USER_PROFILE_BUNDLE: "*"},
                                                   where={"user_id": user.id, "user_order_id": order_id})
        if len(profiles) == 0:
            raise CustomException(code=404, name=ErrorMessages.USER_PROFILE_NOT_FOUND,
                                  details=ErrorMessages.ORDER_NOT_FOUND)
        rate = self.__currency_service.get_rate_by_currency(x_currency)
        return ResponseHelper.success_data_response(
            DtoMapper.to_esim_bundle_response(user_profile=profiles[0], rate=rate, x_currency=x_currency), 0)

    async def get_order_history(self, user_id: str, page_index: int, page_size: int, x_currency: str) -> Response[
        List[UserOrderHistoryResponse]]:
        rate = self.__currency_service.get_currency_rate(os.getenv("DEFAULT_CURRENCY"), to_currency=x_currency)
        user_orders = self.__user_order_repo.list(
            where={"user_id": user_id, "payment_status": OrderStatusEnum.SUCCESS,
                   "order_status": OrderStatusEnum.SUCCESS}, limit=page_size,
            offset=((page_index - 1) * page_size))
        return ResponseHelper.success_data_response(
            [DtoMapper.to_user_order_history(user_order=data, rate=rate, currency=x_currency) for data in
             user_orders],
            len(user_orders))

    async def get_order_history_by_id(self, user_id: str, order_id: str, x_currency: str) -> Response[
        UserOrderHistoryResponse]:
        order = self.__user_order_repo.get_first_by({"user_id": user_id, "id": order_id})
        rate = self.__currency_service.get_currency_rate(from_currency=order.currency, to_currency=x_currency)
        payment_details = stripe_get_payment_details(order.payment_intent_code)
        user_order_history = DtoMapper.to_user_order_history(user_order=order, rate=rate, currency=x_currency)
        user_order_history.payment_details = payment_details
        return ResponseHelper.success_data_response(user_order_history, 1)

    async def cancel_order(self, order_id: str, user: UserModel) -> Response[None]:
        try:
            order: UserOrderModel = self.__user_order_repo.get_first_by({"user_id": user.id, "id": order_id})
            if not order:
                raise CustomException(code=404, name=ErrorMessages.ORDER_NOT_FOUND,
                                      details=ErrorMessages.ORDER_NOT_FOUND)
            self.__user_order_repo.update(order_id, {"order_status": OrderStatusEnum.CANCELED,
                                                     "payment_status": OrderStatusEnum.CANCELED})
            self.__promotion_service.cancel_promotion_usage(order_id=order_id)
            stripe.PaymentIntent.cancel(order.payment_intent_code)
            return ResponseHelper.success_response()
        except Exception as e:
            raise CustomException(code=400, name=ErrorMessages.REQUEST_FAILED, details=str(e))

    async def verify_order_otp(self, user: UserModel, request: VerifyOtpRequestDto) -> Response[bool]:
        logger.info(f"receiving verification otp request {request}")
        user_order: UserOrderModel = self.__user_order_repo.get_by_id(record_id=request.order_id)
        if not user_order:
            raise BadRequestException("Order not found")
        if user_order.otp != request.otp:
            raise BadRequestException("Invalid OTP")

        bundle = BundleDTO.model_validate_json(user_order.bundle_data)
        response = self.__dcb_service.deduct_balance(msisdn=user.msisdn, amount=user_order.amount)
        payment_status = OrderStatusEnum.SUCCESS if response else OrderStatusEnum.FAILURE
        return await self.__bundle_service.buy_bundle(user_order=user_order, bundle=bundle, user_id=user.id,
                                                      payment_status=payment_status, user=user)

    async def __handle_wallet_payment(self, user: UserModel, bundle: BundleDTO, user_order: UserOrderModel,
                                      iccid: str = None) -> Response[
        PaymentIntentResponse]:
        wallet = await self.__user_wallet_service.get_user_wallet_by_user_id(user_id=user.id)
        if wallet.balance < bundle.price:
            raise BadRequestException("You don't have enough funds to pay")
        try:
            await self.__user_wallet_service.add_wallet_transaction(amount=(bundle.price * -1), user_id=user.id,
                                                                    source=UserWalletTransactionSource.PURCHASE_BUNDLE)
            if user_order.order_type == UserOrderType.ASSIGN:
                await self.__bundle_service.buy_bundle(user_order=user_order, bundle=bundle, user_id=user.id,
                                                       payment_status=OrderStatusEnum.SUCCESS, user=user)
            elif user_order.order_type == UserOrderType.BUNDLE_TOP_UP:
                await self.__bundle_service.top_up_bundle(user_order=user_order, bundle=bundle, user_id=user.id,
                                                          payment_status=OrderStatusEnum.SUCCESS, user=user,
                                                          iccid=iccid)
            response = PaymentIntentResponse(order_id=user_order.id, payment_status=PaymentStatusEnum.COMPLETED)
            return ResponseHelper.success_data_response(response, 0)
        except Exception as e:
            raise CustomException(code=400, name=ErrorMessages.REQUEST_FAILED,
                                  details=f"Error while creating order: {e}")

    async def __handle_dcb_payment(self, user: UserModel, user_order: UserOrderModel, bundle: BundleDTO) -> Response[
        PaymentIntentResponse]:
        logger.info(f"handle_dcb_payment request {user=} {bundle=} {user_order=}")
        try:
            otp = generate_otp()
            self.__user_order_repo.update_by(where={"id": user_order.id}, data={"otp": otp})
            msisdn = user.msisdn
            logger.info(f"requesting new otp for msisdn: {msisdn}")
            self.__dcb_service.send_otp(msisdn=msisdn, otp=otp)
            response = PaymentIntentResponse(order_id=user_order.id, payment_status=PaymentStatusEnum.COMPLETED)
            return ResponseHelper.success_data_response(response, 0)
        except Exception as e:
            raise CustomException(code=400, name=ErrorMessages.REQUEST_FAILED,
                                  details=f"Error while creating order: {e}")

    async def __handle_card_payment(self, user: UserModel, order: UserOrderModel, device_id: str,
                                    assign_request: AssignRequest | None, rule_id: str, modified_amount: float,
                                    request: Request, iccid: str = None) -> Response:
        amount = Decimal(str(modified_amount))
        minor_units = (amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        minor_units = int(minor_units)
        metadata = {
            "order_id": order.id,
            "user_id": order.user_id,
            "device_id": device_id,
            "bundle_code": order.bundle_id,
            "order_type": order.order_type,
            "env": os.environ.get("ENVIRONMENT", "DEV"),
            "promo_code": assign_request.promo_code if assign_request else None,
            "rule_id": rule_id,
            "amount": minor_units
        }
        if order.order_type == UserOrderType.BUNDLE_TOP_UP and iccid:
            metadata["iccid"] = iccid
        payment_intent, tax = create_payment_intent(user_bundle_order=order, user_email=user.email,
                                                    metadata=metadata,
                                                    ip_address=request.client.host)
        order.payment_intent_code = payment_intent.id
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
                                         order_id=order.id,
                                         subtotal_price_display=f"{minor_units / 100} {order.currency}",
                                         total_price_display=f"{payment_intent.amount / 100} {order.currency}",
                                         tax_price_display=f"{round(tax.amount_total if tax else 0, 2)} {order.currency}",
                                         has_tax=tax is not None and tax.amount_total > 0
                                         )
        return ResponseHelper.success_data_response(response, 0)

    def __check_if_user_eligible_for_referral(self, user: UserModel, promo_code: str):
        old_profiles = self.__user_profile_repo.list(where={"user_id": user.id})
        if len(old_profiles) > 0:
            raise CustomException(code=400, name=ErrorMessages.USER_HAS_PREVIOUS_ESIM,
                                  details="User already purchased esim before, cannot use referral code")
        user_model: UsersCopyModel = self.__user_repo.get_by_id(user.id)
        if user_model.metadata["referral_code"] and user_model.metadata["referral_code"] == promo_code:
            raise CustomException(code=400, name=ErrorMessages.OWN_REFERRAL_CODE_CANNOT_BE_USED,
                                  details="Own Referral Code Can not be used")

    async def __update_order_with_delay(self, order_id: str, bundle: BundleDTO, rate: float):
        """Background task to update order with delay"""
        await asyncio.sleep(5)
        try:
            modified_amount = bundle.original_price * rate
            amount = bundle.original_price * rate
            logger.info(f"Updating order {order_id} with delayed background task at {datetime.now()}")
            self.__user_order_repo.update_by(where={"id": order_id}, data={
                "amount": int(round(amount * 100)),
                "modified_amount": int(round(modified_amount * 100)),
                "bundle_data": bundle.model_dump_json()
            })
            logger.info(f"Successfully updated order {order_id} in background")
        except Exception as e:
            logger.error(f"Error updating order {order_id} in background: {str(e)}")
