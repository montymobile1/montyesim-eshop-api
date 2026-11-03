"""Shapes of /api/v1/admin/groups (MS-5085): tag groups and their tags.

Create and edit are multipart/form-data: one ``payload`` field holding the JSON body below, plus
one file per icon named ``icon_<client_key>`` (new tags) or ``icon_<tag id>`` (existing tags).
Any other multipart field is 422.
"""
import re
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

GroupStatus = Literal["all", "active", "inactive"]
#: tag_group.type: 1 = group (several tags), 2 = flat (one tag named after the group).
GroupType = Literal[1, 2]
#: tag_group.group_category: the two values the portal offers (TEOS-184; every group on qa uses one).
GroupCategory = Literal["on_land", "at_sea"]

#: tag_group.name is checked by the database as length < 30 (supabase_ddl.sql).
GROUP_NAME_MAX = 29
#: Tag names are 1 to 60 characters (story TEOS-125; TEOS-185). The tag.name column allows 300.
TAG_NAME_MAX = 60
CLIENT_KEY_PATTERN = r"^[A-Za-z0-9_-]{1,50}$"
UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
#: Multipart fields accepted on create and edit: ``payload`` and the icon files.
PAYLOAD_FIELD = "payload"
ICON_FIELD_PREFIX = "icon_"
MAX_TAGS = 500
#: At most 200 icon files in one create or edit, 413 above (story TEOS-125 case 15; TEOS-187).
MAX_ICON_FILES = 200

_uuid = re.compile(UUID_PATTERN)


def _clean_name(value: str, max_length: int, label: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{label}: must not be blank")
    if len(text) > max_length:
        raise ValueError(f"{label}: at most {max_length} characters")
    return text


def normalize_alternative_country(value: Union[str, List[str], None]) -> Optional[str]:
    """Search keywords as the comma-separated string stored in ``tag.data.alternative_country``
    (a list or a string is accepted); ``None`` when empty. Trimmed, empty values dropped and
    duplicates removed ignoring case, first spelling kept (TEOS-182: " Beirut ,beirut,LB" -> "Beirut,LB")."""
    if value is None:
        return None
    parts = value if isinstance(value, list) else str(value).split(",")
    cleaned: Dict[str, str] = {}
    for part in parts:
        text = str(part).strip()
        if text:
            cleaned.setdefault(text.casefold(), text)
    return ",".join(cleaned.values()) or None


class AdminGroup(BaseModel):
    """A tag group as the list returns it (``tag_count`` = number of its tags, ``bundle_count`` = number
    of distinct bundles tagged with any of its tags, TEOS-173)."""
    id: int
    name: Optional[str] = None
    type: Optional[int] = None
    group_category: Optional[str] = None
    is_active: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    tag_count: int = 0
    bundle_count: int = 0


class AdminTag(BaseModel):
    id: str
    tag_group_id: Optional[int] = None
    name: Optional[str] = None
    icon: Optional[str] = None
    sorting_number: Optional[int] = None
    data: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AdminGroupDetail(AdminGroup):
    """A group with its tags ordered by ``sorting_number`` then ``name``. ``top_countries_count`` is the
    ``TOP_COUNTRIES_COUNT`` setting, given for the Countries group (the reorder page shows it)."""
    tags: List[AdminTag] = Field(default_factory=list)
    top_countries_count: Optional[int] = None


class AdminGroupTagInput(BaseModel):
    """A tag to create. ``client_key`` names its icon file in the multipart body (``icon_<client_key>``)."""
    client_key: str = Field(..., pattern=CLIENT_KEY_PATTERN)
    name: str = Field(..., min_length=1, max_length=TAG_NAME_MAX)
    alternative_country: Optional[Union[List[str], str]] = None
    data: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _clean_name(value, TAG_NAME_MAX, "tags.name")


class AdminGroupUpdatedTag(BaseModel):
    """An existing tag of the group. ``remove_icon`` clears the icon (unless a new file ``icon_<id>`` is sent)."""
    id: str = Field(..., pattern=UUID_PATTERN)
    name: str = Field(..., min_length=1, max_length=TAG_NAME_MAX)
    remove_icon: bool = False
    alternative_country: Optional[Union[List[str], str]] = None
    data: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _clean_name(value, TAG_NAME_MAX, "updated_tags.name")


class _GroupFields(BaseModel):
    name: str = Field(..., min_length=1, max_length=GROUP_NAME_MAX)
    type: GroupType
    group_category: GroupCategory

    model_config = ConfigDict(extra="forbid")

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _clean_name(value, GROUP_NAME_MAX, "name")


class AdminGroupCreate(_GroupFields):
    """``payload`` of POST /groups. At least one tag (the portal always sends one)."""
    tags: List[AdminGroupTagInput] = Field(..., min_length=1, max_length=MAX_TAGS)

    @field_validator("tags")
    @classmethod
    def _unique_keys(cls, tags: List[AdminGroupTagInput]) -> List[AdminGroupTagInput]:
        keys = [tag.client_key for tag in tags]
        if len(set(keys)) != len(keys):
            raise ValueError("tags: client_key values must be unique")
        return tags


class AdminGroupUpdate(_GroupFields):
    """``payload`` of PUT /groups/{id}: the group fields plus the tag changes."""
    new_tags: List[AdminGroupTagInput] = Field(default_factory=list, max_length=MAX_TAGS)
    updated_tags: List[AdminGroupUpdatedTag] = Field(default_factory=list, max_length=MAX_TAGS)
    deleted_tag_ids: List[str] = Field(default_factory=list, max_length=MAX_TAGS)

    @field_validator("new_tags")
    @classmethod
    def _unique_keys(cls, tags: List[AdminGroupTagInput]) -> List[AdminGroupTagInput]:
        keys = [tag.client_key for tag in tags]
        if len(set(keys)) != len(keys):
            raise ValueError("new_tags: client_key values must be unique")
        return tags

    @field_validator("deleted_tag_ids")
    @classmethod
    def _uuids(cls, ids: List[str]) -> List[str]:
        for tag_id in ids:
            if not _uuid.match(tag_id or ""):
                raise ValueError(f"deleted_tag_ids: {tag_id!r} is not a uuid")
        if len(set(ids)) != len(ids):
            raise ValueError("deleted_tag_ids: duplicated id")
        return ids


class AdminTagOrder(BaseModel):
    """Body of PUT /groups/{id}/tags/order: every tag of the group, in the wanted order. ``[]`` is accepted
    for a group without tags (200, nothing written); on a group with tags it is 422 incomplete_order (TEOS-198)."""
    tag_ids: List[str] = Field(..., min_length=0, max_length=MAX_TAGS)

    model_config = ConfigDict(extra="forbid")

    @field_validator("tag_ids")
    @classmethod
    def _uuids(cls, ids: List[str]) -> List[str]:
        for tag_id in ids:
            if not _uuid.match(tag_id or ""):
                raise ValueError(f"tag_ids: {tag_id!r} is not a uuid")
        if len(set(ids)) != len(ids):
            raise ValueError("tag_ids: duplicated id")
        return ids


class AdminTagOrderResult(BaseModel):
    tag_ids: List[str]


class AdminGroupStatus(BaseModel):
    """Result of PATCH /groups/{id}/status."""
    id: int
    is_active: bool
