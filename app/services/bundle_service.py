import asyncio
from collections import defaultdict
from typing import List
from datetime import datetime

from coverage.html import os
from jinja2 import Environment, FileSystemLoader
from loguru import logger

from app.config.config import esim_hub_service_instance, send_email, generate_qr_code
from app.config.db import UserBundleType, OrderStatusEnum
from app.config.notification_types import send_buy_bundle_notification, send_buy_topup_notification
from app.config.push_notification_manager import fcm_service
from app.exceptions import BadRequestException
from app.models.user import UserOrderModel, UsersCopyModel, UserProfileModel
from app.repo import UserRepo, UserOrderRepo, UserProfileRepo, UserProfileBundleRepo
from app.repo.bundle_repo import BundleRepo
from app.repo.bundle_tage_repo import BundleTagRepo
from app.repo.tag_repo import TagRepo
from app.schemas.dto_mapper import DtoMapper
from app.schemas.home import BundleDTO, RegionDTO, CountryDTO
from app.schemas.response import Response, ResponseHelper
from app.services.currency_service import CurrencyService
from app.services.grouping_service import GroupingService


class BundleService:

    def __init__(self):
        self.__esim_hub_service = esim_hub_service_instance()
        self.__grouping_service = GroupingService()
        self.__bundle_repo = BundleRepo()
        self.__tag_repo = TagRepo()
        self.__bundle_tag_repo = BundleTagRepo()
        self.__currency_service = CurrencyService()
        self.__user_repo = UserRepo()
        self.__user_order_repo = UserOrderRepo()
        self.__user_profile_repo = UserProfileRepo()
        self.__user_profile_bundle_repo = UserProfileBundleRepo()
    async def get_bundle(self, bundle_id: str,currency_name : str) -> Response[BundleDTO]:
        # bundle = await self.__esim_hub_service.get_bundle_by_id(bundle_id)
        # todo check if currency needed to be checked
        bundle = self.__bundle_repo.get_bundle_by_id(bundle_id=bundle_id)
        rate = self.__currency_service.get_rate_by_currency(currency_name)

        return ResponseHelper.success_data_response(DtoMapper.bundle_currency_update(bundle,currency_name,rate), 1)

    async def get_regions(self) -> Response[List[RegionDTO]]:
        # regions = await self.__esim_hub_service.get_regions()
        regions = await self.__grouping_service.get_all_regions()
        return ResponseHelper.success_data_response(regions, len(regions))

    async def get_bundles_by_country(self, country_codes: str, currency_name: str) -> Response[List[BundleDTO]]:
        if country_codes is None or len(country_codes) == 0:
            raise BadRequestException("country_codes cannot be empty")

        first_tag = self.__tag_repo.get_by_id(country_codes.split(",")[0])
        country = CountryDTO.model_validate(first_tag.data)

        tags = self.__tag_repo.list_in(where={}, filter={"id": [item for item in country_codes.split(',')]})

        if not tags:
            raise BadRequestException("country_codes not found")

        # bundle_tags = self.__bundle_tag_repo.list_in(where={},filter = {"tag_id" : [item.id for item in tags] })

        results = self.__bundle_tag_repo.table \
            .select("bundle_id, tag_id") \
            .filter("tag_id", "in", f"({','.join([item.id for item in tags])})") \
            .execute()

        bundle_map = defaultdict(set)
        for row in results.data:
            bundle_map[row["bundle_id"]].add(row["tag_id"])

        target_tag_set = set([item.id for item in tags])
        matching_bundle_ids = [
            bundle_id for bundle_id, tags in bundle_map.items()
            if tags >= target_tag_set
        ]
        is_active = True
        bundles_model = self.__bundle_repo.list_in(where={"is_active":is_active}, filter={"id": matching_bundle_ids}, order_by="data->price")

        bundles: List[BundleDTO] = []

        rate = self.__currency_service.get_rate_by_currency(currency_name)

        for bundle in bundles_model:
            if bundle and bundle.data:
                bundle_dto = BundleDTO(**bundle.data)
                bundle_dto.icon = country.icon
                bundles.append(DtoMapper.bundle_currency_update(bundle_dto,currency_name,rate))

        filtered_bundles = self.__filter_by_gprs_limit(bundles)
        # bundles = await self.__esim_hub_service.get_bundles_by_country(country_codes.split(","))
        return ResponseHelper.success_data_response(filtered_bundles, len(filtered_bundles))

    async def get_bundles_by_region(self, region_code: str, currency: str) -> Response[List[BundleDTO]]:
        # regions = await self.__esim_hub_service.get_regions()
        regions = await self.__grouping_service.get_all_regions()

        searched_regions = [region for region in regions if region.region_code == region_code]

        if searched_regions is None or len(searched_regions) == 0:
            raise BadRequestException("Region Not Found")

        # todo is currency code needed
        # bundles = await self.__esim_hub_service.get_bundles_by_zone(zone=searched_regions[0].guid,
        #                                                             currency_code=currency)

        bundle_tags = self.__bundle_tag_repo.list(where={"tag_id": searched_regions[0].guid})

        is_active = True
        bundles_model = self.__bundle_repo.list_in(where={"is_active":is_active}, filter={"id": [item.bundle_id for item in bundle_tags]},
                                                   order_by="data->price")

        bundles: List[BundleDTO] = []

        rate = self.__currency_service.get_rate_by_currency(currency)

        for bundle in bundles_model:
            if bundle and bundle.data:
                bundle_dto = BundleDTO(**bundle.data)
                bundle_dto.icon = searched_regions[0].icon
                bundles.append(DtoMapper.bundle_currency_update(bundle_dto,currency,rate))

        filtered_bundles = []
        for bundle in bundles:
            if len(bundle.countries) > 1:
                filtered_bundles.append(bundle)

        filtered = self.__filter_by_gprs_limit(filtered_bundles)
        return ResponseHelper.success_data_response(filtered, len(filtered))

    async def get_countries(self):
        countries = await self.__grouping_service.get_all_countries()
        return ResponseHelper.success_data_response(countries, len(countries))


    async def buy_bundle(self, user_order: UserOrderModel, bundle: BundleDTO, user_id: str,
                                      payment_status: str):
        esim_hub_order = await self.__esim_hub_service.create_reseller_order(bundle_code=bundle.bundle_code,
                                                                             order_id=user_order.id)
        user_order.payment_status = payment_status
        user_order.payment_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        user_order.order_status = OrderStatusEnum.SUCCESS
        if esim_hub_order is None:
            user_order.order_status = OrderStatusEnum.FAILURE
            self.__user_order_repo.update_by({"id": user_order.id}, data=user_order.model_dump(exclude={"id"}))
            logger.info(f"error creating esim hub profile for order {user_order.id}")
            return BadRequestException("Payment failed")
        else:
            user_order.esim_order_id = esim_hub_order.orderId
        self.__user_order_repo.update_by({"id": user_order.id}, data=user_order.model_dump(exclude={"id"}))
        user_profile = self.__user_profile_repo.create({
            "user_id": user_id,
            "user_order_id": user_order.id,
            "shared_user_id": None,
            "iccid": esim_hub_order.iccid,
            "validity": esim_hub_order.validityData,
            "label": None,
            "smdp_address": esim_hub_order.smdpAdress,
            "activation_code": esim_hub_order.activationCode,
            "allow_topup": esim_hub_order.allowTopup,
            "esim_hub_order_id": esim_hub_order.orderId,
            "searched_countries": user_order.searched_countries,
        })
        self.__user_profile_bundle_repo.create({
            "user_id": user_order.user_id,
            "user_order_id": user_order.id,
            "user_profile_id": user_profile.id,
            "esim_hub_order_id": esim_hub_order.orderId,
            "iccid": esim_hub_order.iccid,
            "bundle_type": UserBundleType.PRIMARY_BUNDLE,
            "plan_started": False,
            "bundle_expired": False,
            "bundle_data": bundle.model_dump(),
        })
        await self.__send_buy_notification(bundle_name=bundle.bundle_name, iccid=esim_hub_order.iccid,
                                           user_id=user_order.user_id)
        user = self.__user_repo.get_by_id(record_id=user_order.user_id)
        asyncio.create_task(
            self.__send_email(
                user=user,
                user_profile=user_profile,
                bundle=bundle
            )
        )

        return ResponseHelper.success_response()


    async def top_up_bundle(self, bundle: BundleDTO, user_order: UserOrderModel, iccid: str, user_id: str,
                                     payment_status: str):
        user_profile = self.__user_profile_repo.get_first_by({"user_id": user_id, "iccid": iccid})
        try:
            esim_hub_topup = await self.__esim_hub_service.create_reseller_topup(
                esim_hub_order_id=user_profile.esim_hub_order_id,
                bundle_code=bundle.bundle_code,
                order_id=user_order.id)
        except Exception as e:
            esim_hub_topup = None
            logger.error(f"error while topping up bundle {str(e)}")
        if not esim_hub_topup:
            self.__user_order_repo.update_by({"id": user_order.id}, {
                "order_status": OrderStatusEnum.FAILURE,
                "payment_status": payment_status,
                "callback_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "esim_order_id": None
            })
            logger.error(f"error while topping up bundle {user_order.id}")
            return BadRequestException("Payment failed")
        self.__user_order_repo.update_by({"id": user_order.id}, {
            "order_status": OrderStatusEnum.SUCCESS,
            "payment_status": payment_status,
            "callback_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "esim_order_id": None
        })
        self.__user_profile_bundle_repo.create({
            "user_id": user_order.user_id,
            "user_order_id": user_order.id,
            "user_profile_id": user_profile.id,
            "esim_hub_order_id": esim_hub_topup.orderId,
            "iccid": iccid,
            "bundle_type": UserBundleType.TOP_UP_BUNDLE,
            "plan_started": False,
            "bundle_expired": False,
            "bundle_data": bundle.model_dump(),
        })
        await self.__send_topup_notification(bundle_name=bundle.bundle_name, iccid=iccid,
                                             user_id=user_order.user_id)
        return ResponseHelper.success_response()

    async def __send_email(self, user: UsersCopyModel, user_profile: UserProfileModel, bundle: BundleDTO):
        try:
            qr = generate_qr_code(f"LPA:1${user_profile.smdp_address}${user_profile.activation_code}")
            msisdn = os.getenv("WHATSAPP_NUMBER").replace("+", "").replace("-", "").replace(" ", "")
            data = {
                "bundle_name": bundle.bundle_name,
                "gprs_limit_display": bundle.gprs_limit_display,
                "price": bundle.price_display,
                "coverage": bundle.countries[0].country_code,
                "validity": bundle.validity_display,
                "iccid": user_profile.iccid,
                "smdp_address": user_profile.smdp_address,
                "activation_code": user_profile.activation_code,
                "msisdn": msisdn,
                "user": user.metadata.get("email", user.email)
            }

            env = Environment(loader=FileSystemLoader('app/email_templates'))
            template = env.get_template('send_qr_email_template.htm')
            html_content = template.render(data=data)
            send_email(subject="Activate Your Esim", html_content=html_content,
                       recipients=user.metadata.get("email", user.email), attachment=qr)
        except Exception as e:
            logger.error(f"error while sending email {str(e)}")

    async def __send_buy_notification(self, bundle_name, iccid, user_id):
        notification_message = send_buy_bundle_notification(bundle_name=bundle_name, iccid=iccid)
        fcm_service.send_notification_to_user_from_template(notification_message, user_id=user_id)

    async def __send_topup_notification(self, bundle_name, iccid, user_id):
        notification_message = send_buy_topup_notification(bundle_name=bundle_name, iccid=iccid)
        fcm_service.send_notification_to_user_from_template(notification_message, user_id=user_id)

    def __filter_by_gprs_limit(self, bundles: List[BundleDTO]) -> List[BundleDTO]:
        filtered_bundles_dict = {}

        for bundle in bundles:
            gprs_limit = bundle.gprs_limit_display
            price = bundle.price
            validity = bundle.validity
            key = f"{gprs_limit}_{validity}"

            if key not in filtered_bundles_dict or price < filtered_bundles_dict[key].price:
                filtered_bundles_dict[key] = bundle
        items = list(filtered_bundles_dict.values())
        sorted(items, key=lambda item: item.price, reverse=False)
        return items
