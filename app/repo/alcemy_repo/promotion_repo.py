from app.models.declare_models.promotion_rule import PromotionRule  # SQLAlchemy ORM model
from app.models.declare_models.promotion import Promotion           # SQLAlchemy ORM model
from app.models.declare_models.promotion_usage import PromotionUsage  # SQLAlchemy ORM model
from app.repo.alcemy_repo.base_repo import BaseRepository


class PromotionRuleRepo(BaseRepository):
    def __init__(self):
        super().__init__(PromotionRule)


class PromotionRepo(BaseRepository):
    def __init__(self):
        super().__init__(Promotion)


class PromotionUsageRepo(BaseRepository):
    def __init__(self):
        super().__init__(PromotionUsage)
