from typing import Optional

from pydantic import BaseModel


class ConsumptionLimitRequest(BaseModel):
    order_id: str
    iccid: str
    event_type: Optional[str]
    event_date: Optional[str]
