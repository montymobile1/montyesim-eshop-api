from pydantic import BaseModel

class VoucherRequestRedeem(BaseModel):
    code: str
