from app.config.db import DatabaseTables
from app.models.promotion import PromotionRuleModel, PromotionModel, PromotionUsageModel
from app.repo.base_repo import BaseRepository


class PromotionRuleRepo(BaseRepository):

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_PROMOTION_RULE, PromotionRuleModel)


class PromotionRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_PROMOTION, PromotionModel)


class PromotionUsageRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_PROMOTION_USAGE, PromotionUsageModel)
