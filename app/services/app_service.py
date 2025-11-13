import os
from typing import List

import bleach
import httpx
from fastapi import Request
from loguru import logger

from app.config.config import esim_hub_service_instance, send_email
from app.config.constants import ErrorMessages
from app.config.db import ConfigKeysEnum
from app.config.helper import get_config
from app.exceptions import CustomException
from app.models.app import DeviceModel
from app.models.user import UserModel
from app.repo.config_repo import ConfigRepo, BannerRepo
from app.repo.contact_us_repo import ContactUsRepo
from app.repo.device_repo import DeviceRepo
from app.schemas.app import DeviceRequest, ContactUsRequest, DeleteDeviceRequest, GlobalConfiguration, BannerResponse
from app.schemas.app import FaqResponse, PageContentResponse
from app.schemas.dto_mapper import DtoMapper
from app.schemas.response import ResponseHelper, Response


class AppService:

    def __init__(self):
        self.__esim_hub_service = esim_hub_service_instance()
        self.__contact_us_repo = ContactUsRepo()
        self.__device_repo = DeviceRepo()
        self.__config_repo = ConfigRepo()
        self.__banner_repo = BannerRepo()

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
        old_device = self.__device_repo.get_first_by({"device_id": device_id})
        if old_device:
            location = old_device.ip_location
        else:
            if ip.index(".") > -1:
                ip = ip.split(",")[0].strip()
                try:
                    response = await self.__get_location(ip)
                    if response:
                        location = f"{response['city']}, {response['region']}, {response['country']}"
                    else:
                        location = "-"
                except Exception as e:
                    logger.error(f"Error fetching location for IP {ip}: {e}")
                    location = "-"
            else:
                location = "-"

        device_model = DeviceModel(
            **device_request.model_dump(),
            is_logged_in=True if user else False,
            originated_ip=ip,
            ip_location=location,
            device_id=device_id,
            user_id=user_id,
        )

        if user_id is None:
            update_response = self.__device_repo.update_by(where={"device_id": device_id},
                                                           data=device_model.model_dump(
                                                               exclude={"timestamp_login", "timestamp_logout"}))
            if update_response and len(update_response) > 0:
                return ResponseHelper.success_response()

        logger.info("No existing Device row found, performing upsert...")
        device_model.user_id = user_id
        upsert_response = self.__device_repo.upsert(
            data=device_model.model_dump(exclude={"timestamp_login", "timestamp_logout"}),
            on_conflict="device_id,user_id")

        logger.info("Upsert successful:", upsert_response)
        return ResponseHelper.success_response()

    async def delete_device(self, delete_device_request: DeleteDeviceRequest) -> Response:
        logger.info(f"deleting device {delete_device_request=}")
        return ResponseHelper.success_response()

    async def faq(self, accepted_language: str) -> Response[List[FaqResponse]]:
        try:
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
        except Exception as e:
            logger.error(f"Error fetching FAQ content: {e}")
            return ResponseHelper.success_data_response([], 0)

    async def about_us(self, accepted_language: str) -> Response[PageContentResponse]:
        try:
            response = await self.__esim_hub_service.get_content_tag("ABOUT_US", accepted_language)
            return ResponseHelper.success_data_response(DtoMapper.to_page_content_response(response), 1)
        except Exception as e:
            logger.error(f"Error fetching About Us content: {e}")
            return ResponseHelper.success_data_response(
                PageContentResponse(page_title="", page_content="", page_intro=""), 1)

    async def contact_us(self, contact_us_request: ContactUsRequest):
        response = self.__contact_us_repo.create({
            "email": contact_us_request.email,
            "content": bleach.clean(contact_us_request.content),
        })
        if not response:
            raise CustomException(code=400, details="Bad Request", name=ErrorMessages.MESSAGE_WAS_NOT_SUBMITTED)
        content = f"""
            <h1>Received New Email Message</h1>
            <p><b>From</b>: {contact_us_request.email}</p>
            <p><b>Content</b>: {contact_us_request.content}</p>
        """
        try:
            send_email(subject="New Email Received", html_content=content, recipients=get_config("SUPPORT_EMAIL"))
        except Exception as e:
            logger.error(f"Error sending email: {e}")
        return ResponseHelper.success_response()

    async def terms_and_conditions(self, accepted_language) -> Response[PageContentResponse]:
        try:
            response = await self.__esim_hub_service.get_content_tag("TERM_CONDITION", accepted_language)
            return ResponseHelper.success_data_response(DtoMapper.to_page_content_response(response), 1)
        except Exception as e:
            logger.error(f"Error fetching Terms and Conditions content: {e}")
            return ResponseHelper.success_data_response(
                PageContentResponse(page_title="", page_content="", page_intro=""), 1)

    async def privacy_policy(self, accepted_language: str) -> Response[PageContentResponse]:
        try:
            response = await self.__esim_hub_service.get_content_tag("PRIVACY_POLICY", accepted_language)
            return ResponseHelper.success_data_response(DtoMapper.to_page_content_response(response), 1)
        except Exception as e:
            logger.error(f"Error fetching Privacy Policy content: {e}")
            return ResponseHelper.success_data_response(
                PageContentResponse(page_title="", page_content="", page_intro=""), 1)

    async def user_guide(self):
        return ResponseHelper.success_response()

    async def configurations(self) -> Response[List[GlobalConfiguration]]:
        response = []
        configs = self.__config_repo.list(where={})
        for config in configs:
            response.append(GlobalConfiguration(key=config.key.upper(), value=config.value))
        app_cache_key = self.__config_repo.get_first_by({"key": ConfigKeysEnum.APP_CACHE_KEY})
        if app_cache_key:
            response.append(GlobalConfiguration(key="CATALOG.BUNDLES_CACHE_VERSION", value=app_cache_key.value))
        response.append(
            GlobalConfiguration(key="whatsapp_number".upper(), value=os.getenv("WHATSAPP_NUMBER", "961123123")))
        response.append(GlobalConfiguration(key="supabase_base_url".upper(), value=os.getenv("SUPABASE_URL")))
        response.append(
            GlobalConfiguration(key="supabase_base_anon_key".upper(), value=os.getenv("SUPABASE_ANON_KEY", "")))
        response.append(GlobalConfiguration(key="default_currency", value=os.getenv("DEFAULT_CURRENCY", "EUR")))
        return ResponseHelper.success_data_response(response, len(response))

    async def __get_location(self, ip: str):
        url = f"https://ipapi.co/{ip}/json/"
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
        if response.status_code == 200:
            data = response.json()
            return {
                "ip": data.get("ip"),
                "city": data.get("city"),
                "region": data.get("region"),
                "country": data.get("country_name"),
                "latitude": data.get("latitude"),
                "longitude": data.get("longitude")
            }
        else:
            logger.error(f"Failed to fetch location for IP {ip}: {response.status_code} {response.text}")
        return None

    def banners(self, locale: str = "en", x_platform: str = "web") -> Response[List[BannerResponse]]:
        banners = self.__banner_repo.list(where={"platform": x_platform})
        logger.info(f"banners {banners=} {locale=}")
        response = [BannerResponse(**banner.model_dump()) for banner in banners]
        return ResponseHelper.success_data_response(response, len(banners))
