import os
from typing import List

from deep_translator import GoogleTranslator
from loguru import logger

from app.models.app import TagModel
from app.repo.bundle_repo import BundleRepo, BundleTranslationRepo
from app.repo.bundle_tage_repo import BundleTagRepo
from app.repo.tag_repo import TagRepo, TagTranslationRepo
from app.schemas.dto_mapper import DtoMapper
from app.schemas.home import CountryDTO, RegionDTO, BundleDTO
from app.schemas.response import ResponseHelper
from app.services.task_executor import TaskExecutor


class GroupingService:
    def __init__(self):
        self.__tag_repo = TagRepo()
        self.__bundle_tag_repo = BundleTagRepo()
        self.__bundle_repo = BundleRepo()
        self.__bundle_translation_repo = BundleTranslationRepo()
        self.__tag_translation_repo = TagTranslationRepo()
        self.__task_executor = TaskExecutor()

    async def __get_all_tags_by_group_id(self, group_id) -> List[TagModel]:
        tags = self.__tag_repo.select_procedure(function_name="get_active_tag_names_and_data_by_group",
                                                where={"_tag_group_id": group_id})
        return tags

    async def __get_all_tags_by_group_id_with_language(self, group_id: int, locale: str = 'en') -> List[TagModel]:
        tags = self.__tag_repo.select_procedure(function_name="get_translated_tag_by_tag_group_id",
                                                where={"tag_group_id_param": group_id, "locale_param": locale})
        return tags

    async def get_all_countries(self, locale: str) -> List[CountryDTO]:
        if locale == os.getenv("DEFAULT_LOCALE", "en"):
            tags = await self.__get_all_tags_by_group_id(group_id=1)
        else:
            tags = await self.__get_all_tags_by_group_id_with_language(group_id=1, locale=locale)
        tags = sorted(tags, key=lambda tag: tag.name)
        for tag in tags:
            tag.data["country"] = tag.name
        return [CountryDTO.model_validate(tag.data) for tag in tags]

    async def get_all_regions(self, locale: str) -> List[RegionDTO]:
        if locale == os.getenv("DEFAULT_LOCALE", "en"):
            tags = await self.__get_all_tags_by_group_id(group_id=2)
        else:
            tags = await self.__get_all_tags_by_group_id_with_language(group_id=2, locale=locale)
        for tag in tags:
            tag.data["region_name"] = tag.name
        return [RegionDTO.model_validate(tag.data) for tag in tags]

    async def get_cruise_bundle(self, rate: float, currency_name: str, locale: str) -> List[BundleDTO]:
        tags = await self.__get_all_tags_by_group_id(group_id=3)

        tag_id = tags[0].id if tags else 0
        bundles: List[BundleDTO] = []

        if tag_id:
            bundler_tags = self.__bundle_tag_repo.list(
                where={"tag_id": tag_id}
            )

            for bundle_tag in bundler_tags:
                bundles_list = self.__bundle_repo.list(where={"id": bundle_tag.bundle_id, "is_active": True})
                if len(bundles_list) == 0:
                    continue
                bundle = bundles_list[0]
                if bundle and bundle.data:
                    bundle_dto = BundleDTO(**bundle.data)
                    if locale != os.getenv("DEFAULT_LOCALE", "en"):
                        tags_id = [bundle_country.id for bundle_country in bundle_dto.countries]
                        country_tags = self.__tag_repo.select_procedure(
                            function_name="get_translated_tag_by_tag_id_list",
                            where={"tag_ids": tags_id,
                                   "locale_param": locale})
                        for country_tag in country_tags:
                            country_tag.data["country"] = country_tag.name
                        countries = [tag.data for tag in country_tags]
                        bundle_dto.countries = countries
                    bundles.append(DtoMapper.bundle_currency_update(bundle_dto, currency_name, rate))

        return bundles

    async def get_global_bundle(self, rate: float, currency_name: str, locale: str) -> List[BundleDTO]:
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
                bundles_list = self.__bundle_repo.list(where={"id": bundle_tag.bundle_id, "is_active": True})
                if len(bundles_list) == 0:
                    continue
                bundle = bundles_list[0]
                if bundle and bundle.data:
                    bundle_dto = BundleDTO(**bundle.data)
                    if locale != os.getenv("DEFAULT_LOCALE", "en"):
                        tags_id = [bundle_country.id for bundle_country in bundle_dto.countries]
                        country_tags = self.__tag_repo.select_procedure(
                            function_name="get_translated_tag_by_tag_id_list",
                            where={"tag_ids": tags_id,
                                   "locale_param": locale})
                        for country_tag in country_tags:
                            country_tag.data["country"] = country_tag.name
                        countries = [tag.data for tag in country_tags]
                        bundle_dto.countries = countries
                    bundles.append(DtoMapper.bundle_currency_update(bundle_dto, currency_name, rate))
        return bundles

    async def translate_tags(self, locale: str):

        def task():
            return self.__translate_tags(locale=locale)

        self.__task_executor.add_task(task)
        return ResponseHelper.success_response()

    async def translate_bundles(self, locale: str):

        def task():
            return self.__translate_bundles(locale=locale)

        self.__task_executor.add_task(task)
        return ResponseHelper.success_response()

    def __translate_tags(self, locale: str):
        tags = self.__tag_repo.list(where={})
        for tag in tags:
            logger.trace(f"translating tag {tag.name} to locale {locale}")
            old_translation = self.__tag_translation_repo.get_first_by(where={"tag_id": tag.id, "locale": locale})
            if old_translation:
                continue
            translated = GoogleTranslator(source='en', target=locale).translate(tag.name)
            data = {
                "tag_id": tag.id,
                "locale": locale,
                "name": translated,
                "data": tag.data
            }
            self.__tag_translation_repo.create(data)

    def __translate_bundles(self, locale: str):
        bundles = self.__bundle_repo.list(where={})
        for bundle in bundles:
            old = self.__bundle_translation_repo.get_first_by(where={"id": bundle.id, "locale": locale})
            if old:
                continue
            logger.info(f"translating bundle {bundle.id} to locale {locale}")
            try:
                bundle_dto = BundleDTO(**bundle.data)
                countries = bundle_dto.countries
                translated_countries = []
                for country in countries:
                    country.country = GoogleTranslator(source='en', target=locale).translate(country.country)
                    translated_countries.append(country)
                bundle_dto.countries = translated_countries
                self.__bundle_translation_repo.create({
                    "id": bundle.id,
                    "data": bundle_dto.model_dump(),
                    "locale": locale
                })
            except Exception as e:
                logger.error(f"error translating bundle {bundle.id} to locale {locale} error: {e}")
