from app.models.bundle_tag import BundleTagModel

from app.repo.base_repo import BaseRepository


class BundleTagRepo(BaseRepository):

    def __init__(self):
        super().__init__(BundleTagModel)
