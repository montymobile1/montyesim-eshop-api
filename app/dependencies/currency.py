from fastapi import Header
from typing import Optional

async def x_currency_header(
    x_currency_header: Optional[str] = Header(
        None,
        description="Currency preference (e.g., USD, EUR)",
        example="USD"
    )
) -> Optional[str]:
    return x_currency_header

