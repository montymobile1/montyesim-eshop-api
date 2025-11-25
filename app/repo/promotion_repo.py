from app.models.promotion import PromotionModel
from app.models.promotion_rule import PromotionRuleModel
from app.models.promotion_usage import PromotionUsageModel
from app.repo.base_repo import BaseRepository


class PromotionRuleRepo(BaseRepository):

    def __init__(self):
        super().__init__(PromotionRuleModel)


class PromotionRepo(BaseRepository):
    def __init__(self):
        super().__init__(PromotionModel)


class PromotionUsageRepo(BaseRepository):
    def __init__(self):
        super().__init__(PromotionUsageModel)
