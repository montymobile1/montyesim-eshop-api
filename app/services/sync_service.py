import math
import os
import uuid
from typing import List

from loguru import logger

from app.config.db import ConfigKeysEnum
from app.models.app import TagModel, BundleModel, BundleTagModel
from app.repo.bundle_repo import BundleRepo
from app.repo.bundle_tage_repo import BundleTagRepo
from app.repo.config_repo import ConfigRepo
from app.repo.tag_repo import TagRepo
from app.schemas.home import CountryDTO, RegionDTO, BundleDTO
from app.services.integration.esim_hub_service import EsimHubService


class SyncService:

    def __init__(self):
        self.__esim_hub_service = EsimHubService(
            base_url=os.getenv("ESIM_HUB_BASE_URL"),
            api_key=os.getenv("ESIM_HUB_API_KEY"),
            tenant_key=os.getenv("ESIM_HUB_TENANT_KEY"),
        )
        self.__bundle_repo = BundleRepo()
        self.__tag_repo = TagRepo()
        self.__bundle_tag_repo = BundleTagRepo()
        self.__config_repo = ConfigRepo()

    async def sync_bundles(self, page_index=1):
        logger.info(f"Syncing bundles started")
        page_size = 100
        all_bundle_response = await self.__esim_hub_service.get_all_bundles(page_index=page_index, page_size=page_size)
        all_bundles_count = all_bundle_response.total_rows
        pages = math.ceil(all_bundles_count / page_size)
        logger.info(f"all bundle count: {all_bundles_count}, pages: {pages}")
        for index, bundle in enumerate(all_bundle_response.bundles):
            logger.info("Syncing bundle {}".format(index + 1))
            await self.sync_bundle(bundle)
        for page in range(2, pages + 1):
            all_bundle_response = await self.__esim_hub_service.get_all_bundles(page_index=page, page_size=page_size)
            logger.info(f"Syncing bundle page: {page} of size: {pages}")
            for index, bundle in enumerate(all_bundle_response.bundles):
                logger.info("Syncing bundle {}".format(index + 1))
                await self.sync_bundle(bundle)
        logger.info(f"Syncing bundles finished")

    async def sync_bundle(self, bundle: BundleDTO):
        try:
            countries = bundle.countries
            await self.__sync_country_tags(countries)
            regions = bundle.bundle_region
            regions = list(filter(lambda r: r.region_code != "GLOBAL", regions))
            await self.__sync_region_tags(regions)
            if not self.__bundle_repo.get_by_id(bundle.bundle_code):
                self.__bundle_repo.create(BundleModel(id=bundle.bundle_code, is_active=True,
                                                      data=bundle.model_dump(
                                                          exclude={"updated_at", "created_at", "id"})).model_dump(
                    exclude={"updated_at", "created_at"}))
                for country in countries:
                    self.__bundle_tag_repo.create(
                        BundleTagModel(bundle_id=bundle.bundle_code, tag_id=country.id, id=None).model_dump(
                            exclude={"updated_at", "created_at", "id"}))
                for region in regions:
                    tag = self.__tag_repo.get_first_by({"name": region.region_name})
                    self.__bundle_tag_repo.create(
                        BundleTagModel(bundle_id=bundle.bundle_code, tag_id=tag.id, id=None).model_dump(
                            exclude={"updated_at", "created_at", "id"}))
                    logger.debug("adding region for bundle {}".format(bundle.bundle_code))
            else:
                logger.info(f"bundle already added, updating it {bundle.bundle_code}")
                self.__bundle_repo.update(record_id=bundle.bundle_code,
                                          data=BundleModel(id=bundle.bundle_code, is_active=True,
                                                           data=bundle.model_dump()).model_dump(
                                              exclude={"updated_at", "created_at", "id"}))
                for country in countries:
                    if not self.__bundle_tag_repo.get_first_by({"bundle_id": bundle.bundle_code, "tag_id": country.id}):
                        self.__bundle_tag_repo.create(
                            BundleTagModel(bundle_id=bundle.bundle_code, tag_id=country.id, id=None).model_dump(
                                exclude={"updated_at", "created_at", "id"}))
                for region in regions:
                    if not self.__bundle_tag_repo.get_first_by(
                            {"bundle_id": bundle.bundle_code, "tag_id": region.guid}):
                        self.__bundle_tag_repo.create(
                            BundleTagModel(bundle_id=bundle.bundle_code, tag_id=region.guid, id=None).model_dump(
                                exclude={"updated_at", "created_at", "id"}))
                        logger.debug("updating region for bundle {}".format(bundle.bundle_code))
        except Exception as e:
            logger.error(e)

    async def update_sync_version(self):
        new_key = uuid.uuid4().hex
        old_config = self.__config_repo.get_first_by({"key": ConfigKeysEnum.APP_CACHE_KEY})
        if not old_config:
            self.__config_repo.create({"key": ConfigKeysEnum.APP_CACHE_KEY, "value": new_key})
        else:
            self.__config_repo.update_by(where={"key": ConfigKeysEnum.APP_CACHE_KEY}, data={"value": new_key})

    async def delete_bundle(self, bundle_id: str):
        try:
            self.__bundle_tag_repo.delete_by({"bundle_id": bundle_id})
            self.__bundle_repo.delete(record_id=bundle_id)
            logger.info(f"deleted bundle {bundle_id}")
        except Exception as e:
            logger.error(f"error while deleting bundle {bundle_id=} {e}")

    async def __sync_country_tags(self, countries: List[CountryDTO]):
        for country in countries:
            if not self.__tag_repo.get_first_by({"name": country.country}):
                self.__tag_repo.create(
                    TagModel(name=country.country, icon=country.icon, tag_group_id=1, data=country.model_dump(),
                             id=country.id).model_dump(
                        exclude={"updated_at", "created_at"}))

    async def __sync_region_tags(self, regions: List[RegionDTO]):
        for region in regions:
            if region.region_code == "GLOBAL":
                continue
            if not self.__tag_repo.get_first_by({"name": region.region_name}):
                self.__tag_repo.create(
                    TagModel(name=region.region_name, icon=region.icon, tag_group_id=2, data=region.model_dump(),
                             id=region.guid).model_dump(
                        exclude={"updated_at", "created_at"}))
