from typing import List

from app.config.db import DatabaseTables
from app.models.app import TagModel
from app.repo.bundle_repo import BundleRepo
from app.repo.bundle_tage_repo import BundleTagRepo
from app.repo.tag_group_repo import tagGroupRepo
from app.repo.tag_repo import TagRepo, TagTranslationRepo
from app.schemas.dto_mapper import DtoMapper
from app.schemas.home import CountryDTO, RegionDTO, BundleDTO
from deep_translator import GoogleTranslator


class GroupingService:
    def __init__(self):
        self.__tag_group_repo = tagGroupRepo()
        self.__tag_repo = TagRepo()
        self.__bundle_tag_repo = BundleTagRepo()
        self.__bundle_repo = BundleRepo()
        self.__tag_translation_repo = TagTranslationRepo()

    async def __get_all_tags_by_group_id(self, group_id) -> List[TagModel]:
        tags = self.__tag_repo.list(where={"tag_group_id": group_id})
        return tags

    async def __get_all_tags_by_group_id_with_language(self, group_id :int,locale :str ='en') -> List[TagModel]:
        tags = self.__tag_repo.select_procedure(function_name = "get_translated_tag_by_tag_group_id",where ={"tag_group_id_param": group_id,"locale_param":locale})
        # tags = self.__tag_repo.list(where={"tag_group_id": group_id})
        return tags

    async def get_all_countries(self,locale :str) -> List[CountryDTO]:
        tags = await self.__get_all_tags_by_group_id_with_language(group_id=1,locale=locale)
        tags = sorted(tags, key=lambda tag: tag.name)
        for tag in tags:
            tag.data["country"] = tag.name
        return [CountryDTO.model_validate(tag.data) for tag in tags]

    async def get_all_regions(self,locale :str) -> List[RegionDTO]:
        tags = await self.__get_all_tags_by_group_id_with_language(group_id=2,locale=locale)
        for tag in tags:
            tag.data["region_name"] = tag.name
        return [RegionDTO.model_validate(tag.data) for tag in tags]

    async def get_cruise_bundle(self,rate :float,currency_name : str,locale :str) -> List[BundleDTO]:
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
                    tags_id = [bundle_country.id for  bundle_country in bundle_dto.countries]
                    country_tags = self.__tag_repo.select_procedure(function_name="get_translated_tag_by_tag_id_list",
                                                            where={"tag_ids": tags_id,
                                                                   "locale_param": locale})
                    for country_tag in country_tags:
                        country_tag.data["country"] = country_tag.name
                    countries = [tag.data for  tag in country_tags]
                    bundle_dto.countries = countries
                    bundles.append(DtoMapper.bundle_currency_update(bundle_dto, currency_name, rate))

        return bundles

    async def get_global_bundle(self,rate: float, currency_name : str,locale :str) -> List[BundleDTO]:
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
                    tags_id = [bundle_country.id for  bundle_country in bundle_dto.countries]
                    country_tags = self.__tag_repo.select_procedure(function_name="get_translated_tag_by_tag_id_list",
                                                            where={"tag_ids": tags_id,
                                                                   "locale_param": locale})
                    for country_tag in country_tags:
                        country_tag.data["country"] = country_tag.name
                    countries = [tag.data for  tag in country_tags]
                    bundle_dto.countries = countries
                    bundles.append(DtoMapper.bundle_currency_update(bundle_dto, currency_name, rate))
        return bundles

    async def translate_tags(self,locale :str):
        tags = self.__tag_repo.list(where={})
        for tag in tags:
            translated = GoogleTranslator(source='en', target=locale).translate(tag.name)
            print(tag)
            data = {
                "tag_id" : tag.id,
                "locale" : locale,
                "name" : translated,
                "data" : tag.data
            }
            print(data)
            self.__tag_translation_repo.create(data)