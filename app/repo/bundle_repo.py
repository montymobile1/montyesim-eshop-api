from app.config.db import DatabaseTables
from app.models.app import BundleModel
from app.repo.base_repo import BaseRepository
from app.schemas.home import BundleDTO


class BundleRepo(BaseRepository):

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_BUNDLE, BundleModel)

    def get_bundle_by_id(self, bundle_id: str) -> BundleDTO:
        bundle_model = super().get_by_id(record_id=bundle_id)
        return BundleDTO.model_validate(bundle_model.data)
