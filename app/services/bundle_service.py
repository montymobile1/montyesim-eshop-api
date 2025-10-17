import os
import threading
from collections import defaultdict
from datetime import datetime
from typing import List, Literal

from loguru import logger
from soupsieve.util import lower

from app.config.config import esim_hub_service_instance, send_email, generate_qr_code, get_email_template
from app.config.db import UserBundleType, OrderStatusEnum, PaymentTypeEnum, PromotionRuleAction, ConfigKeysEnum
from app.config.notification_types import send_buy_bundle_notification, send_buy_topup_notification
from app.config.push_notification_manager import fcm_service
from app.config.utils import get_config
from app.exceptions import BadRequestException
from app.models.app import BundleModel
from app.models.user import UserOrderModel, UsersCopyModel, UserProfileModel, UserProfileBundleModel
from app.repo import UserRepo, UserOrderRepo, UserProfileRepo, UserProfileBundleRepo
from app.repo.bundle_repo import BundleRepo
from app.repo.bundle_tage_repo import BundleTagRepo
from app.repo.tag_repo import TagRepo
from app.schemas.bundle import RelatedSearchRequestDto
from app.schemas.dto_mapper import DtoMapper
from app.schemas.home import BundleDTO, RegionDTO, CountryDTO
from app.schemas.response import Response, ResponseHelper
from app.services.currency_service import CurrencyService
from app.services.grouping_service import GroupingService
from app.services.promotion_service import PromotionService


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
        self.__promotion_service = PromotionService()

    def bundle_exists(self, bundle_id: str) -> bool:
        try:
            bundle = self.__bundle_repo.get_by_id(record_id=bundle_id)
            return bundle is not None
        except Exception as e:
            logger.error(f"error while getting bundle {e}")
            return False

    def get_bundle_by_id(self, bundle_id: str) -> BundleModel | None:
        try:
            bundle = self.__bundle_repo.get_by_id(record_id=bundle_id)
            return bundle
        except Exception as e:
            logger.error(f"error while getting bundle {e}")
            return None

    def get_bundle(self, bundle_id: str, currency_name: str, locale: str = "en") -> Response[BundleDTO]:
        bundle = self.__bundle_repo.get_bundle_by_id(bundle_id=bundle_id)
        rate = self.__currency_service.get_rate_by_currency(currency_name)

        tags_id = [bundle_country.id for bundle_country in bundle.countries]
        if locale != os.getenv("DEFAULT_LOCALE", "en"):
            country_tags = self.__tag_repo.select_procedure(function_name="get_translated_tag_by_tag_id_list",
                                                            where={"tag_ids": tags_id,
                                                                   "locale_param": locale})
            for country_tag in country_tags:
                country_tag.data["country"] = country_tag.name
            countries = [CountryDTO(**tag.data) for tag in country_tags]
            bundle.countries = countries

        return ResponseHelper.success_data_response(DtoMapper.bundle_currency_update(bundle, currency_name, rate), 1)

    async def get_regions(self, locale: str) -> Response[List[RegionDTO]]:
        regions = await self.__grouping_service.get_all_regions(locale=locale)
        return ResponseHelper.success_data_response(regions, len(regions))

    def get_bundles_by_country(self, country_codes: str, currency_name: str, locale: str) -> Response[
        List[BundleDTO]]:
        if country_codes is None or len(country_codes) == 0:
            raise BadRequestException("country_codes cannot be empty")

        first_tag = self.__tag_repo.get_by_id(country_codes.split(",")[0])
        country = CountryDTO.model_validate(first_tag.data)

        tags = self.__tag_repo.list_in(where={}, filter={"id": list(country_codes.split(','))})

        if not tags:
            raise BadRequestException("country_codes not found")

        results = self.__bundle_tag_repo.table \
            .select("bundle_id, tag_id") \
            .filter("tag_id", "in", f"({','.join([item.id for item in tags])})") \
            .execute()

        bundle_map = defaultdict(set)
        for row in results.data:
            bundle_map[row["bundle_id"]].add(row["tag_id"])

        target_tag_set = {item.id for item in tags}
        matching_bundle_ids = [
            bundle_id for bundle_id, tags in bundle_map.items()
            if tags >= target_tag_set
        ]
        is_active = True
        bundles_model = self.__bundle_repo.list_in(where={"is_active": is_active}, filter={"id": matching_bundle_ids},
                                                   order_by="data->price")

        bundles: List[BundleDTO] = []

        rate = self.__currency_service.get_rate_by_currency(currency_name)

        for bundle in bundles_model:
            if bundle and bundle.data:
                bundle_dto = BundleDTO(**bundle.data)
                bundle_dto.icon = country.icon
                tags_id = [bundle_country.id for bundle_country in bundle_dto.countries]
                if locale != os.getenv("DEFAULT_LOCALE", "en"):
                    country_tags = self.__tag_repo.select_procedure(function_name="get_translated_tag_by_tag_id_list",
                                                                    where={"tag_ids": tags_id,
                                                                           "locale_param": locale})
                    for country_tag in country_tags:
                        country_tag.data["country"] = country_tag.name
                    countries = [CountryDTO(**tag.data) for tag in country_tags]
                    bundle_dto.countries = countries

                bundles.append(DtoMapper.bundle_currency_update(bundle_dto, currency_name, rate))

        filtered_bundles = self.__filter_by_gprs_limit(bundles)
        return ResponseHelper.success_data_response(filtered_bundles, len(filtered_bundles))

    async def get_bundles_by_region(self, region_code: str, currency: str, locale: str) -> Response[List[BundleDTO]]:
        start_time = datetime.now()
        regions = await self.__grouping_service.get_all_regions(locale)

        searched_regions = [region for region in regions if region.region_code == region_code]

        if len(searched_regions) == 0:
            raise BadRequestException("Region Not Found")

        bundle_tags = self.__bundle_tag_repo.list(where={"tag_id": searched_regions[0].guid})

        is_active = True
        bundles_model = self.__bundle_repo.list_in(where={"is_active": is_active},
                                                   filter={"id": [item.bundle_id for item in bundle_tags]},
                                                   order_by="data->price")

        bundles: List[BundleDTO] = []

        rate = self.__currency_service.get_rate_by_currency(currency)

        for bundle in bundles_model:
            if bundle and bundle.data:
                bundle_dto = BundleDTO(**bundle.data)
                bundle_dto.icon = searched_regions[0].icon
                if locale != os.getenv("DEFAULT_LOCALE", "en"):
                    tags_id = [bundle_country.id for bundle_country in bundle_dto.countries]
                    country_tags = self.__tag_repo.select_procedure(function_name="get_translated_tag_by_tag_id_list",
                                                                    where={"tag_ids": tags_id,
                                                                           "locale_param": locale})
                    for country_tag in country_tags:
                        country_tag.data["country"] = country_tag.name
                    countries = [tag.data for tag in country_tags]
                    bundle_dto.countries = countries

                bundles.append(DtoMapper.bundle_currency_update(bundle_dto, currency, rate))

        filtered_bundles = []
        for bundle in bundles:
            if len(bundle.countries) > 1:
                filtered_bundles.append(bundle)

        filtered = self.__filter_by_gprs_limit(filtered_bundles)
        duration = (datetime.now() - start_time).total_seconds()
        logger.info(f"get_bundles_by_region executed in {duration} seconds")
        return ResponseHelper.success_data_response(filtered, len(filtered))

    async def get_countries(self, locale: str):
        countries = await self.__grouping_service.get_all_countries(locale)
        return ResponseHelper.success_data_response(countries, len(countries))

    async def buy_bundle(self, user_order: UserOrderModel, bundle: BundleDTO, user_id: str,
                         payment_status: str, rule_id: str = None, payment_type: str = PaymentTypeEnum.CARD):
        rate = self.__currency_service.get_currency_rate(from_currency=user_order.currency, to_currency="USD")
        user = self.__user_repo.get_by_id(record_id=user_id)
        msisdn = user.metadata.get("msisdn", "")
        email = user.email
        unique_identifier = f"{msisdn if msisdn else email}|{user_order.id}"
        order_id = user_order.id
        promo_code = user_order.promo_code if user_order.promo_code else user_order.referral_code
        new_price = (round((user_order.modified_amount / 100) * rate, 2)) if promo_code else None
        discount_amount = self.__get_discount_amount(promo_code) if promo_code else None
        discount_rate = self.__get_discount_rate(promo_code) if promo_code else None
        bundle_type = self.__bundle_type(code=bundle.bundle_code)
        esim_hub_order = await self.__esim_hub_service.create_reseller_order(bundle_code=bundle.bundle_code,
                                                                             order_id=unique_identifier, user=user,
                                                                             payment_type=payment_type,
                                                                             new_price=new_price,
                                                                             discount_amount=discount_amount,
                                                                             discount_rate=discount_rate,
                                                                             bundle_type=bundle_type)

        user_order.payment_status = payment_status
        user_order.payment_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        user_order.order_status = OrderStatusEnum.SUCCESS
        if esim_hub_order is None:
            user_order.order_status = OrderStatusEnum.FAILURE
            self.__user_order_repo.update_by({"id": user_order.id}, data=user_order.model_dump(exclude={"id"}))
            logger.info(f"error creating esim hub profile for order {user_order.id}")
            await self.__promotion_service.update_promotion_usage(user_id=user_id, code=promo_code, status="failed",
                                                                  rule_id=rule_id, order_id=order_id)
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
        # await self.__promotion_service.check_referral_rewards_after_buy_bundle(user_id)
        if user_order.promo_code or user_order.referral_code:
            await self.__promotion_service.apply_promotion_code_after_purchase(user_id=user_id,
                                                                               code=user_order.promo_code or user_order.referral_code,
                                                                               status="completed",
                                                                               order_id=user_order.id,
                                                                               rule_id=rule_id)

        await self.__send_buy_notification(bundle_name=bundle.bundle_name, iccid=esim_hub_order.iccid,
                                           user_id=user_order.user_id)
        user = self.__user_repo.get_by_id(record_id=user_order.user_id)
        thread = threading.Thread(target=self.__send_email, args=(user, user_profile, bundle, user_order))
        thread.start()

        return ResponseHelper.success_response()

    async def top_up_bundle(self, bundle: BundleDTO, user_order: UserOrderModel, iccid: str, user_id: str,
                            payment_status: str, payment_type: str = PaymentTypeEnum.CARD):
        user = self.__user_repo.get_by_id(record_id=user_id)
        msisdn = user.metadata.get("msisdn", "")
        email = user.email
        order_id = f"{msisdn if msisdn else email}|{user_order.id}"
        user_profile: UserProfileModel = self.__user_profile_repo.get_first_by({"user_id": user_id, "iccid": iccid})
        primary_bundle: UserProfileBundleModel = self.__user_profile_bundle_repo.get_first_by(
            {"user_id": user_id, "iccid": iccid, "bundle_type": UserBundleType.PRIMARY_BUNDLE})
        bundle_type = self.__bundle_type(code=bundle.bundle_code)
        try:
            esim_hub_topup = await self.__esim_hub_service.create_reseller_topup(
                esim_hub_order_id=user_profile.esim_hub_order_id,
                bundle_code=bundle.bundle_code,
                order_id=order_id,
                user=user,
                payment_type=payment_type,
                bundle_type=bundle_type)
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
            "plan_started": True if primary_bundle.bundle_expired is False else False,
            "bundle_expired": False,
            "bundle_data": bundle.model_dump(),
        })
        await self.__send_topup_notification(bundle_name=bundle.bundle_name, iccid=iccid,
                                             user_id=user_order.user_id)
        return ResponseHelper.success_response()

    def __send_email(self, user: UsersCopyModel, user_profile: UserProfileModel, bundle: BundleDTO,
                     user_order: UserOrderModel):
        try:
            qr = generate_qr_code(f"LPA:1${user_profile.smdp_address}${user_profile.activation_code}")
            msisdn = os.getenv("WHATSAPP_NUMBER")
            if msisdn:
                msisdn = msisdn.replace("+", "").replace("-", "").replace(" ", "")
            coverage = self.__get_coverage(user_profile=user_profile, bundle=bundle)
            display_email = user.metadata.get("display_email", None)
            email = user.metadata.get("email", user.email) if display_email is None else display_email
            data = {
                "bundle_name": bundle.bundle_name,
                "gprs_limit_display": bundle.gprs_limit_display,
                "price": f"{round(user_order.modified_amount / 100, 2)} {user_order.currency.upper()}",
                "coverage": coverage,
                "validity": bundle.validity_display,
                "iccid": user_profile.iccid,
                "smdp_address": user_profile.smdp_address,
                "activation_code": user_profile.activation_code,
                "msisdn": msisdn,
                "user": email
            }
            language = lower(user.metadata.get("language", "en"))
            template = get_email_template(f"send_qr_email_template_{language}.htm")
            html_content = template.render(data=data)
            send_email(subject="Activate Your Esim", html_content=html_content,
                       recipients=user.metadata.get("email", email), attachment=qr)
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
        items = filtered_bundles_dict.values()
        sorted_bundles = sorted(items, key=lambda item: item.price, reverse=False)
        return sorted_bundles

    def __get_coverage(self, user_profile: UserProfileModel, bundle: BundleDTO):
        try:
            searched_countries = RelatedSearchRequestDto.model_validate_json(user_profile.searched_countries)
        except Exception as e:
            logger.error(f"error while validating searched_countries {str(e)}")
            searched_countries = None
        bundle_countries = bundle.countries
        more_countries = f"+{len(bundle_countries)} more countries " if len(bundle_countries) > 1 else ""
        if len(bundle_countries) > 1:
            coverage = f"{bundle_countries[0].country_code} {more_countries}"
        else:
            coverage = bundle_countries[0].country_code if bundle_countries else "No coverage"

        if searched_countries:
            if searched_countries.countries:
                coverage = f"{searched_countries.countries[0].country_name} {more_countries}"
            if searched_countries.region:
                coverage = searched_countries.region.region_name
        return coverage

    def __get_discount_amount(self, promo_code: str):
        try:
            if self.__promotion_service.is_referral_code(promo_code):
                rule = self.__promotion_service.get_referral_rule()
                if rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_AMOUNT:
                    return float(get_config(ConfigKeysEnum.REFERRAL_CODE_AMOUNT))
            promotion = self.__promotion_service.get_promotion_by_code(promo_code)
            if promotion:
                rule = self.__promotion_service.get_rule_by_id(promotion.rule_id)
                if rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_AMOUNT:
                    return promotion.amount
            return 0
        except Exception as e:
            logger.error(f"error while getting discount amount {str(e)}")
            return 0

    def __get_discount_rate(self, promo_code: str):
        try:
            if self.__promotion_service.is_referral_code(promo_code):
                rule = self.__promotion_service.get_referral_rule()
                if rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_PERCENTAGE:
                    return float(get_config(ConfigKeysEnum.REFERRAL_CODE_PERCENTAGE))
            promotion = self.__promotion_service.get_promotion_by_code(promo_code)
            if promotion:
                rule = self.__promotion_service.get_rule_by_id(promotion.rule_id)
                if rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_PERCENTAGE:
                    return promotion.amount
            return 0
        except Exception as e:
            logger.error(f"error while getting discount rate {str(e)}")
            return 0

    def __bundle_type(self, code) -> Literal["COUNTRY", "CRUISE"]:
        bundle_type = "COUNTRY"
        bundle_tags = self.__bundle_tag_repo.list(where={"bundle_id": code})
        for bundle_tag in bundle_tags:
            tag = self.__tag_repo.get_first_by(where={"id": bundle_tag.tag_id})
            if tag.tag_group_id == 3:
                bundle_type = "CRUISE"
                break
        if bundle_type not in ("COUNTRY", "CRUISE"):
            raise ValueError(f"Invalid bundle_type: {bundle_type}")
        return bundle_type
