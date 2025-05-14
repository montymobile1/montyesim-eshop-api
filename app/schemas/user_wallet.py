from pydantic import BaseModel


class UserWalletResponse(BaseModel):
    balance: float
    currency: str


class UserWalletRequestDto(BaseModel):
    user_id: str
    amount: float
    currency: str


class TopUpWalletRequest(BaseModel):
    amount: float
