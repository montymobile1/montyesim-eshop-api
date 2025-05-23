from typing import Optional

from pydantic import BaseModel
from app.config.db import NotificationCategoryType


class ConsumptionLimitRequest(BaseModel):
    order_id: str
    iccid: str
    event_type: Optional[str]
    event_date: Optional[str]