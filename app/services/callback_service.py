import json
import json
import os
import threading
from datetime import datetime
from typing import Dict

import stripe
from fastapi import Request, HTTPException
from jinja2 import Environment, FileSystemLoader
from loguru import logger

from app.config.config import STRIPE_WEBHOOK_SECRET, esim_hub_service_instance, send_email
from app.config.db import DatabaseTables
from app.config.notification_types import send_consumption_80_bundle_notification, \
    send_consumption_100_bundle_notification, send_plan_started_notification, \
    send_wallet_top_up_failed_notification
from app.config.push_notification_manager import fcm_service
from app.models.user import OrderStatusEnum, UserOrderType, UsersCopyModel
from app.repo import UserOrderRepo, UserProfileRepo, UserProfileBundleRepo, UserRepo
from app.schemas.callback import ConsumptionLimitRequest, NotificationCategoryType
from app.schemas.dto_mapper import DtoMapper
from app.schemas.home import BundleDTO
from app.schemas.response import ResponseHelper
from app.services.bundle_service import BundleService
from app.services.promotion_service import PromotionService
from app.services.sync_service import SyncService
from app.services.user_wallet_service import UserWalletService


class CallbackService:

    def __init__(self):
        self.__esim_hub_service = esim_hub_service_instance()
        self.__user_repo = UserRepo()
        self.__user_order_repo = UserOrderRepo()
        self.__user_profile_repo = UserProfileRepo()
        self.__user_profile_bundle_repo = UserProfileBundleRepo()
        self.__sync_service = SyncService()
        self.__user_wallet_service = UserWalletService()
        self.__promotion_service = PromotionService()
        self.__bundle_service = BundleService()

    async def handle_plan_event_callback(self, callback_request: Request):
        try:
            request_data = await callback_request.json()
            logger.info(f"received 'handle_plan_event_callback' request: {request_data}")
            request = ConsumptionLimitRequest.model_validate(request_data)
            event_type = request.event_type
            iccid = request.iccid

            # Get user profile information
            orders = self.__user_profile_repo.select(tables={DatabaseTables.TABLE_USER_PROFILE_BUNDLE: "*"},
                                                     where={"esim_hub_order_id": request.order_id, "iccid": iccid})

            if len(orders) == 0:
                logger.warning(f"No user profile found for esim_hub_order_id {request.order_id} and iccid {iccid}")
                return

            order_info = orders[0]
            orders = []

            # Get primary user info
            primary_user_id = order_info.user_id
            if primary_user_id:
                primary_user_metadata = {}
                primary_user = self.__user_repo.get_by_id(
                    record_id=primary_user_id)  # get_user(primary_user_id)
                if primary_user:
                    primary_user_metadata = primary_user.metadata
                model = DtoMapper.to_order_notification_model(order_info, primary_user_id, primary_user_metadata, iccid)
                orders.append(model)
            # Get shared user info if exists
            shared_user_id = order_info.shared_user_id
            if shared_user_id:
                shared_user_metadata = {}
                shared_user = self.__user_repo.get_by_id(record_id=shared_user_id)  # get_user(shared_user_id)
                if shared_user:
                    shared_user_metadata = shared_user.metadata
                model = DtoMapper.to_order_notification_model(order_info, shared_user_id, shared_user_metadata, iccid)
                orders.append(model)

            if not orders:
                logger.warning(f"No users found for ICCID: {iccid}")
                return

            # Send notification to all associated users
            for order in orders:
                try:
                    # Get bundle info from user profile
                    # Prepare notification data based on event type
                    if event_type == NotificationCategoryType.CONSUMPTION80.value:
                        notification_data = send_consumption_80_bundle_notification(
                            user_name=order.user_display_name,
                            bundle_name=order.bundle_display_name,
                            iccid=iccid
                        )
                        user = self.__user_repo.get_by_id(record_id=order.user_id)
                        await self.__send_email_80_consumption(
                            user=user,
                            bundle_name=order.bundle_display_name,
                            iccid=iccid
                        )
                    elif event_type == NotificationCategoryType.CONSUMPTION100.value:
                        notification_data = send_consumption_100_bundle_notification(
                            user_name=order.user_display_name,
                            bundle_name=order.bundle_display_name,
                            iccid=iccid
                        )
                        user = self.__user_repo.get_by_id(record_id=order.user_id)
                        await self.__send_email_100_consumption(
                            user=user,
                            bundle_name=order.bundle_display_name,
                            iccid=iccid
                        )
                    elif event_type == NotificationCategoryType.BUNDLE_STARTED.value:
                        datetime_str = order_info.validity
                        dt_object = datetime.strptime(datetime_str, "%Y-%m-%dT%H:%M:%S")
                        date_only_str = dt_object.strftime("%Y-%m-%d")
                        notification_data = send_plan_started_notification(
                            bundle_name=order.bundle_display_name,
                            validity_date=date_only_str
                        )
                    else:
                        logger.warning(f"Unsupported event type for plan status callback: {event_type}")
                        return

                    fcm_service.send_notification_to_user_from_template(
                        content_template=notification_data,
                        user_id=order.user_id
                    )
                except Exception as e:
                    logger.error(f"Failed to send notification to user {order.user_id}: {str(e)}")

        except Exception as e:
            logger.error(f"Error in handle_plan_event_callback: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    async def handle_payment_webhook(self, request: Request):
        # Extract and parse the payload
        payload = await request.body()
        sig_header = request.headers.get("stripe-signature")
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except ValueError:
            logger.error("Invalid Stripe webhook payload.")
            raise HTTPException(status_code=400, detail="Invalid payload")
        except stripe.error.SignatureVerificationError:
            logger.error("Stripe webhook signature verification failed.")
            raise HTTPException(status_code=400, detail="Invalid signature")
        return await self.__handle_payment_webhook_data(event)

    async def handle_payment_webhook_fake(self, request: Request):
        try:
            payload = await request.body()
            payload_json = json.loads(payload)
        except Exception as e:
            logger.error(e)
            raise HTTPException(status_code=400, detail="Invalid payload")
        await self.__handle_payment_webhook_data(payload_json)

    async def handle_sync_all_bundles(self, page_index=1):
        thread = threading.Thread(target=self.__run_full_sync, args=(page_index,))
        thread.start()
        return ResponseHelper.success_response()

    async def handle_sync_one_bundle(self, id: str):
        logger.info(f"receiving bundle sync request {id}")
        thread = threading.Thread(target=self.__run_one_sync, args=(id,))
        thread.start()
        return ResponseHelper.success_response()

    async def handle_sync_bundle(self, request: Request):
        logger.info(f"receiving bundle sync request {request}")
        # thread = threading.Thread(target=self.__run_one_sync, args=(id,))
        # thread.start()
        return ResponseHelper.success_response()

    def __run_one_sync(self, id: str):
        import asyncio
        try:
            bundle = asyncio.run(
                self.__esim_hub_service.get_bundle_by_id(bundle_id=id, currency_code=os.getenv("DEFAULT_CURRENCY")))
            asyncio.run(self.__sync_service.sync_bundle(bundle))
            asyncio.run(self.__sync_service.update_sync_version())
        except Exception as e:
            logger.error(f"error while syncing bundle {id}: {str(e)}")

    def __run_full_sync(self, page_index=1):
        import asyncio
        asyncio.run(self.__sync_service.sync_bundles(page_index=page_index))
        asyncio.run(self.__sync_service.update_sync_version())

    async def __handle_payment_webhook_data(self, event: dict):
        # Extract payment intent data
        logger.debug(f"Received payment webhook.{event.get('type')}")
        if event.get("type") not in ["payment_intent.succeeded", "payment_intent.failed"]:
            logger.info(f"Ignoring payment intent {event.get('type')}")
            return ResponseHelper.success_response()

        payment_intent = event.get("data").get("object", {})
        metadata = payment_intent.get("metadata", {})

        # Validate required metadata fields
        environment = metadata.get("env")
        if environment != os.getenv("ENVIRONMENT", "DEV"):
            logger.info(
                f"Ignoring payment webhook for ({environment}) running environment({os.getenv('ENVIRONMENT', 'DEV')})")
            return ResponseHelper.success_response()
        if metadata.get("user_wallet_id", None):
            return await self.__handle_wallet_top_up(metadata, event.get("type"))
        await self.__check_metadata_fields(metadata)
        order_id = metadata.get("order_id")
        user_id = metadata.get("user_id")
        order_type = metadata.get("order_type")
        iccid = metadata.get("iccid", None)
        promo_code = metadata.get("promo_code", None)
        rule_id = metadata.get("rule_id", None)
        amount = metadata.get("amount", None)
        user_order = self.__user_order_repo.get_by_id(order_id)
        bundle = BundleDTO.model_validate_json(user_order.bundle_data)
        payment_status = OrderStatusEnum.SUCCESS if event.get(
            "type") == "payment_intent.succeeded" else OrderStatusEnum.FAILURE
        if payment_status == OrderStatusEnum.FAILURE:
            logger.info(f"payment failed for order {order_id}")
            if promo_code:
                self.__promotion_service.update_promotion_usage(user_id, promo_code, "failed", rule_id, amount)
            return HTTPException(status_code=200, detail="Payment Failed")

        if payment_status == OrderStatusEnum.SUCCESS and order_type == UserOrderType.ASSIGN:
            await self.__promotion_service.check_referral_rewards_after_buy_bundle(user_id)
            if promo_code:
                self.__promotion_service.update_promotion_usage(user_id, promo_code, "completed", rule_id, amount)
            return await self.__bundle_service.buy_bundle(user_order=user_order, bundle=bundle,
                                                          payment_status=payment_status,
                                                          user_id=user_id)

        elif payment_status == OrderStatusEnum.SUCCESS and order_type == UserOrderType.BUNDLE_TOP_UP:
            if not iccid:
                logger.error(f"invalid iccid ({iccid}) for topup request ({user_order.id})")
                return HTTPException(status_code=400, detail="Invalid iccid")
            return await self.__bundle_service.top_up_bundle(bundle=bundle, user_order=user_order, iccid=iccid,
                                                             user_id=user_id,
                                                             payment_status=payment_status)
        return ResponseHelper.success_response()

    async def __check_metadata_fields(self, metadata: dict):
        if not all([metadata["order_id"], metadata["user_id"], metadata["bundle_code"]]):
            logger.error(f"Missing metadata fields: {metadata}")
            raise HTTPException(status_code=400, detail="Missing order details in metadata")

    async def __send_email_80_consumption(self, user: UsersCopyModel, bundle_name, iccid):
        try:
            msisdn = os.getenv("WHATSAPP_NUMBER").replace("+", "").replace("-", "").replace(" ", "")

            data = {
                "user": user.metadata.get("email", user.email),
                "bundle_name": bundle_name,
                "montyesim_msisdn": msisdn,
                "iccid": iccid
            }

            env = Environment(loader=FileSystemLoader('app/email_templates'))
            template = env.get_template('80_percent_email_template.htm')
            html_content = template.render(data=data)
            send_email(subject="80% Consumption", html_content=html_content,
                       recipients=user.metadata.get("email", user.email))
        except Exception as e:
            logger.error(f"error while sending email {str(e)}")

    async def __send_email_100_consumption(self, user: UsersCopyModel, bundle_name, iccid):
        try:
            msisdn = os.getenv("WHATSAPP_NUMBER").replace("+", "").replace("-", "").replace(" ", "")

            data = {
                "user": user.metadata.get("email", user.email),
                "bundle_name": bundle_name,
                "montyesim_msisdn": msisdn,
                "iccid": iccid
            }

            env = Environment(loader=FileSystemLoader('app/email_templates'))
            template = env.get_template('expiry_email_template.htm')
            html_content = template.render(data=data)
            send_email(subject="100% Consumption", html_content=html_content,
                       recipients=user.metadata.get("email", user.email))
        except Exception as e:
            logger.error(f"error while sending email {str(e)}")

    async def __handle_wallet_top_up(self, metadata: Dict[str, str], event_type: str):

        user_wallet_id = metadata.get("user_wallet_id")
        user_id = metadata.get("user_id")
        order_id = metadata.get("order_id")
        order = self.__user_order_repo.get_by_id(order_id)
        user_wallet = self.__user_wallet_service.get_user_wallet_by_id(user_wallet_id)
        try:
            if event_type == "payment_intent.succeeded":
                amount = (order.amount / 100)
                logger.info(f"updating user wallet: {user_wallet} with new {amount=}")
                await self.__user_wallet_service.add_wallet_transaction(amount, user_id)
                self.__user_order_repo.update(order_id, {"payment_status": OrderStatusEnum.SUCCESS})
                logger.info(f"Top-Up for user {user_id} wallet {user_wallet} with amount {amount} {order.currency} succeeded")
                return ResponseHelper.success_response()
            else:
                self.__user_order_repo.update(order_id, {"payment_status": OrderStatusEnum.FAILURE})
                logger.info(
                    f"Payment Failed for Wallet Top-Up for user {user_id} with amount {order.amount} {order.currency}")
                content = send_wallet_top_up_failed_notification()
                fcm_service.send_notification_to_user_from_template(content, user_id=user_id)
                return ResponseHelper.success_response()
        except Exception as e:
            logger.error(f"error while updating user wallet {str(e)}")
            content = send_wallet_top_up_failed_notification()
            fcm_service.send_notification_to_user_from_template(content, user_id=user_id)
            return ResponseHelper.success_response()
