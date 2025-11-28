import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import ceil
from typing import List

from gotrue import AuthResponse
from loguru import logger

from app.config.context import currency_context
from app.models import UserProfileBundleModel, UserProfileModel, UserOrderModel, UserWalletModel
from app.models.currency import CurrencyModel
from app.models.notification import NotificationModel
from app.models.promotion_usage import PromotionUsageModel
from app.schemas.app import UserNotificationResponse, PageContentResponse, ExchangeRate
from app.schemas.auth import AuthResponseDTO, UserInfo
from app.schemas.bundle import EsimBundleResponse, ConsumptionResponse, TransactionHistoryResponse, \
    UserOrderHistoryResponse, RelatedSearchRequestDto, CountryRequestDto
from app.schemas.esim_hub import ContentResponse
from app.schemas.home import BundleDTO, BundleCategoryDTO, CountryDTO, RegionDTO, CurrencyDto
from app.schemas.promotion import PromotionHistoryDto
from app.schemas.user_bundle import CallBackNotificationInfo
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
        currency_code = currency_context.get() if currency is None else currency
        original_price = bundle["price"]
        price = bundle["exchangedPrice"] if bundle["exchangedPrice"] is not None else original_price
        gprs_limit = bundle_info.get("gprsLimit", 0)
        gprs_limit_display = f'{gprs_limit} {bundle_info.get("dataUnit")}' if gprs_limit >= 0 else "∞ Unlimited"
        if os.getenv("DISPLAY_PRICE", "normal") == "rounded":
            price_display = f'{int(ceil(round(price, 2)))} {currency_code}'
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
            "bundle_region": bundle_regions,
            "is_active": bundle.get("isActive", True),
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
    def to_transaction_history_response(user_profile_bundle: UserProfileBundleModel,
                                        x_currency: str,
                                        rate: float = 1) -> TransactionHistoryResponse:
        bundle = None
        if user_profile_bundle.bundle_data:
            bundle = BundleDTO.model_validate(user_profile_bundle.bundle_data)
            bundle = DtoMapper.bundle_currency_update(bundle, currency=x_currency, rate=rate)
        data = {
            "user_order_id": str(user_profile_bundle.user_order_id),
            "iccid": user_profile_bundle.iccid,
            "bundle_type": user_profile_bundle.bundle_type,
            "plan_started": user_profile_bundle.plan_started,
            "bundle_expired": user_profile_bundle.bundle_expired,
            "created_at": str(user_profile_bundle.created_at),
            "bundle": bundle,
        }
        return TransactionHistoryResponse.model_validate(data)

    @staticmethod
    def get_profile_current_bundle(user_profile: UserProfileModel):
        bundles = user_profile.user_profile_bundle
        if not bundles:
            return None
        if len(bundles) == 1:
            return bundles[0]

        def get_created_at_datetime(bundle):
            """Helper to safely get created_at as datetime object"""
            if not bundle.created_at:
                return datetime.min
            if isinstance(bundle.created_at, datetime):
                return bundle.created_at
            if isinstance(bundle.created_at, str):
                try:
                    # Handle ISO format strings with 'Z' suffix
                    normalized = bundle.created_at.replace('Z', '+00:00') if bundle.created_at.endswith(
                        'Z') else bundle.created_at
                    return datetime.fromisoformat(normalized)
                except Exception:
                    return datetime.min
            return datetime.min

        sorted_bundles = sorted(
            bundles,
            key=get_created_at_datetime,
            reverse=True
        )

        priority_bundle = next(
            (bundle for bundle in sorted_bundles if bundle.plan_started and not bundle.bundle_expired),
            None
        )
        if priority_bundle:
            return priority_bundle

        unexpired_bundle = next(
            (bundle for bundle in sorted_bundles if not bundle.bundle_expired),
            None
        )
        if unexpired_bundle:
            return unexpired_bundle

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
    def to_esim_bundle_response(user_profile: UserProfileModel, rate: float,
                                x_currency: str, tax: float = 0) -> EsimBundleResponse | None:
        if isinstance(rate,Decimal):
            rate = float(rate)
        if isinstance(tax,Decimal):
            tax = float(tax)
        profile_current_bundle: UserProfileBundleModel = DtoMapper.get_profile_current_bundle(user_profile)
        if profile_current_bundle is None or profile_current_bundle.bundle_data is None:
            logger.warning(f"Bundle data missing for user profile {user_profile.id}")
            return None
        bundle_data: BundleDTO = BundleDTO.model_validate(profile_current_bundle.bundle_data)
        bundle_category = bundle_data.bundle_category

        display_title = bundle_data.display_title
        icon_url = f"{SUPABASE_URL}/storage/v1/object/public/media/region/generic.png"

        searched_countries_array = []
        searched_region = None
        try:
            if user_profile.searched_countries:
                search_field = RelatedSearchRequestDto.model_validate_json(user_profile.searched_countries)
                searched_countries_array = search_field.countries if search_field.countries else []
                searched_region = search_field.regions
        except Exception as e:
            logger.debug(f"Exception parsing RelatedSearchRequestDto: {e}")
        if searched_countries_array and len(searched_countries_array) > 0:
            first_country = searched_countries_array[0]
            display_title = first_country.country_name
            icon_url = f"{SUPABASE_URL}/storage/v1/object/public/media/country/{str(first_country.iso3_code).lower()}.png"

        elif bundle_category.type.lower() == "region" and searched_region:
            display_title = searched_region.region_name
            icon_url = f"{SUPABASE_URL}/storage/v1/object/public/media/region/{searched_region.iso_code}.png"
        elif bundle_category.type.lower() == "global":
            display_title = bundle_category.title
            icon_url = f"{SUPABASE_URL}/storage/v1/object/public/media/region/Global.png"
        elif bundle_data.countries and len(bundle_data.countries) > 0:
            country = bundle_data.countries[0]
            display_title = country.country
            icon_url = f"{SUPABASE_URL}/storage/v1/object/public/media/country/{str(country.iso3_code).lower()}.png"
        if bundle_data.label is not None and bundle_data.label != "":
            display_title = bundle_data.label

        countries_sorted = DtoMapper.move_matching_countries_to_top(bundle_data.countries, searched_countries_array)

        if not profile_current_bundle.plan_started:
            order_status = "Inactive"
        elif not profile_current_bundle.bundle_expired:
            order_status = "Active"
        else:
            order_status = "Expired"
        # Convert Decimal to float to avoid type mismatch errors
        original_price_float = float(bundle_data.original_price) if bundle_data.original_price else 0.0
        amount = (original_price_float * rate) + ((tax / 100) * rate)
        data = {
            "is_topup_allowed": user_profile.allow_topup,
            "plan_started": profile_current_bundle.plan_started,
            "bundle_expired": profile_current_bundle.bundle_expired,
            "label_name": user_profile.label or None,
            "order_number": str(user_profile.user_order_id),
            "order_status": order_status,
            "searched_countries": [],
            "qr_code_value": f"LPA:1${user_profile.smdp_address}${user_profile.activation_code}",
            'activation_code': user_profile.activation_code,
            "smdp_address": user_profile.smdp_address,
            "validity_date": user_profile.validity,
            "iccid": user_profile.iccid,
            "payment_date": str(profile_current_bundle.created_at),
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
            "price": amount,
            "price_display": f"{round(amount, 2)} {x_currency}",
            "unlimited": bundle_data.unlimited,
            "validity": bundle_data.validity,
            "validity_label": bundle_data.validity_label,
            "validity_display": bundle_data.validity_display,
            "plan_type": bundle_data.plan_type,
            "activity_policy": "",
            "bundle_message": [],
            "countries": countries_sorted,
            "icon": icon_url,
            "transaction_history": [
                DtoMapper.to_transaction_history_response(user_profile_bundle=bundle, rate=rate, x_currency=x_currency)
                for
                bundle in
                user_profile.user_profile_bundle],
        }
        return EsimBundleResponse.model_validate(data)

    @staticmethod
    def to_user_notification_response(notification: NotificationModel) -> UserNotificationResponse:
        data_dict = json.loads(notification.data)
        data = {
            "notification_id": notification.id,
            "title": notification.title,
            "content": notification.content,
            "datetime": str(notification.created_at),
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
            "plan_status": data["planStatus"],
            "expiry_date": data["profileExpiryDate"],
        }
        return ConsumptionResponse.model_validate(consumption_data)

    @staticmethod
    def to_order_notification_model(bundle: UserProfileModel, user_id: str,
                                    user_metadata: dict, iccid: str) -> CallBackNotificationInfo:
        # Extract user display name
        first_name = user_metadata.get("first_name", "")
        last_name = user_metadata.get("last_name", "")
        full_name = f"{first_name} {last_name}".strip()
        user_display_name = full_name or user_metadata.get("email") or "User"

        # Figure out bundle display name and DTO from bundles list (can contain BundleDTO or UserProfileBundleModel)
        bundle_display_name = bundle.label
        bundle_dto: BundleDTO | None = None
        if getattr(bundle, 'bundles', None):
            first = bundle.user_profile_bundle[0]
            try:
                if isinstance(first, BundleDTO):
                    bundle_dto = first
                elif hasattr(first, 'bundle_data') and first.bundle_data:
                    bundle_dto = BundleDTO.model_validate(first.bundle_data)
            except Exception as e:
                logger.debug(f"Failed to coerce first bundle to BundleDTO: {e}")

        # Derive bundle display name from DTO if not explicitly labeled
        if not bundle_display_name and bundle_dto:
            bundle_display_name = getattr(bundle_dto, 'display_title', None)
        bundle_display_name = bundle_display_name or "Bundle"

        # Determine bundle expiration in days from validity_display when available
        bundle_expiration_days = 0
        if bundle_dto and getattr(bundle_dto, 'validity_display', None):
            parts = str(bundle_dto.validity_display).split()
            if parts:
                try:
                    amount = int(parts[0])
                    unit = parts[1].lower() if len(parts) > 1 else "day"
                    if unit.startswith("day"):
                        multiplier = 1
                    elif unit.startswith("week"):
                        multiplier = 7
                    elif unit.startswith("month"):
                        multiplier = 30
                    elif unit.startswith("year"):
                        multiplier = 365
                    else:
                        multiplier = 0
                    bundle_expiration_days = amount * multiplier
                except Exception as e:
                    logger.debug(f"Unable to parse validity_display '{bundle_dto.validity_display}': {e}")

        # Compute validity datetime safely (created_at can be string)
        validity_str = None
        created_at_str = getattr(bundle, 'created_at', None)
        created_at_dt = None
        if isinstance(created_at_str, str) and created_at_str:
            # Normalize Z to +00:00 for fromisoformat compatibility
            normalized = created_at_str.replace('Z', '+00:00') if created_at_str.endswith('Z') else created_at_str
            try:
                created_at_dt = datetime.fromisoformat(normalized)
            except Exception:
                created_at_dt = None
        if created_at_dt is None:
            try:
                created_at_dt = datetime.now(tz=timezone.utc)
            except Exception:
                created_at_dt = datetime.now()

        try:
            validity_dt = created_at_dt + timedelta(days=bundle_expiration_days)
            validity_str = validity_dt.isoformat()
        except Exception as e:
            logger.debug(f"Failed computing validity from created_at + delta: {e}")

        # Fallback to bundle.validity string if computation failed
        if not validity_str and getattr(bundle, 'validity', None):
            validity_str = bundle.validity

        return CallBackNotificationInfo(
            user_id=str(user_id),
            user_display_name=user_display_name,
            bundle_display_name=bundle_display_name,
            iccid=bundle.iccid,
            validity=validity_str or "",
            label=bundle.label,
            smdp_address=bundle.smdp_address,
            activation_code=bundle.activation_code,
            allow_topup=bundle.allow_topup,
            esim_hub_order_id=bundle.esim_hub_order_id,
            searched_countries=bundle.searched_countries,
        )

    @staticmethod
    def to_user_order_history(user_order: UserOrderModel, rate: float = 1.0,
                              currency: str = None) -> UserOrderHistoryResponse:
        # Convert Decimal to float to avoid type mismatch errors
        modified_or_amount = float(user_order.modified_amount) if user_order.modified_amount is not None else float(
            user_order.amount)
        tax_amount = float(user_order.tax_amount) if user_order.tax_amount else 0.0
        amount = modified_or_amount + tax_amount
        data = {
            "order_number": str(user_order.id),
            "order_status": user_order.payment_status,
            "order_amount": (amount * rate),
            "order_currency": user_order.currency,
            "order_display_price": f"{round((amount / 100) * rate, 2)} {currency}",
            "order_date": user_order.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "order_type": user_order.order_type,
            "bundle_details": BundleDTO.model_validate_json(user_order.bundle_data),
            "company_name": os.getenv("MERCHANT_DISPLAY_NAME", "Company Name"),
            "company_address": os.getenv("MERCHANT_ADDRESS", "Company Address"),
            "company_phone": os.getenv("MERCHANT_PHONE", "Company Phone"),
            "company_email": os.getenv("MERCHANT_EMAIL", "Company Email"),
            "company_website": os.getenv("MERCHANT_WEBSITE", "https://example.com"),
            "payment_type": user_order.payment_type
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
    def to_auth_response(supabase_response: AuthResponse, user_wallet: UserWalletResponse = None,
                         currency: str = os.getenv("DEFAULT_CURRENCY")) -> AuthResponseDTO:
        user_metadata = supabase_response.user.user_metadata
        fullname = user_metadata.get("full_name", "").strip()

        first_name = user_metadata.get("first_name", "")
        last_name = user_metadata.get("last_name", "")
        referral_code = user_metadata.get("referral_code", "")
        login_type = user_metadata.get("login_type", "email")
        email_editable = login_type != "email"
        phone_editable = login_type != "phone"

        if fullname and first_name == "":
            name_parts = fullname.split()
            first_name = name_parts[0] if len(name_parts) > 0 else ""
            last_name = name_parts[1] if len(name_parts) > 1 else ""
        msisdn = user_metadata.get("msisdn", "")
        user_email = user_metadata.get("email") if not supabase_response.user.email else supabase_response.user.email
        if user_email.startswith(msisdn):
            user_email = user_metadata.get("display_email", None)
        if user_email is None or user_email == "":
            user_email = user_metadata.get("email", None)
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
            currency_code=user_metadata.get("currency", os.getenv("DEFAULT_CURRENCY")),
            email_editable=email_editable,
            phone_editable=phone_editable,
            language=user_metadata.get("language", "En"),
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
        # Convert Decimal to float to avoid type mismatch errors
        price = float(bundle.original_price) * float(rate)
        bundle.currency_code = currency
        if os.getenv("DISPLAY_PRICE", "normal") == "rounded":
            price = int(ceil(price))
        bundle.price = price
        bundle.price_display = f'{bundle.price:.2f} {currency}'
        return bundle

    @staticmethod
    def to_currency_dto(currency: CurrencyModel) -> CurrencyDto:
        currency_dto = {
            "currency": currency.name
        }
        return CurrencyDto.model_validate(currency_dto)

    @staticmethod
    def to_promotion_history_dto(promotion_usage: PromotionUsageModel, name: str,
                                 promotion_name, rate: float, currency: str) -> PromotionHistoryDto:
        is_referral = False
        if promotion_usage.referral_code:
            is_referral = True

        # Convert Decimal to float to avoid type mismatch errors
        amount_float = float(promotion_usage.amount) if promotion_usage.amount else 0.0

        promotion_history_data = {
            "is_referral": is_referral,
            "amount": f'{round(amount_float * rate, 2):.2f} {currency}',
            "name": promotion_usage.referred_to,
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
