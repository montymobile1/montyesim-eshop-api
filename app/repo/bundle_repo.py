from typing import List

from app.config.db import DatabaseTables
from app.models.app import BundleModel, BundleTranslationModel
from app.repo.base_repo import BaseRepository
from app.schemas.home import BundleDTO


class BundleRepo(BaseRepository):

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_BUNDLE, BundleModel)

    def get_bundle_by_id(self, bundle_id: str) -> BundleDTO:
        bundle_model = super().get_by_id(record_id=bundle_id)
        return BundleDTO.model_validate(bundle_model.data)

    def get_bundles_by_tag(self, tag_id: str, locale: str = "en") -> List[BundleModel]:
        return super().select_procedure(where={"p_tag_id": tag_id, "p_locale": locale},
                                        function_name="get_bundles_for_tag_translated")

    def get_bundles_by_tags(self, tag_ids: str, locale: str = "en") -> List[BundleModel]:
        return super().select_procedure(where={"p_tags_ids": tag_ids, "p_locale": locale}, function_name="get_bundles_for_tags_translated")


class BundleTranslationRepo(BaseRepository):

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_BUNDLE_TRANSLATION, BundleTranslationModel)
