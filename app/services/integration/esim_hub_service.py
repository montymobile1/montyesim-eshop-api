import os
from typing import List, Literal, Optional, Dict, Any, Union

import httpx
from loguru import logger

from app.config.api import EsimHubEndpoint
from app.exceptions import EsimHubException
from app.schemas.bundle import ConsumptionResponse
from app.schemas.dto_mapper import DtoMapper
from app.schemas.esim_hub import EsimHubOrderResponse, GlobalConfigurationResponse, ContentResponse
from app.schemas.home import RegionDTO, CountryDTO, BundleDTO, AllBundleResponse


class EsimHubService:

    def __init__(self, base_url: str, api_key: str, tenant_key: str):
        self.__api_key = api_key
        self.__base_url = base_url
        self.__tenant_key = tenant_key

    async def get_regions(self) -> List[RegionDTO]:
        params = {
            "pageIndex": 1,
            "pageSize": 300,
            "Name": "",
            "IsoCode": "",
            "CountryCode": ""
        }
        response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_GET_REGIONS, params=params)
        if not "success" in response:
            raise EsimHubException(response)
        regions = []
        for region in response["data"]["zones"]:
            try:
                regions.append(DtoMapper.to_region_dto(region))
            except Exception as e:
                logger.error(f"error while mapping region : {str(e)}")
                continue
        return regions

    async def health_check(self):
        try:
            configurations_status = await self.__do_request(method="GET", path="/configuration")
        except Exception as e:
            logger.error(f"error while getting configurations status {e}")
            configurations_status = {"success": False}
        try:
            catalog_status = await self.__do_request(method="GET", path="/catalog")
        except Exception as e:
            logger.error(f"error while getting catalog status {e}")
            catalog_status = {"success": False}
        try:
            core_status = await self.__do_request(method="GET", path="/core", base_url=os.getenv("ESIM_HUB_BASE_URL2"))
        except Exception as e:
            logger.error(f"error while getting core status {e}")
            core_status = {"success": False}
        return {
            "configurations_status": "ok" if "success" in configurations_status and configurations_status[
                "success"] else "failed",
            "catalog_status": "ok" if "success" in catalog_status and catalog_status["success"] else "failed",
            "core_status": "ok" if "success" in core_status and core_status["success"] else "failed",
        }

    async def get_countries(self) -> List[CountryDTO]:
        params = {
            "pageIndex": 1,
            "pageSize": 300,
            "Name": "",
            "IsoCode": "",
            "CountryCode": ""
        }
        response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_GET_COUNTRIES,
                                           base_url=os.getenv("ESIM_HUB_BASE_URL2"), params=params)
        if not "success" in response:
            raise EsimHubException(response)
        countries = []
        for country in response["data"]["countries"]:
            try:
                countries.append(DtoMapper.to_country_dto(country))
            except Exception as e:
                logger.error(f"error while mapping country : {str(e)}")
                continue
        return countries

    async def get_all_bundles(self, page_index: int = 1, page_size: int = 100,
                              currency_code=os.getenv("DEFAULT_CURRENCY")) -> AllBundleResponse:
        params = {
            "pageIndex": page_index,
            "pageSize": page_size,
            "CurrencyCode": currency_code,
        }
        response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_GET_ALL_BUNDLES, params=params)
        if not "success" in response:
            raise EsimHubException(response)
        bundles = []
        for bundle in response["data"]["items"]:
            try:
                bundles.append(DtoMapper.to_bundle_dto(bundle=bundle, currency=currency_code))
            except Exception as e:
                logger.error(f"error while mapping bundle: {str(e)}")
                continue
        return AllBundleResponse(total_rows=response["data"]["totalRows"], bundles=bundles)

    async def get_bundles_by_category(self, category: str, currency_code: str = os.getenv("DEFAULT_CURRENCY")) -> List[
        BundleDTO]:
        params = {
            "CategoryTags": category,
            "CurrencyCode": currency_code,
            "pageIndex": 1,
            "pageSize": 300,
            "SortBy": "PRICE_ASC",
        }
        response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_GET_BUNDLES_BY_CATEGORY,
                                           params=params)
        if not "success" in response:
            raise EsimHubException(response)
        bundles = []
        for bundle in response["data"]["items"]:
            try:
                bundles.append(DtoMapper.to_bundle_dto(bundle=bundle, currency=currency_code))
            except Exception as e:
                logger.error(f"error while mapping bundle: {str(e)}")
                continue
        return bundles

    async def get_bundles_by_zone(self, zone: str, currency_code: str = os.getenv("DEFAULT_CURRENCY")) -> List[
        BundleDTO]:
        params = {
            "ZoneGuids": zone,
            "CurrencyCode": currency_code,
            "pageIndex": 1,
            "pageSize": 300,
            "SortBy": "PRICE_ASC",
        }
        response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_GET_BUNDLES_BY_ZONE, params=params)
        logger.debug(response)
        if not "success" in response:
            raise EsimHubException(response)
        bundles = []
        for bundle in response["data"]["items"]:
            try:
                bundles.append(DtoMapper.to_bundle_dto(bundle=bundle, currency=currency_code))
            except Exception as e:
                logger.error(f"error while mapping bundle : {str(e)}")
                continue
        return bundles

    async def get_activation_code(self, order_id: str) -> str | None:
        params = {
            "orderId": order_id,
        }
        try:
            response = await self.__do_request("GET", EsimHubEndpoint.API_GET_ACTIVATION_CODE, params=params,
                                               base_url=os.getenv("ESIM_HUB_BASE_URL2"))
            if not "success" in response:
                logger.error(f"error while getting activation code {response}")
                return None
            return response["data"]["activationCode"]
        except Exception as e:
            logger.error(f"error while getting activation code : {str(e)}")
            return None

    async def create_reseller_order(self, bundle_code: str, order_id: str) -> EsimHubOrderResponse | None:
        request_body = {
            "BundleGuid": bundle_code,
            "Quantity": 1,
            "UniqueIdentifier": order_id,
            "ServiceTag": "ESIM"

        }
        try:
            response = await self.__do_request(method="POST", path=EsimHubEndpoint.API_CREATE_RESELLER_ORDER,
                                               body=request_body,
                                               base_url=os.getenv("ESIM_HUB_BASE_URL2"))
            logger.debug(f"request body: {request_body}")
            logger.debug(f"response: {response}")
            if not "success" in response or response["success"] == False:
                logger.error("Failed to create reseller Hub: {}".format(response["message"]))
                return None
            response_data = response["data"]
            esim_order_id = response_data["orderId"]
            activation_code = await self.get_activation_code(esim_order_id)
            response_data["activationCode"] = activation_code
            return EsimHubOrderResponse.model_validate(response_data)
        except Exception as e:
            logger.error(f"Failed to create reseller order: {str(e)}")
            return None

    async def create_reseller_topup(self, bundle_code: str, esim_hub_order_id,
                                    order_id: str) -> EsimHubOrderResponse | None:
        request_body = {
            "BundleGuid": bundle_code,
            "OrderId": esim_hub_order_id,
            "UniqueIdentifier": order_id,
            "ServiceTag": "ESIM"

        }
        response = await self.__do_request(method="POST", path=EsimHubEndpoint.API_CREATE_RESELLER_TOPUP,
                                           body=request_body,
                                           base_url=os.getenv("ESIM_HUB_BASE_URL2"))
        logger.info(response)
        if not "success" in response:
            logger.error("Failed to create reseller Hub: {}".format(response))
            return None
        return EsimHubOrderResponse.model_validate(response["data"])

    async def get_bundles_by_country(self, country_codes: List[str],
                                     currency_code: str = os.getenv("DEFAULT_CURRENCY", "EUR")) -> \
            List[BundleDTO]:
        params = {
            "CountryGuids": country_codes,
            "CurrencyCode": currency_code,
            "pageIndex": 1,
            "pageSize": 300,
            "SortBy": "PRICE_ASC",
        }
        response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_SEARCH_BUNDLES_BY_COUNTRY,
                                           params=params)
        if not "success" in response:
            raise EsimHubException(response)
        bundles = []
        for bundle in response["data"]["items"]:
            try:
                bundles.append(DtoMapper.to_bundle_dto(bundle=bundle, currency=currency_code))
            except Exception as e:
                logger.error(f"error while mapping bundle : {str(e)}")
                continue
        return bundles

    async def get_topup_related_bundles(self, bundle_code: str, order_id: str,
                                        currency_code: str = os.getenv("DEFAULT_CURRENCY")) -> List[BundleDTO]:
        params = {
            "orderId": order_id,
            "CurrencyCode": currency_code,
            "pageIndex": 1,
            "pageSize": 300,
            "SortBy": "PRICE_ASC",
        }
        response = await self.__do_request(method="GET",
                                           path=EsimHubEndpoint.API_GET_TOPUP_RELATED_BUNDLES,
                                           base_url=os.getenv("ESIM_HUB_BASE_URL2"), params=params)
        if not "success" in response:
            raise EsimHubException(response)
        bundles = []
        for bundle in response["data"]["items"]:
            bundles.append(DtoMapper.to_bundle_dto(bundle=bundle, currency=currency_code))
        return bundles

    async def get_bundle_by_id(self, bundle_id: str, currency_code: str = os.getenv("DEFAULT_CURRENCY")) -> BundleDTO:
        params = {
            "RecordGuid": bundle_id,
            "CurrencyCode": currency_code
        }
        response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_GET_BUNDLE_BY_ID, params=params)
        if not "success" in response:
            raise EsimHubException(response)
        data = response["data"]["item"]
        return DtoMapper.to_bundle_dto(bundle=data, currency=currency_code)

    async def get_bundle_consumption(self, order_id: str) -> ConsumptionResponse:
        params = {
            "orderId": order_id
        }
        response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_GET_BUNDLE_CONSUMPTION,
                                           base_url=os.getenv("ESIM_HUB_BASE_URL2"), params=params)
        logger.debug(f"get bundle consumption: {response}")
        if not "success" in response:
            raise EsimHubException(response)
        return DtoMapper.to_consumption_response(dict(response["data"]))

    async def get_global_configurations(self) -> List[GlobalConfigurationResponse]:
        params = {
            "Keys": "CATALOG.BUNDLES_CACHE_VERSION"
        }
        try:
            response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_GET_GLOBAL_CONFIGURATIONS,
                                               params=params)
            if not "success" in response:
                logger.error("Failed to get global configurations: {}".format(response))
                return []

            logger.debug(response["data"]["globalConfigurations"])
            return [GlobalConfigurationResponse.model_validate(data) for data in
                    response["data"]["globalConfigurations"]]
        except Exception as e:
            logger.error("Failed to get global configurations: {}".format(e))
            raise EsimHubException(str(e))

    async def get_content_tag(self,
                              tag: Literal["TERM_CONDITION", "ABOUT_US", "ADS", "FAQ", "PRIVACY_POLICY"],
                              lang_code: str = "en") -> ContentResponse:
        body_request = {
            "Tag": tag
        }
        try:
            response = await self.__do_request(method="POST", path=EsimHubEndpoint.API_GET_CONTENT_TAG,
                                               body=body_request, headers={"LanguageCode": lang_code})
            logger.debug("success" in response)
            if not "success" in response:
                raise EsimHubException(response)
            return ContentResponse.model_validate(response["data"]["item"])
        except Exception as e:
            logger.error("Failed to get content tag: {}".format(e))
            raise EsimHubException(e)

    async def get_content_tags(self,
                               tag: Literal["TERM_CONDITION", "ABOUT_US", "ADS", "FAQ", "PRIVACY_POLICY"],
                               lang_code: str = "en") -> List[ContentResponse]:
        body_request = {
            "Tag": tag
        }
        try:
            response = await self.__do_request(method="POST", path=EsimHubEndpoint.API_GET_CONTENT_TAGS,
                                               body=body_request, headers={"LanguageCode": lang_code})
            logger.debug(response)
            if not "success" in response:
                raise EsimHubException(response)
            return [ContentResponse.model_validate(item) for item in response["data"]["items"]]
        except Exception as e:
            logger.error("Failed to get content tag: {}".format(e))
            raise EsimHubException(e)

    async def __do_request(self,
                           method: Literal["GET", "OPTIONS", "HEAD", "POST", "PUT", "PATCH", "DELETE"], path: str,
                           headers: Optional[Dict[str, str]] = None,
                           params: Optional[Dict[str, str]] | Optional[Dict[str, List[str]]] = None,
                           body: Optional[Any] = None,
                           base_url=None) -> Union[Dict[str, Any], List[Any], EsimHubException]:
        if headers is None:
            headers = {}
        if base_url is None:
            base_url = self.__base_url
        try:
            with httpx.Client() as client:
                headers["Tenant"] = self.__tenant_key
                headers["Content-Type"] = "application/json"
                headers["Accept"] = "application/json"
                headers["Api-Key"] = self.__api_key
                response = client.request(method=method, url=base_url + path, headers=headers, params=params,
                                          json=body, timeout=120)
                logger.debug("Request: curl -X {} {} {} Response: {}".format(method, response.url, " ".join(
                    [f'--header "{key}: {value}"' for key, value in headers.items()]), response))
                if response.status_code != httpx.codes.OK:
                    try:
                        json_response = response.json()
                        raise EsimHubException(
                            json_response["message"] if "message" in json_response else str(json_response))
                    except Exception as e:
                        raise EsimHubException(f"eSIM Hub API request failed: {response.status_code}")
                return response.json()
        except Exception as e:
            if type(e).__name__ == "CustomException":
                raise e
            raise EsimHubException(str(e))

    async def check_bundle_applicable(self, bundle_id: str) -> bool:
        try:
            params = {
                "bundleCode": bundle_id
            }
            response = await self.__do_request(method="GET", path=EsimHubEndpoint.API_CHECK_BUNDLE_APPLICABLE,
                                               params=params, base_url=os.getenv("ESIM_HUB_BASE_URL2"))
            if not "success" in response:
                return False
            return True
        except Exception as e:
            logger.error("Failed to check bundle applicable: {}".format(e))
            return False
