"""Request and response shapes of /api/v1/admin/bundles (MS-5083)."""
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.admin.common import AdminStatusPatch

BundleStatus = Literal["all", "active", "inactive"]

#: ``bundle.bundle_name`` is the display title shown in the app; the table check allows 60 characters.
MAX_BUNDLE_NAME = 60
#: Largest tag set accepted by ``PUT /bundles/{id}/tags`` (a bundle has a handful of tags in practice).
MAX_TAG_IDS = 500


class AdminBundle(BaseModel):
    """One row of the bundles list: the stored row as it is, ``data`` being the synced bundle JSON."""
    id: str
    bundle_name: Optional[str] = None
    is_active: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    data: Optional[Dict[str, Any]] = None


class AdminBundleTagGroup(BaseModel):
    id: int
    name: Optional[str] = None
    type: Optional[int] = None
    group_category: Optional[str] = None


class AdminBundleTagItem(BaseModel):
    id: str
    name: Optional[str] = None
    icon: Optional[str] = None
    group: Optional[AdminBundleTagGroup] = None


class AdminBundleTag(BaseModel):
    """One ``bundle_tag`` link with its tag and the tag's group (the assign page builds its groups from it)."""
    bundle_tag_id: int
    tag: AdminBundleTagItem


class AdminBundleDetail(AdminBundle):
    """``GET /bundles/{id}``: the bundle plus its tags."""
    tags: List[AdminBundleTag] = Field(default_factory=list)


class AdminBundleRename(BaseModel):
    """Body of ``PATCH /bundles/{id}``: the display name, 1 to 60 characters, not only spaces."""
    bundle_name: str = Field(..., min_length=1, max_length=MAX_BUNDLE_NAME)

    model_config = ConfigDict(extra="forbid")

    @field_validator("bundle_name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("bundle_name cannot be only spaces")
        return stripped


class AdminBundleRenamed(BaseModel):
    id: str
    bundle_name: Optional[str] = None


def _reason_required_when_deactivating(is_active: bool, reason: Optional[str]) -> None:
    if not is_active and not (reason or "").strip():
        raise ValueError("reason is required when deactivating")


class AdminBundleStatusPatch(AdminStatusPatch):
    """``PATCH /bundles/{id}/status``: the standard status body, the reason being required to deactivate."""

    @model_validator(mode="after")
    def _check_reason(self):
        _reason_required_when_deactivating(self.is_active, self.reason)
        return self


class AdminBundleStatus(BaseModel):
    id: str
    is_active: bool


class AdminBundleBulkStatus(BaseModel):
    """Body of ``POST /bundles/status/bulk``: every bundle goes to ``is_active``; a reason is required
    to deactivate (optional to activate), as in the portal dialogs."""
    is_active: bool
    reason: Optional[str] = Field(None, min_length=1, max_length=500)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _check_reason(self):
        _reason_required_when_deactivating(self.is_active, self.reason)
        return self


class AdminBundleBulkResult(BaseModel):
    rows_affected: int
    affected_bundle_ids: List[str] = Field(default_factory=list)


class AdminBundleTagsSet(BaseModel):
    """Body of ``PUT /bundles/{id}/tags``: the full tag set. An empty list removes every tag (MS-5065)."""
    tag_ids: List[UUID] = Field(..., max_length=MAX_TAG_IDS)

    model_config = ConfigDict(extra="forbid")


class AdminBundleTags(BaseModel):
    id: str
    tag_ids: List[str] = Field(default_factory=list)
