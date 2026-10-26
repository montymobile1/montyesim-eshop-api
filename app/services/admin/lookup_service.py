"""Dropdown sources of the admin portal (MS-5077): ``GET /<resource>/lookup``.

Each lookup returns one page of ``{id, label, ...extra}`` items plus the exact total, so the portal's
async selects know whether there is a next page (``page * page_size < total_count``).
Search is literal (the repositories escape the LIKE wildcards) and the order is stable
(label column, then id), so pages never repeat or skip rows.
"""
from typing import Any, Dict, Optional

from app.repo.admin.bundle_repo import BundleAdminRepo
from app.repo.admin.promotion_rule_repo import PromotionRuleAdminRepo
from app.repo.admin.tag_group_repo import TagGroupAdminRepo
from app.repo.admin.tag_repo import TagAdminRepo
from app.repo.admin.users_copy_repo import UsersCopyAdminRepo
from app.repo.admin.base import Paged
from app.schemas.admin.common import AdminPage, LookupItem, PageParams


def _page(result: Paged, to_item) -> AdminPage[LookupItem]:
    return AdminPage[LookupItem](data=[to_item(row) for row in result.rows], total_count=result.total)


def _with_suffix(text: Optional[str], suffix: Optional[str]) -> str:
    """``"text (suffix)"``; just the non-empty part when one side is missing."""
    text, suffix = (text or "").strip(), (suffix or "").strip()
    if text and suffix:
        return f"{text} ({suffix})"
    return text or suffix


def user_item(row: Dict[str, Any]) -> LookupItem:
    return LookupItem(id=str(row["id"]), label=row.get("email") or "", email=row.get("email"))


def bundle_item(row: Dict[str, Any]) -> LookupItem:
    code = row.get("bundle_code")
    return LookupItem(id=str(row["id"]), label=_with_suffix(row.get("display_title"), code), bundle_code=code)


def group_item(row: Dict[str, Any]) -> LookupItem:
    # is_active (TEOS-168): the assign page disables inactive groups; NULL counts as active
    return LookupItem(id=str(row["id"]), label=row.get("name") or "", type=row.get("type"),
                      group_category=row.get("group_category"), is_active=row.get("is_active") is not False)


def tag_item(row: Dict[str, Any]) -> LookupItem:
    group = row.get("tag_group") or {}
    return LookupItem(id=str(row["id"]), label=_with_suffix(row.get("name"), group.get("name")))


def promotion_rule_item(row: Dict[str, Any]) -> LookupItem:
    return LookupItem(id=str(row["id"]), label=row.get("name") or "")


class LookupService:
    def __init__(self, users: Optional[UsersCopyAdminRepo] = None, bundles: Optional[BundleAdminRepo] = None,
                 groups: Optional[TagGroupAdminRepo] = None, tags: Optional[TagAdminRepo] = None,
                 promotion_rules: Optional[PromotionRuleAdminRepo] = None):
        self._users = users or UsersCopyAdminRepo()
        self._bundles = bundles or BundleAdminRepo()
        self._groups = groups or TagGroupAdminRepo()
        self._tags = tags or TagAdminRepo()
        self._promotion_rules = promotion_rules or PromotionRuleAdminRepo()

    def users(self, search: Optional[str], page: PageParams,
                record_id: Optional[str] = None) -> AdminPage[LookupItem]:
        """``{id, label: email, email}``; users without an email are left out."""
        return _page(self._users.lookup(search, page, record_id), user_item)

    def bundles(self, search: Optional[str], page: PageParams,
                record_id: Optional[str] = None) -> AdminPage[LookupItem]:
        """``{id, label: "display_title (bundle_code)", bundle_code}``; search on bundle_code."""
        return _page(self._bundles.lookup(search, page, record_id), bundle_item)

    def groups(self, search: Optional[str], page: PageParams,
                record_id: Optional[str] = None) -> AdminPage[LookupItem]:
        """``{id, label: name, type, group_category, is_active}``."""
        return _page(self._groups.lookup(search, page, record_id), group_item)

    def tags(self, search: Optional[str], page: PageParams,
                record_id: Optional[str] = None) -> AdminPage[LookupItem]:
        """``{id, label: "name (group name)"}``; search on the tag name."""
        return _page(self._tags.lookup(search, page, record_id), tag_item)

    def promotion_rules(self, search: Optional[str], page: PageParams,
                record_id: Optional[str] = None) -> AdminPage[LookupItem]:
        """``{id, label: name}``; search on name."""
        return _page(self._promotion_rules.lookup(search, page, record_id), promotion_rule_item)


service = LookupService()
