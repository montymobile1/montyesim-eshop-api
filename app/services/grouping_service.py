import os
from typing import List

from deep_translator import GoogleTranslator

from app.models import TagModel
from app.repo.bundle_repo import BundleRepo
from app.repo.bundle_tage_repo import BundleTagRepo
from app.repo.tag_repo import TagRepo, TagTranslationRepo
from app.schemas.dto_mapper import DtoMapper
from app.schemas.home import CountryDTO, RegionDTO, BundleDTO


class GroupingService:
    def __init__(self):
        self.__tag_repo = TagRepo()
        self.__bundle_tag_repo = BundleTagRepo()
        self.__bundle_repo = BundleRepo()
        self.__tag_translation_repo = TagTranslationRepo()

    async def __get_all_tags_by_group_id(self, group_id) -> List[TagModel]:
        tags = await self.__tag_repo.get_active_tag_names_and_data_by_group(group_id=group_id)
        return tags

    async def __get_all_tags_by_group_id_with_language(self, group_id: int, locale: str = 'en') -> List[TagModel]:
        tags = await self.__tag_repo.get_active_tag_names_and_data_by_group(group_id=group_id, locale=locale)
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
        bundle_models = await self.__bundle_repo.get_bundles_by_tag_group(tag_group_id=3, locale=locale)
        all_bundles = []
        for bundle_model in bundle_models:
            all_bundles.append(DtoMapper.bundle_currency_update(bundle_model, currency_name, rate))
        return all_bundles

    async def get_global_bundle(self, rate: float, currency_name: str, locale: str) -> List[BundleDTO]:
        bundle_models = await self.__bundle_repo.get_bundles_by_tag_group(tag_group_id=4, locale=locale)
        all_bundles = []
        for bundle_model in bundle_models:
            all_bundles.append(DtoMapper.bundle_currency_update(bundle_model, currency_name, rate))
        return all_bundles

    async def translate_tags(self, locale: str):
        tags = await self.__tag_repo.list(where={})
        for tag in tags:
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
            await self.__tag_translation_repo.create(data)
