from typing import Optional

from pydantic import BaseModel


class UserModel(BaseModel):
    id: str
    email: str
    token: str
    msisdn: Optional[str]
    is_verified: bool
    language: Optional[str] = "en"
    is_anonymous: Optional[bool] = False
    anonymous_user_id: Optional[str] = None
