from typing import Optional

from pydantic import BaseModel

class NotificationModel(BaseModel):
    id: Optional[int] = None
    title: str
    content: str
    status: Optional[bool] = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    data: Optional[str] = None
    user_id: Optional[str] = None
    image_url: Optional[str] = None