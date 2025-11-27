import asyncio
import os
from datetime import datetime, timezone as dt_timezone
from typing import List, Literal

from loguru import logger
from soupsieve.util import lower

from app.config.config import esim_hub_service_instance, send_email, generate_qr_code, get_email_template
from app.config.constants import ErrorMessages
from app.config.db import UserBundleType, OrderStatusEnum, PaymentTypeEnum, PromotionRuleAction, ConfigKeysEnum
from app.config.helper import get_config
from app.config.notification_types import send_buy_bundle_notification, send_buy_topup_notification
from app.config.push_notification_manager import fcm_service
from app.config.utils import truncate_two_decimals_decimal
from app.exceptions import BadRequestException, CustomException
from app.models import UserOrderModel, UserProfileModel, UsersCopyModel
from app.models.bundle import BundleModel
from app.repo import UserRepo, UserOrderRepo, UserProfileRepo, UserProfileBundleRepo
from app.repo.bundle_repo import BundleRepo
from app.schemas.bundle import RelatedSearchRequestDto
from app.schemas.dto_mapper import DtoMapper
from app.schemas.home import BundleDTO, RegionDTO
from app.schemas.response import Response, ResponseHelper
from app.services.cache_service import CacheService
from app.services.currency_service import CurrencyService
from app.services.grouping_service import GroupingService
from app.services.promotion_service import PromotionService
from app.services.task_executor import TaskExecutor


class BundleService:

    def __init__(self):
        self.__esim_hub_service = esim_hub_service_instance()
        self.__grouping_service = GroupingService()
        self.__bundle_repo = BundleRepo()
        self.__currency_service = CurrencyService()
        self.__user_repo = UserRepo()
        self.__user_order_repo = UserOrderRepo()
        self.__user_profile_repo = UserProfileRepo()
        self.__user_profile_bundle_repo = UserProfileBundleRepo()
        self.__promotion_service = PromotionService()
        self.__task_executor = TaskExecutor()

    async def bundle_exists(self, bundle_id: str) -> bool:
        try:
            bundle = await self.__bundle_repo.get_by_id(record_id=bundle_id)
            return bundle is not None
        except Exception as e:
            logger.error(f"error while getting bundle {e}")
            return False

    async def get_bundle_by_id(self, bundle_id: str) -> BundleModel | None:
        try:
            bundle = await self.__bundle_repo.get_by_id(record_id=bundle_id)
            return bundle
        except Exception as e:
            logger.error(f"error while getting bundle {e}")
            return None

    async def get_bundle(self, bundle_id: str, currency_name: str, locale: str = "en") -> Response[BundleDTO]:
        rate = await self.__currency_service.aget_rate_by_currency(currency_name)
        bundles = await self.__bundle_repo.get_bundles_by_tag_group(bundle_code=bundle_id)
        if len(bundles) == 0:
            raise CustomException(code=400, name=ErrorMessages.BUNDLE_NOT_AVAILABLE,
                                  details=ErrorMessages.BUNDLE_NOT_AVAILABLE)
        bundle = bundles[0]
        return ResponseHelper.success_data_response(DtoMapper.bundle_currency_update(bundle, currency_name, rate), 1)

    async def get_regions(self, locale: str) -> Response[List[RegionDTO]]:
        regions = await self.__grouping_service.get_all_regions(locale=locale)
        return ResponseHelper.success_data_response(regions, len(regions))

    async def get_bundles_by_country(self, country_codes: str, currency_name: str, locale: str) -> Response[
        List[BundleDTO]]:
        cache_config_key = get_config("CACHE_KEY", "default")
        cache_key = f"by_country:{country_codes}:{cache_config_key}:{currency_name}:{locale}"

        cached: List[BundleDTO] | None = await CacheService.read_list_from_cache(cache_key, BundleDTO)
        if cached is not None:
            logger.info(f"getting bundles from cache {cache_key}")
            return ResponseHelper.success_data_response(cached, len(cached))

        if country_codes is None or len(country_codes) == 0:
            raise BadRequestException("country_codes cannot be empty")
        original_bundles = await self.__bundle_repo.get_bundles_by_tag_group(tag_ids=country_codes.split(","),
                                                                             locale=locale)
        bundles: List[BundleDTO] = []
        rate = await self.__currency_service.aget_rate_by_currency(currency_name)
        for b in original_bundles:
            bundles.append(DtoMapper.bundle_currency_update(b, currency_name, rate))
        await CacheService.add_to_cache(cache_key, bundles, ttl=int(get_config("CACHE_TIME", 600)))
        return ResponseHelper.success_data_response(bundles, len(bundles))

    async def get_bundles_by_region(self, region_code: str, currency: str, locale: str) -> Response[List[BundleDTO]]:
        start_time = datetime.now()
        cache_config_key = get_config("CACHE_KEY", "default")
        cache_key = f"by_region:{region_code}:{cache_config_key}:{currency}:{locale}"

        cached: List[BundleDTO] | None = await CacheService.read_list_from_cache(cache_key, BundleDTO)
        if cached is not None:
            logger.info(f"getting bundles from cache {cache_key}")
            return ResponseHelper.success_data_response(cached, len(cached))

        regions = await self.__grouping_service.get_all_regions(locale)

        searched_regions = [region for region in regions if region.region_code == region_code]

        if len(searched_regions) == 0:
            raise BadRequestException("Region Not Found")

        original_bundles = await self.__bundle_repo.get_bundles_by_tag_group(tag_ids=[searched_regions[0].guid],
                                                                             locale=locale)
        bundles: List[BundleDTO] = []
        rate = await self.__currency_service.aget_rate_by_currency(currency)
        for b in original_bundles:
            if len(b.countries) > 1:
                bundles.append(DtoMapper.bundle_currency_update(b, currency, rate))

        filtered = self.__filter_by_gprs_limit(bundles)
        filtered = self.__filter_forbidden_countries(filtered)
        duration = (datetime.now() - start_time).total_seconds()
        logger.info(f"get_bundles_by_region executed in {duration} seconds")
        await CacheService.add_to_cache(cache_key, filtered, int(get_config("CACHE_TIME", 600)))
        return ResponseHelper.success_data_response(filtered, len(filtered))

    async def get_countries(self, locale: str):
        countries = await self.__grouping_service.get_all_countries(locale)
        return ResponseHelper.success_data_response(countries, len(countries))

    async def buy_bundle(self, user_order: UserOrderModel, bundle: BundleDTO, user_id: str,
                         payment_status: str, payment_type: str, rule_id: str = None):
        rate = await self.__currency_service.aget_currency_rate(from_currency=user_order.currency, to_currency="USD")
        user = await self.__user_repo.get_by_id(record_id=user_id)
        msisdn = user.metadata_json.get("msisdn", "")
        email = user.email
        unique_identifier = f"{msisdn if msisdn else email}|{user_order.id}"
        order_id = user_order.id
        promo_code = user_order.promo_code if user_order.promo_code else user_order.referral_code
        new_price = (round((user_order.modified_amount / 100) * rate, 2)) if promo_code else None
        discount_amount = await self.__get_discount_amount(promo_code) if promo_code else None
        discount_rate = await self.__get_discount_rate(promo_code) if promo_code else None
        bundle_type = await self.__bundle_type(code=bundle.bundle_code)
        esim_hub_order = await self.__esim_hub_service.create_reseller_order(bundle_code=bundle.bundle_code,
                                                                             order_id=unique_identifier, user=user,
                                                                             payment_type=payment_type,
                                                                             new_price=new_price,
                                                                             discount_amount=discount_amount,
                                                                             discount_rate=discount_rate,
                                                                             bundle_type=bundle_type)

        user_order.payment_status = payment_status
        user_order.payment_time = datetime.now(tz=dt_timezone.utc).replace(tzinfo=None)
        user_order.order_status = OrderStatusEnum.SUCCESS
        if esim_hub_order is None:
            user_order.order_status = OrderStatusEnum.FAILURE
            await self.__user_order_repo.update_by({"id": user_order.id}, data=user_order)
            logger.info(f"error creating esim hub profile for order {user_order.id}")
            await self.__promotion_service.update_promotion_usage(user_id=user_id, code=promo_code, status="failed",
                                                                  rule_id=rule_id, order_id=order_id)
            return BadRequestException("Payment failed")
        else:
            user_order.esim_order_id = esim_hub_order.orderId
        await self.__user_order_repo.update_by({"id": user_order.id}, data=user_order)
        user_profile = await self.__user_profile_repo.create({
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
        await self.__user_profile_bundle_repo.create({
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
                                                                               status="completed",
                                                                               user_order=user_order,
                                                                               rule_id=rule_id)

        await self.__send_buy_notification(bundle_name=bundle.bundle_name, iccid=esim_hub_order.iccid,
                                           user_id=user_order.user_id)
        user = await self.__user_repo.get_by_id(record_id=user_order.user_id)

        def task():
            asyncio.run(self.__send_email(user=user, user_profile=user_profile, bundle=bundle, user_order=user_order))

        self.__task_executor.add_task(task)
        return ResponseHelper.success_response()

    async def top_up_bundle(self, bundle: BundleDTO, user_order: UserOrderModel, iccid: str, user_id: str,
                            payment_status: str, payment_type: str = PaymentTypeEnum.CARD):
        user = await self.__user_repo.get_by_id(record_id=user_id)
        msisdn = user.metadata_json.get("msisdn", "")
        email = user.email
        order_id = f"{msisdn if msisdn else email}|{user_order.id}"
        user_profile = await self.__user_profile_repo.get_first_by({"user_id": user_id, "iccid": iccid})
        primary_bundle = await self.__user_profile_bundle_repo.get_first_by(
            {"user_id": user_id, "iccid": iccid, "bundle_type": UserBundleType.PRIMARY_BUNDLE})
        bundle_type = await self.__bundle_type(code=bundle.bundle_code)
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
            await self.__user_order_repo.update_by({"id": user_order.id}, {
                "order_status": OrderStatusEnum.FAILURE,
                "payment_status": payment_status,
                "callback_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "esim_order_id": None
            })
            logger.error(f"error while topping up bundle {user_order.id}")
            return BadRequestException("Payment failed")
        await self.__user_order_repo.update_by({"id": user_order.id}, {
            "order_status": OrderStatusEnum.SUCCESS,
            "payment_status": payment_status,
            "callback_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "esim_order_id": None
        })
        await self.__user_profile_bundle_repo.create({
            "user_id": user_order.user_id,
            "user_order_id": user_order.id,
            "user_profile_id": user_profile.id,
            "esim_hub_order_id": esim_hub_topup.orderId,
            "iccid": iccid,
            "bundle_type": UserBundleType.TOP_UP_BUNDLE,
            "plan_started": True if primary_bundle.bundle_expired is True else False,
            "bundle_expired": False,
            "bundle_data": bundle.model_dump(),
        })
        await self.__send_topup_notification(bundle_name=bundle.bundle_name, iccid=iccid,
                                             user_id=user_order.user_id)
        return ResponseHelper.success_response()

    async def __send_email(self, user: UsersCopyModel, user_profile: UserProfileModel, bundle: BundleDTO,
                           user_order: UserOrderModel):
        try:
            qr = generate_qr_code(f"LPA:1${user_profile.smdp_address}${user_profile.activation_code}")
            msisdn = get_config("WHATSAPP_NUMBER", "")
            if msisdn and msisdn != "":
                msisdn = msisdn.replace("+", "").replace("-", "").replace(" ", "")
            currency = user.metadata_json.get("currency", os.getenv("DEFAULT_CURRENCY", "USD"))
            rate = self.__currency_service.get_currency_rate(from_currency="USD", to_currency=currency)
            coverage = await self.__get_coverage(user_profile=user_profile, bundle=bundle)
            display_email = user.metadata_json.get("display_email", None)
            email = user.metadata_json.get("email", user.email) if display_email is None else display_email
            amount = float(user_order.modified_amount) + float(user_order.tax_amount)
            amount = truncate_two_decimals_decimal((amount / 100) * rate)
            data = {
                "bundle_name": bundle.bundle_name,
                "gprs_limit_display": bundle.gprs_limit_display,
                "price": f"{amount} {currency.upper()}",
                "coverage": coverage,
                "validity": bundle.validity_display,
                "iccid": user_profile.iccid,
                "smdp_address": user_profile.smdp_address,
                "activation_code": user_profile.activation_code,
                "msisdn": msisdn,
                "user": email,
                "base_url": get_config("BASE_URL", "https://sales-esim-shop-portal.onrender.com")
            }
            language = lower(user.metadata_json.get("language", "en"))
            try:
                template = get_email_template(f"send_qr_email_template_{language}.htm")
            except Exception as e:
                logger.error(f"error while getting email template for language {language}, error: {str(e)}")
                template = get_email_template("send_qr_email_template_en.htm")
            html_content = template.render(data=data)
            send_email(subject="Activate Your Esim", html_content=html_content,
                       recipients=user.metadata_json.get("email", email), attachment=qr)
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

    def __filter_forbidden_countries(self, bundles: List[BundleDTO]) -> List[BundleDTO]:
        forbidden_countries = get_config("FORBIDDEN_COUNTRIES", "").split(",")
        filtered_bundles = []
        for bundle in bundles:
            allowed_countries = [country for country in bundle.countries if
                                 country.country_code not in forbidden_countries]
            if allowed_countries:
                bundle.countries = allowed_countries
                filtered_bundles.append(bundle)
        return filtered_bundles

    async def __get_coverage(self, user_profile: UserProfileModel, bundle: BundleDTO):

        bundle_type = await self.__bundle_type(code=bundle.bundle_code)
        if bundle_type == "CRUISE":
            return "Cruise"
        searched_countries = None
        try:
            if user_profile.searched_countries is not None:
                searched_countries = RelatedSearchRequestDto.model_validate_json(user_profile.searched_countries)
        except Exception as e:
            logger.error(f"error while validating searched_countries {str(e)}")

        bundle_countries = bundle.countries
        more_countries = f"+{len(bundle_countries)} more countries " if len(bundle_countries) > 1 else ""
        if len(bundle_countries) > 1:
            coverage = f"{bundle_countries[0].country_code} {more_countries}"
        else:
            coverage = bundle_countries[0].country_code if bundle_countries else "No coverage"

        if searched_countries:
            if searched_countries.countries and len(searched_countries.countries) > 0:
                coverage = f"{searched_countries.countries[0].country_name} {more_countries}"
            if searched_countries.region:
                coverage = searched_countries.region.region_name
        return coverage

    async def __get_discount_amount(self, promo_code: str):
        try:
            if await self.__promotion_service.is_referral_code(promo_code):
                rule = await self.__promotion_service.get_referral_rule()
                if rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_AMOUNT:
                    return float(get_config(ConfigKeysEnum.REFERRAL_CODE_AMOUNT))
            promotion = await self.__promotion_service.get_promotion_by_code(promo_code)
            if promotion:
                rule = await self.__promotion_service.get_rule_by_id(promotion.rule_id)
                if rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_AMOUNT:
                    return promotion.amount
            return 0
        except Exception as e:
            logger.error(f"error while getting discount amount {str(e)}")
            return 0

    async def __get_discount_rate(self, promo_code: str):
        try:
            if await self.__promotion_service.is_referral_code(promo_code):
                rule = await self.__promotion_service.get_referral_rule()
                if rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_PERCENTAGE:
                    return float(get_config(ConfigKeysEnum.REFERRAL_CODE_PERCENTAGE))
            promotion = await self.__promotion_service.get_promotion_by_code(promo_code)
            if promotion:
                rule = await self.__promotion_service.get_rule_by_id(promotion.rule_id)
                if rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_PERCENTAGE:
                    return promotion.amount
            return 0
        except Exception as e:
            logger.error(f"error while getting discount rate {str(e)}")
            return 0

    async def __bundle_type(self, code) -> Literal["COUNTRY", "CRUISE"]:
        is_cruise = await self.__bundle_repo.is_cruise_bundle(code)
        return "CRUISE" if is_cruise else "COUNTRY"
