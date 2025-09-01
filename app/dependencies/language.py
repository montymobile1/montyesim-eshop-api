from fastapi import Header
from typing import Optional

async def accept_language_header(
    accept_language: Optional[str] = Header(
        None,
        description="Language preference (en/ar)",
        example="en"
    )
) -> Optional[str]:
    return accept_language

