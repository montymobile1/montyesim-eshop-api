"""Shapes of /api/v1/admin/banners (MS-5058, MS-5060).

Create and edit are multipart/form-data (title, description, platform, action?, is_active?, image),
so their fields are declared as ``Form``/``File`` parameters on the routes; the limits live here.
"""
from typing import Literal, Optional

from pydantic import BaseModel

BannerStatus = Literal["all", "active", "inactive"]
BannerPlatform = Literal["web", "mobile"]

TITLE_MAX = 100
DESCRIPTION_MAX = 500
#: Actions are app constants such as REFER_NOW, CHAT, CASHBACK (portal EnumData.actionData).
ACTION_PATTERN = r"^[A-Za-z0-9_]{1,50}$"
#: The only multipart fields accepted on create and edit; anything else is 422.
BANNER_FORM_FIELDS = frozenset({"title", "description", "platform", "action", "is_active", "image"})


class AdminBanner(BaseModel):
    """A banner as the admin API returns it. ``image_url`` is the stored public URL of the image."""
    id: int
    title: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    action: Optional[str] = None
    platform: Optional[str] = None
    is_active: bool = True
    created_at: Optional[str] = None
#: ``is_active`` is read as text: FastAPI turns a missing boolean form field into False, which
#: would deactivate a banner whenever the field is left out.
IS_ACTIVE_PATTERN = r"^(true|false|True|False)$"
