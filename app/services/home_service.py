import os
from typing import Literal, List

import aiocache
from loguru import logger

from app.config.config import esim_hub_service_instance
from app.config.db import ConfigKeysEnum
from app.schemas.home import HomeResponseDto, BundleDTO
from app.schemas.response import Response, ResponseHelper
from app.services.currency_service import CurrencyService
from app.services.grouping_service import GroupingService


class HomeService:

    def __init__(self):
        self.__esim_hub_service = esim_hub_service_instance()
        self.__grouping_service = GroupingService()
        self.__currency_service = CurrencyService()

    async def home(self) -> Response[HomeResponseDto]:
        all_countries = await self.__get_countries()
        regions = await self.__get_regions()
        cruise_bundles = await self.__get_bundles_by_category("CRUISE")
        all_global_bundles = await self.__get_bundles_by_category("GLOBAL")
        global_bundles = []
        for bundle in all_global_bundles:
            if len(bundle.countries) >= os.getenv("GLOBAL_COUNTRIES_COUNT", 50):
                global_bundles.append(bundle)

        home_response = {
            "countries": all_countries,
            "regions": regions,
            "cruise_bundles": cruise_bundles,
            "global_bundles": global_bundles
        }
        return ResponseHelper.success_data_response(HomeResponseDto(**home_response), 0)

    async def home_v2(self, currency: str, locale: str) -> Response[HomeResponseDto]:
        from app.repo.config_repo import ConfigRepo
        config_repo = ConfigRepo()
        bundle_key_config = await config_repo.get_first_by({"key": ConfigKeysEnum.APP_CACHE_KEY})
        bundle_key = bundle_key_config.value if bundle_key_config else "default"
        cache_key = f"home:{bundle_key}:{currency}:{locale}"
        cached_response = await self.__read_from_cache(cache_key)
        if cached_response:
            logger.info(f"getting response from cache: {cache_key}")
            return ResponseHelper.success_data_response(cached_response, 0)
        all_countries = await self.__get_countries_v2(locale)
        regions = await self.__get_regions_v2(locale)
        rate = await self.__currency_service.aget_rate_by_currency(currency)
        cruise_bundles = await self.__grouping_service.get_cruise_bundle(rate=rate, currency_name=currency,
                                                                         locale=locale)
        if len(cruise_bundles) > 0:
            cruise_bundles.sort(key=lambda bundle: bundle.price or 0, reverse=False)
        all_global_bundles = await self.__grouping_service.get_global_bundle(rate=rate, currency_name=currency,
                                                                             locale=locale)
        if len(all_global_bundles) > 0:
            all_global_bundles.sort(key=lambda bundle: bundle.price or 0, reverse=False)
        global_bundles = [bundle for bundle in all_global_bundles
                          if len(bundle.countries) >= int(os.getenv("GLOBAL_COUNTRIES_COUNT", 50))]

        home_response = {
            "countries": all_countries,
            "regions": regions,
            "cruise_bundles": cruise_bundles,
            "global_bundles": global_bundles
        }

        # Create the response with validated DTO
        home_dto = HomeResponseDto(**home_response)
        await self.__store_in_cache(cache_key, home_dto)
        return ResponseHelper.success_data_response(home_dto, 0)

    async def get_cruise_bundles(self, currency: str, locale: str) -> Response[HomeResponseDto]:
        rate = await self.__currency_service.aget_rate_by_currency(currency)
        cruise_bundles = await self.__grouping_service.get_cruise_bundle(rate=rate, currency_name=currency,
                                                                         locale=locale)
        cruise_bundles.sort(key=lambda bundle: bundle.price or 0, reverse=False)
        home_response = {
            "countries": [],
            "regions": [],
            "cruise_bundles": cruise_bundles,
            "global_bundles": []
        }
        return ResponseHelper.success_data_response(HomeResponseDto(**home_response), 0)

    async def get_land_bundles(self, currency: str, locale: str) -> Response[HomeResponseDto]:
        all_countries = await self.__get_countries_v2(locale)
        regions = await self.__get_regions_v2(locale)
        rate = await self.__currency_service.aget_rate_by_currency(currency)
        all_global_bundles = await self.__grouping_service.get_global_bundle(rate=rate, currency_name=currency,
                                                                             locale=locale)
        all_global_bundles.sort(key=lambda bundle: bundle.price or 0, reverse=False)
        global_bundles = []
        for bundle in all_global_bundles:
            if len(bundle.countries) >= os.getenv("GLOBAL_COUNTRIES_COUNT", 50):
                global_bundles.append(bundle)
        global_bundles = await self.__grouping_service.get_global_bundle(rate=rate, currency_name=currency,
                                                                         locale=locale)
        global_bundles.sort(key=lambda bundle: bundle.price or 0, reverse=False)

        home_response = {
            "countries": all_countries,
            "regions": regions,
            "cruise_bundles": [],
            "global_bundles": global_bundles
        }
        return ResponseHelper.success_data_response(HomeResponseDto(**home_response), 0)

    async def __get_countries(self):
        try:
            return await self.__esim_hub_service.get_countries()
        except Exception as e:
            logger.error(f"error while getting countries: {str(e)}")
            return []

    async def __get_regions(self):
        try:
            return await self.__esim_hub_service.get_regions()
        except Exception as e:
            logger.error(f"error while getting regions: {str(e)}")
            return []

    async def __get_bundles_by_category(self, category: Literal["CRUISE", "GLOBAL"]) -> List[BundleDTO]:
        try:
            return await self.__esim_hub_service.get_bundles_by_category(category=category)
        except Exception as e:
            logger.error(f"error while getting cruise bundles: {str(e)}")
            return []

    async def __get_countries_v2(self, locale: str):
        try:
            return await self.__grouping_service.get_all_countries(locale)
        except Exception as e:
            logger.error(f"error while getting countries: {str(e)}")
            return []

    async def __get_regions_v2(self, locale: str):
        try:
            return await self.__grouping_service.get_all_regions(locale)
        except Exception as e:
            logger.error(f"error while getting regions: {str(e)}")
            return []

    async def __store_in_cache(self, cache_key: str, data: HomeResponseDto):
        try:
            cache = aiocache.caches.get("default")
            await cache.clear()  # Remove all old cache keys before storing new
            await cache.set(cache_key, data.model_dump_json(), ttl=333600)
            logger.info(f"Stored data in cache with key: {cache_key}")
        except Exception as e:
            logger.error(f"Error storing data in cache: {e}")

    async def __read_from_cache(self, cache_key) -> HomeResponseDto | None:
        try:
            cached_data = await aiocache.caches.get("default").get(cache_key)
            if cached_data:
                logger.info(f"Retrieved data from cache with key: {cache_key}")
                return HomeResponseDto.model_validate_json(cached_data)
            else:
                logger.info(f"No data found in cache for key: {cache_key}")
                return None
        except Exception as e:
            logger.error(f"Error reading from cache: {e}")
            return None
