import json
import os
from datetime import datetime
from math import ceil
from typing import List

from gotrue import AuthResponse
from loguru import logger

from app.config.db import PaymentTypeEnum
from app.models.app import CurrencyModel
from app.models.notification import NotificationModel
from app.models.promotion import PromotionUsageModel
from app.models.user import UserProfileModel, UserProfileBundleModel, UserProfileBundleWithProfileModel, \
    CallBackNotificationInfoModel, UserOrderModel, UserWalletModel
from app.schemas.app import UserNotificationResponse, PageContentResponse, ExchangeRate
from app.schemas.auth import AuthResponseDTO, UserInfo
from app.schemas.bundle import EsimBundleResponse, ConsumptionResponse, TransactionHistoryResponse, \
    UserOrderHistoryResponse, RelatedSearchRequestDto, CountryRequestDto
from app.schemas.esim_hub import ContentResponse
from app.schemas.home import BundleDTO, BundleCategoryDTO, CountryDTO, RegionDTO, CurrencyDto
from app.schemas.promotion import PromotionHistoryDto
from app.schemas.user_wallet import UserWalletResponse

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")


class DtoMapper:

    @staticmethod
    def to_bundle_dto(bundle: dict, currency: str = None) -> BundleDTO:
        bundle_info = bundle.get("bundleInfo", {})
        validity_period = bundle.get("validityPeriodCycle", {})
        validity_details = validity_period.get("details", [])
        if len(validity_details) > 0:
            validity = validity_details[0].get("name", "0 Day")
        else:
            validity = "0 Day"
        bundle_regions = [DtoMapper.to_region_dto(region) for region in bundle.get("supportedZones", [])]
        bundle_category = bundle.get("bundleCategory", {})
        countries = [DtoMapper.to_country_dto(c) for c in bundle.get("supportedCountries", [])]
        currency_code = os.getenv("DEFAULT_CURRENCY",
                                  "EUR") if currency is None else currency
        original_price = bundle["price"]
        price = bundle["exchangedPrice"] if bundle["exchangedPrice"] is not None else original_price
        gprs_limit = bundle_info.get("gprsLimit", 0)
        gprs_limit_display = f'{gprs_limit} {bundle_info.get("dataUnit")}' if gprs_limit >= 0 else "∞ Unlimited"
        if os.getenv("DISPLAY_PRICE", "normal") == "rounded":
            price = int(ceil(price))
            price_display = f'{price} {currency_code}'
        else:
            price_display = f'{round(price, 2):.2f} {currency_code}'
        bundle_data = {
            "display_title": bundle["bundleDetails"][0]["name"],
            "display_subtitle": bundle["bundleDetails"][0]["description"],
            "bundle_code": bundle["recordGuid"],
            "bundle_category": DtoMapper.to_bundle_category_dto(bundle_category),
            "bundle_marketing_name": bundle["bundleDetails"][0]["name"],
            "bundle_name": bundle["bundleDetails"][0]["name"],
            "count_countries": len(countries),
            "currency_code": currency_code,
            "gprs_limit": gprs_limit,
            "gprs_limit_display": gprs_limit_display,
            "original_price": round(original_price, 2),
            "price": round(price, 2),
            "price_display": price_display,
            "unlimited": True if gprs_limit < 0 else False,
            "validity": validity.split(" ")[0],
            "validity_label": validity.split(" ")[1],
            "validity_display": validity,
            "countries": countries,
            "is_stockable": bundle_info.get("isStockable"),
            "bundle_info_code": bundle_info.get("bundleCode"),
            "bundle_region": bundle_regions
        }
        bundle_data[
            "icon"] = f'{SUPABASE_URL}/storage/v1/object/public/media/bundle_{bundle_data.get("bundle_code", "generic")}.png'
        return BundleDTO.model_validate(bundle_data)

    @staticmethod
    def to_country_dto(country: dict) -> CountryDTO:
        country_data = {
            "id": country.get("recordGuid"),
            "alternative_country": country.get("altName", ""),
            "country": country.get("name", "Unknown"),
            "country_code": country.get("isoCode", "Unknown"),
            "iso3_code": country.get("isoCode3", "Unknown"),
            "zone_name": country.get("zone", "Unknown"),
            "icon": f"{SUPABASE_URL}/storage/v1/object/public/media/country/{str(country.get('isoCode3', 'generic')).lower()}.png",
        }
        return CountryDTO.model_validate(country_data)

    @staticmethod
    def to_region_dto(region: dict) -> RegionDTO:
        region_data = {
            "region_code": region.get("tag", "Unknown"),
            "region_name": region.get("name", "Unknown"),
            "zone_name": region.get("name", "Unknown"),
            "icon": f"{SUPABASE_URL}/storage/v1/object/public/media/region/{region.get('tag', 'generic')}.png",
            "guid": region.get("recordGuid", "Unknown"),
        }
        return RegionDTO.model_validate(region_data)

    @staticmethod
    def to_bundle_category_dto(category: dict) -> BundleCategoryDTO | None:
        bundle_category_data = {
            "type": category.get("tag", "Unknown"),
            "title": category.get("name", "Unknown"),
            "code": category.get("recordGuid", "Unknown"),
        }
        return BundleCategoryDTO.model_validate(bundle_category_data)

    @staticmethod
    def to_transaction_history_response(user_profile_bundle: UserProfileBundleModel) -> TransactionHistoryResponse:
        data = {
            "user_order_id": user_profile_bundle.user_order_id,
            "iccid": user_profile_bundle.iccid,
            "bundle_type": user_profile_bundle.bundle_type.value,
            "plan_started": user_profile_bundle.plan_started,
            "bundle_expired": user_profile_bundle.bundle_expired,
            "created_at": user_profile_bundle.created_at,
            "bundle": BundleDTO.model_validate(
                user_profile_bundle.bundle_data) if user_profile_bundle.bundle_data else None,
        }
        return TransactionHistoryResponse.model_validate(data)

    @staticmethod
    def get_profile_current_bundle(user_profile: UserProfileModel):
        #    Sort bundles by created_at in descending order and get the first bundle based on criteria:
        #    1. First bundle with plan_started=True and bundle_expired=False
        #    2. If none found, get first bundle with bundle_expired=False
        #    3. If still none found, return the first bundle in the sorted list
        bundles = user_profile.bundles
        if not bundles:
            return None
        # If there's only one bundle, return it directly
        if len(bundles) == 1:
            return bundles[0]

        # Sort bundles by created_at in descending order
        sorted_bundles = sorted(
            bundles,
            key=lambda bundle: datetime.fromisoformat(bundle.created_at) if bundle.created_at else datetime.min,
            reverse=True
        )

        # Find first bundle with plan_started=True and bundle_expired=False
        priority_bundle = next(
            (bundle for bundle in sorted_bundles if bundle.plan_started and not bundle.bundle_expired),
            None
        )
        if priority_bundle:
            return priority_bundle

        # If no priority bundle found, find first bundle with bundle_expired=False
        unexpired_bundle = next(
            (bundle for bundle in sorted_bundles if not bundle.bundle_expired),
            None
        )
        if unexpired_bundle:
            return unexpired_bundle

        # If no bundle matches criteria, return the first bundle in the sorted list
        return sorted_bundles[0] if sorted_bundles else None

    @staticmethod
    def move_matching_countries_to_top(countries_dto: List[CountryDTO],
                                       searched_countries: List[CountryRequestDto]) -> List[CountryDTO]:
        search_iso3_codes = {country.iso3_code for country in searched_countries if country.iso3_code}

        matching_countries = []
        non_matching_countries = []

        for country in countries_dto:
            if country.iso3_code and country.iso3_code in search_iso3_codes:
                matching_countries.append(country)
            else:
                non_matching_countries.append(country)
        return matching_countries + non_matching_countries

    @staticmethod
    def to_esim_bundle_response(user_profile: UserProfileModel) -> EsimBundleResponse | None:
        profile_current_bundle: UserProfileBundleModel = DtoMapper.get_profile_current_bundle(user_profile)
        if profile_current_bundle is None or profile_current_bundle.bundle_data is None:
            # Handle the case where bundle data is missing
            logger.warning(f"Bundle data missing for user profile {user_profile.id}")
            return None

        # Validate bundle data
        bundle_data: BundleDTO = BundleDTO.model_validate(profile_current_bundle.bundle_data)
        bundle_category = bundle_data.bundle_category

        # Set default values
        display_title = bundle_data.display_title
        icon_url = f"{SUPABASE_URL}/storage/v1/object/public/media/region/generic.png"

        # Initialize search variables
        searched_countries_array = []
        searched_region = None
        try:
            if user_profile.searched_countries:
                search_field = RelatedSearchRequestDto.model_validate_json(user_profile.searched_countries)
                searched_countries_array = search_field.countries if search_field.countries else []
                searched_region = search_field.regions
        except Exception as e:
            logger.debug(f"Exception parsing RelatedSearchRequestDto: {e}")
            pass

        # Determine display title and icon_url based on available data
        if searched_countries_array and len(searched_countries_array) > 0:
            # Get first country code from searched_countries
            first_country = searched_countries_array[0]
            display_title = first_country.country_name
            icon_url = f"{SUPABASE_URL}/storage/v1/object/public/media/country/{str(getattr(first_country.iso3_code, 'country_name', 'generic')).lower()}.png"
            # change countries sorting
        elif bundle_category.type.lower() == "region" and searched_region:
            # No searched countries, check bundle_category.type
            display_title = searched_region.region_name
            icon_url = f"{SUPABASE_URL}/storage/v1/object/public/media/region/{searched_region.iso_code}.png"
        elif bundle_category.type.lower() == "global":
            display_title = bundle_category.title
            icon_url = f"{SUPABASE_URL}/storage/v1/object/public/media/region/Global.png"
        elif bundle_data.countries and len(bundle_data.countries) > 0:
            country = bundle_data.countries[0]
            display_title = country.country
            icon_url = f"{SUPABASE_URL}/storage/v1/object/public/media/country/{str(getattr(country.iso3_code, 'country_name', 'generic')).lower()}.png"
        if bundle_data.label is not None and bundle_data.label != "":
            display_title = bundle_data.label

        countries_sorted = DtoMapper.move_matching_countries_to_top(bundle_data.countries, searched_countries_array)
        data = {
            "is_topup_allowed": user_profile.allow_topup,
            "plan_started": profile_current_bundle.plan_started,
            "bundle_expired": profile_current_bundle.bundle_expired,
            "label_name": user_profile.label or None,
            "order_number": user_profile.user_order_id,
            "order_status": "Active" if not profile_current_bundle.bundle_expired else "Expired",
            "searched_countries": [],
            "qr_code_value": f"LPA:1${user_profile.smdp_address}${user_profile.activation_code}",
            'activation_code': user_profile.activation_code,
            "smdp_address": user_profile.smdp_address,
            "validity_date": user_profile.validity,
            "iccid": user_profile.iccid,
            "payment_date": profile_current_bundle.created_at,
            "shared_with": None,
            "display_title": display_title,
            "display_subtitle": bundle_data.display_subtitle,
            "bundle_code": bundle_data.bundle_code,
            "bundle_category": bundle_data.bundle_category,
            "bundle_marketing_name": bundle_data.bundle_marketing_name,
            "bundle_name": bundle_data.bundle_name,
            'count_countries': bundle_data.count_countries,
            "currency_code": bundle_data.currency_code,
            "gprs_limit_display": bundle_data.gprs_limit_display,
            "price": bundle_data.price,
            "price_display": bundle_data.price_display,
            "unlimited": bundle_data.unlimited,
            "validity": bundle_data.validity,
            "validity_display": bundle_data.validity_display,
            "plan_type": bundle_data.plan_type,
            "activity_policy": "",
            "bundle_message": [],
            "countries": countries_sorted,
            "icon": icon_url,
            "transaction_history": [DtoMapper.to_transaction_history_response(bundle) for bundle in
                                    user_profile.bundles],
        }
        return EsimBundleResponse.model_validate(data)

    @staticmethod
    def to_user_notification_response(notification: NotificationModel) -> UserNotificationResponse:
        data_dict = json.loads(notification.data)
        data = {
            "notification_id": notification.id,
            "title": notification.title,
            "content": notification.content,
            "datetime": notification.created_at,
            "transaction_status": data_dict.get("transaction_status", ""),
            "transaction": data_dict.get("transaction", ""),
            "transaction_message": data_dict.get("transaction_message", ""),
            "status": notification.status,
            "iccid": data_dict.get("iccid", ""),
            "category": data_dict.get("category", ""),
            "translated_message": data_dict.get("translated_message", "")
        }
        return UserNotificationResponse.model_validate(data)

    @staticmethod
    def to_consumption_response(data: dict) -> ConsumptionResponse:
        consumption_data = {
            "data_allocated": data["dataAllocated"],
            "data_used": data["dataUsed"],
            "data_remaining": data["dataRemaining"],
            "data_allocated_display": f'{data["dataAllocated"]} {data["dataUnit"]}',
            "data_used_display": f'{data["dataUsed"]} {data["dataUnit"]}',
            "data_remaining_display": f'{data["dataRemaining"]} {data["dataUnit"]}',
            "plan_status": data["planStatus"]
        }
        return ConsumptionResponse.model_validate(consumption_data)

    @staticmethod
    def to_order_notification_model(bundle: UserProfileBundleWithProfileModel, user_id: str,
                                    user_metadata: dict, iccid: str) -> CallBackNotificationInfoModel:
        first_name = user_metadata.get("first_name", "")
        last_name = user_metadata.get("last_name", "")
        full_name = f"{first_name} {last_name}".strip()
        user_display_name = full_name or user_metadata.get("email") or "User"
        bundle_display_name = bundle.label

        bundle_data = bundle.bundles[0]
        for bundle_profile in bundle.bundles:
            if bundle_profile.iccid == iccid:
                bandle_data = bundle_profile

        if not bundle_display_name and bundle and bundle_data.bundle_data:
            bundle_display_name = bundle_data.bundle_data.get("display_title")
        bundle_display_name = bundle_display_name or "Bundle"

        return CallBackNotificationInfoModel(
            user_id=user_id,
            user_display_name=user_display_name,
            bundle_display_name=bundle_display_name,
            iccid=bundle.iccid,
            validity=bundle.validity,
            label=bundle.label,
            smdp_address=bundle.smdp_address,
            activation_code=bundle.activation_code,
            allow_topup=bundle.allow_topup,
            esim_hub_order_id=bundle.esim_hub_order_id,
            searched_countries=bundle.searched_countries,
        )

    @staticmethod
    def to_user_order_history(user_order: UserOrderModel):
        data = {
            "order_number": user_order.id,
            "order_status": user_order.payment_status,
            "order_amount": user_order.amount,
            "order_currency": user_order.currency,
            "order_display_price": str(float(user_order.amount) / 100) + " " + user_order.currency,
            "order_date": user_order.created_at,
            "order_type": user_order.order_type,
            "bundle_details": BundleDTO.model_validate_json(user_order.bundle_data),
            "company_name": os.getenv("MERCHANT_DISPLAY_NAME", "Company Name"),
            "payment_type": os.getenv("PAYMENT_METHODS", PaymentTypeEnum.CARD)
        }
        return UserOrderHistoryResponse.model_validate(data)

    @staticmethod
    def to_page_content_response(content_response: ContentResponse) -> PageContentResponse:
        data = {
            "page_title": content_response.contentCategory.contentCategoryDetails[0].name,
            "page_content": content_response.contentDetails[0].description,
            "page_intro": ""
        }
        return PageContentResponse.model_validate(data)

    @staticmethod
    def to_auth_response(supabase_response: AuthResponse, user_wallet: UserWalletResponse = None) -> AuthResponseDTO:
        user_metadata = supabase_response.user.user_metadata
        fullname = user_metadata.get("full_name", "").strip()  # Ensure it's a string and remove extra spaces

        first_name = user_metadata.get("first_name", "")
        last_name = user_metadata.get("last_name", "")
        referral_code = user_metadata.get("referral_code", "")

        if fullname and first_name == "":
            name_parts = fullname.split()
            first_name = name_parts[0] if len(name_parts) > 0 else ""
            last_name = name_parts[1] if len(name_parts) > 1 else ""
        msisdn = user_metadata.get("msisdn", "")
        user_email = user_metadata.get("email") if not supabase_response.user.email else supabase_response.user.email
        if user_email.startswith(msisdn):
            user_email = user_metadata.get("display_email", None)
        user_info = UserInfo(
            is_verified=user_metadata.get("email_verified", False),
            first_name=first_name,
            last_name=last_name,
            msisdn=msisdn,
            email=user_email,
            user_token=supabase_response.user.id,
            should_notify=user_metadata.get("should_notify", False),
            referral_code=referral_code,
            balance=user_wallet.balance if user_wallet else 0,
            currency_code=user_wallet.currency if user_wallet else "",
        )

        if not hasattr(supabase_response, "session"):
            return AuthResponseDTO(
                access_token="",
                refresh_token="",
                user_info=user_info,
                user_token=supabase_response.user.id,
                is_verified=user_info.is_verified
            )

        return AuthResponseDTO(
            access_token=supabase_response.session.access_token or "",
            refresh_token=supabase_response.session.refresh_token or "",
            user_info=user_info,
            user_token=supabase_response.user.id,
            is_verified=user_info.is_verified
        )

    @staticmethod
    def to_user_wallet_response(user_wallet: UserWalletModel) -> UserWalletResponse:
        data = {
            "balance": user_wallet.amount,
            "currency": user_wallet.currency
        }
        return UserWalletResponse.model_validate(data)

    @staticmethod
    def bundle_currency_update(bundle: BundleDTO, currency: str = None, rate: float = 1.0) -> BundleDTO:
        if rate == 1.0:
            currency = os.getenv("DEFAULT_CURRENCY")
        price = bundle.original_price * rate
        bundle.currency_code = currency
        if os.getenv("DISPLAY_PRICE", "normal") == "rounded":
            price = int(ceil(price))
        bundle.price = price
        bundle.price_display = f'{bundle.price:.2f} {bundle.currency_code}'
        return bundle

    @staticmethod
    def to_currency_dto(currency: CurrencyModel) -> CurrencyDto:
        currency_dto = {
            "currency": currency.name
        }
        return CurrencyDto.model_validate(currency_dto)

    @staticmethod
    def to_promotion_history_dto(promotion_usage: PromotionUsageModel, name: str,
                                 promotion_name) -> PromotionHistoryDto:
        is_referral = False
        if promotion_usage.referral_code:
            is_referral = True

        promotion_history_data = {
            "is_referral": is_referral,
            "amount": f'{round(promotion_usage.amount, 2):.2f} {os.getenv("DEFAULT_CURRENCY")}',
            "name": name,
            "promotion_name": promotion_name,
            "date": promotion_usage.created_at
        }

        return PromotionHistoryDto.model_validate(promotion_history_data)

    @staticmethod
    def to_exchange_rate(data: dict) -> ExchangeRate:
        data = {
            "system_currency_code": data.get("systemCurrencyCode", ""),
            "currency_code": data.get("currencyCode", ""),
            "current_rate": data.get("currentRate", ""),
            "new_rate": data.get("newRate", ""),
        }
        return ExchangeRate.model_validate(data)
