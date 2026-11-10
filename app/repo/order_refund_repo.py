from typing import Optional

from app.config.db import DatabaseTables
from app.exceptions import DatabaseException
from app.models.order_refund import UserOrderRefundModel
from app.repo.base_repo import BaseRepository


class OrderRefundRepo(BaseRepository):
    """`user_order_refund`: at most one row per order (primary key `order_id`).

    Every state change is a single conditional statement, so webhook redelivery, retries or several
    workers can never refund the same order twice.
    """

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_ORDER_REFUND, UserOrderRefundModel)

    def insert_if_absent(self, data: dict) -> Optional[UserOrderRefundModel]:
        """INSERT ... ON CONFLICT (order_id) DO NOTHING. Returns the new row, or None when the order
        already has one (someone else claimed it first)."""
        try:
            response = self.table.upsert(data, on_conflict="order_id", ignore_duplicates=True).execute()
            return self.model(**response.data[0]) if response.data else None
        except Exception as e:
            raise DatabaseException(str(e))

    def transition(self, order_id: str, from_status: str, data: dict) -> Optional[UserOrderRefundModel]:
        """UPDATE ... WHERE order_id = ? AND status = from_status. Returns the updated row, or None when
        the row was not in `from_status` any more."""
        try:
            response = self.table.update(data).eq("order_id", order_id).eq("status", from_status).execute()
            return self.model(**response.data[0]) if response.data else None
        except Exception as e:
            raise DatabaseException(str(e))

    def get_by_order_id(self, order_id: str) -> Optional[UserOrderRefundModel]:
        try:
            response = self.table.select("*").eq("order_id", order_id).limit(1).execute()
            return self.model(**response.data[0]) if response.data else None
        except Exception as e:
            raise DatabaseException(str(e))
