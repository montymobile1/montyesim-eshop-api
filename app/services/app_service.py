import os
from typing import List

import bleach
from fastapi import Request
from loguru import logger

from app.config.config import esim_hub_service_instance, send_email
from app.config.db import ConfigKeysEnum, PaymentTypeEnum
from app.exceptions import CustomException
from app.models.app import DeviceModel
from app.models.user import UserModel
from app.repo.config_repo import ConfigRepo
from app.repo.contact_us_repo import ContactUsRepo
from app.repo.device_repo import DeviceRepo
from app.schemas.app import DeviceRequest, ContactUsRequest, DeleteDeviceRequest, GlobalConfiguration
from app.schemas.app import FaqResponse, PageContentResponse
from app.schemas.dto_mapper import DtoMapper
from app.schemas.response import ResponseHelper, Response


class AppService:

    def __init__(self):
        self.__esim_hub_service = esim_hub_service_instance()
        self.__contact_us_repo = ContactUsRepo()
        self.__device_repo = DeviceRepo()
        self.__config_repo = ConfigRepo()

    async def add_device(self, user: UserModel | None, device_id: str, device_request: DeviceRequest,
                         request: Request) -> \
            Response[None]:
        user_id = None
        if user:
            user_id = user.id
        ip = (
                request.headers.get("X-Forwarded-For")
                or request.headers.get("X-Real-IP")
                or request.client.host
        )

        device_model = DeviceModel(
            **device_request.model_dump(),  # This will unpack all fields from device_request
            is_logged_in=True if user else False,
            originated_ip=ip,
            ip_location="New York, USA",
            device_id=device_id,
            user_id=user_id,
        )

        # if device_id is null we need to check if it already exist to update values, since upsert doesnt work for Null values
        if user_id is None:
            update_response = self.__device_repo.update_by(where={"device_id": device_id},
                                                           data=device_model.model_dump(
                                                               exclude={"timestamp_login", "timestamp_logout"}))
            # Check if any rows were updated
            if update_response and len(update_response) > 0:
                return ResponseHelper.success_response()

        logger.info("No existing Device row found, performing upsert...")
        device_model.user_id = user_id
        upsert_response = self.__device_repo.upsert(
            data=device_model.model_dump(exclude={"timestamp_login", "timestamp_logout"}),
            on_conflict="device_id,user_id")

        logger.info("Upsert successful:", upsert_response)
        return ResponseHelper.success_response()

    async def delete_device(self, user: UserModel, delete_device_request: DeleteDeviceRequest) -> Response:
        return ResponseHelper.success_response()

    async def faq(self, accepted_language: str) -> Response[List[FaqResponse]]:
        results = await self.__esim_hub_service.get_content_tags(tag="FAQ", lang_code=accepted_language)
        faqs = []
        for item in results:
            if len(item.children) == 0:
                continue
            faqs.append(
                FaqResponse(
                    question=item.contentDetails[0].name,
                    answer=item.children[0].contentDetails[0].name
                )
            )
        faqs.reverse()
        return ResponseHelper.success_data_response(faqs, len(faqs))

    async def about_us(self, accepted_language: str) -> Response[PageContentResponse]:
        response = await self.__esim_hub_service.get_content_tag("ABOUT_US", accepted_language)
        return ResponseHelper.success_data_response(DtoMapper.to_page_content_response(response), 1)

    async def contact_us(self, contact_us_request: ContactUsRequest):
        response = self.__contact_us_repo.create({
            "email": contact_us_request.email,
            "content": bleach.clean(contact_us_request.content),
        })
        if not response:
            raise CustomException(code=400, details="Bad Request", name="Message was not submitted")
        content = f"""
            <h1>Received New Email Message</h1>
            <p><b>From</b>: {contact_us_request.email}</p>
            <p><b>Content</b>: {contact_us_request.content}</p>
        """
        send_email(subject="New Email Received", html_content=content, recipients=os.getenv("SUPPORT_EMAIL"))
        return ResponseHelper.success_response()

    async def terms_and_conditions(self, accepted_language) -> Response[PageContentResponse]:
        response = await self.__esim_hub_service.get_content_tag("TERM_CONDITION", accepted_language)
        return ResponseHelper.success_data_response(DtoMapper.to_page_content_response(response), 1)

    async def privacy_policy(self, accepted_language: str) -> Response[PageContentResponse]:
        response = await self.__esim_hub_service.get_content_tag("PRIVACY_POLICY", accepted_language)
        return ResponseHelper.success_data_response(DtoMapper.to_page_content_response(response), 1)

    async def user_guide(self):
        return ResponseHelper.success_response()

    async def configurations(self) -> Response[List[GlobalConfiguration]]:
        response = []
        app_cache_key = self.__config_repo.get_first_by({"key": ConfigKeysEnum.APP_CACHE_KEY})
        if app_cache_key:
            response.append(GlobalConfiguration(key="CATALOG.BUNDLES_CACHE_VERSION", value=app_cache_key.value))
        response.append(
            GlobalConfiguration(key="whatsapp_number".upper(), value=os.getenv("WHATSAPP_NUMBER", "961123123")))
        response.append(GlobalConfiguration(key="supabase_base_url".upper(), value=os.getenv("SUPABASE_URL")))
        response.append(
            GlobalConfiguration(key="supabase_base_anon_key".upper(), value=os.getenv("SUPABASE_ANON_KEY", "")))
        response.append(GlobalConfiguration(key="default_currency", value=os.getenv("DEFAULT_CURRENCY", "EUR")))
        response.append(GlobalConfiguration(key="allowed_payment_types", value=os.getenv("PAYMENT_METHODS",
                                                                                         f"{PaymentTypeEnum.CARD.value},{PaymentTypeEnum.WALLET.value},{PaymentTypeEnum.DCB.value}")))
        response.append(GlobalConfiguration(key="login_type", value=os.getenv("LOGIN_TYPE", "email")))
        return ResponseHelper.success_data_response(response, len(response))
