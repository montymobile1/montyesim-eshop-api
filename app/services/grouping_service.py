from typing import List

from app.models.app import TagModel
from app.repo.bundle_repo import BundleRepo
from app.repo.bundle_tage_repo import BundleTagRepo
from app.repo.tag_group_repo import tagGroupRepo
from app.repo.tag_repo import TagRepo
from app.schemas.dto_mapper import DtoMapper
from app.schemas.home import CountryDTO, RegionDTO, BundleDTO


class GroupingService:
    def __init__(self):
        self.__tag_group_repo = tagGroupRepo()
        self.__tag_repo = TagRepo()
        self.__bundle_tag_repo = BundleTagRepo()
        self.__bundle_repo = BundleRepo()

    async def __get_all_tags_by_group_id(self, group_id) -> List[TagModel]:
        tags = self.__tag_repo.list(where={"tag_group_id": group_id})
        return tags

    async def get_all_countries(self) -> List[CountryDTO]:
        tags = await self.__get_all_tags_by_group_id(group_id=1)
        tags = sorted(tags, key=lambda tag: tag.name)
        return [CountryDTO.model_validate(tag.data) for tag in tags]

    async def get_all_regions(self) -> List[RegionDTO]:
        tags = await self.__get_all_tags_by_group_id(group_id=2)
        return [RegionDTO.model_validate(tag.data) for tag in tags]

    async def get_cruise_bundle(self,rate :float,currency_name : str) -> List[BundleDTO]:
        tags = await self.__get_all_tags_by_group_id(group_id=3)

        tag_id = tags[0].id if tags else 0
        bundles: List[BundleDTO] = []

        if tag_id:
            bundler_tags = self.__bundle_tag_repo.list(
                where={"tag_id": tag_id}
            )

            for bundle_tag in bundler_tags:
                # Assuming bundle_tag has `bundle_id` field (not `id`)
                bundle = self.__bundle_repo.get_by_id(record_id=bundle_tag.bundle_id)

                if bundle and bundle.data:
                    bundle_dto = BundleDTO(**bundle.data)
                    bundles.append(DtoMapper.bundle_currency_update(bundle_dto, currency_name, rate))

        return bundles

    async def get_global_bundle(self,rate: float, currency_name : str) -> List[BundleDTO]:
        tags = await self.__get_all_tags_by_group_id(group_id=4)
        if not tags:
            return []

        tag_id = tags[0].id if tags else 0

        bundles: List[BundleDTO] = []

        if tag_id:
            bundler_tags = self.__bundle_tag_repo.list(
                where={"tag_id": tag_id}
            )

            for bundle_tag in bundler_tags:
                # Assuming bundle_tag has `bundle_id` field (not `id`)
                bundle = self.__bundle_repo.get_by_id(record_id=bundle_tag.bundle_id)

                if bundle and bundle.data:
                    bundle_dto = BundleDTO(**bundle.data)
                    bundles.append(DtoMapper.bundle_currency_update(bundle_dto, currency_name, rate))
        return bundles
