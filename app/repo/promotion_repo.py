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

    # MT OSTE-932 select and update function
    def select_and_update_usage(self, user_id: str, promotion_code: str, status: str):
        return super().select_procedure(
            where={"p_user_id": user_id, "p_promotion_code": promotion_code, "p_status": status},
            function_name="promotion_usage_select_and_update")
