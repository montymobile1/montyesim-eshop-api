"""MS-5077 lookups: repository queries (built with the real PostgREST client, never executed) and
the mapping of rows to ``{id, label, ...extra}`` items."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from postgrest.exceptions import APIError

from app.exceptions.admin import AdminUpstreamError
from app.repo.admin.base import Paged
from app.repo.admin.bundle_repo import BundleAdminRepo
from app.repo.admin.promotion_rule_repo import PromotionRuleAdminRepo
from app.repo.admin.tag_group_repo import TagGroupAdminRepo
from app.repo.admin.tag_repo import TagAdminRepo
from app.repo.admin.users_copy_repo import UsersCopyAdminRepo
from app.schemas.admin.common import PageParams
from app.services.admin.lookup_service import LookupService

PAGE = PageParams(page=2, page_size=10)


def _capture(monkeypatch, repo):
    """Replaces ``paged`` so the built query and the requested order can be inspected."""
    seen = {}

    def fake_paged(query, page, order=None):
        seen.update(query=query, page=page, order=order, params=dict(query.params.multi_items()))
        return Paged(rows=[], total=0)

    monkeypatch.setattr(repo, "paged", fake_paged)
    return seen


# ---------- repositories ----------

def test_users_lookup_needs_an_email_searches_email_and_orders_by_email_then_id(monkeypatch):
    repo = UsersCopyAdminRepo()
    seen = _capture(monkeypatch, repo)

    repo.lookup(" ab ", PAGE)

    assert seen["params"]["select"] == "id,email"
    emails = [v for k, v in seen["query"].params.multi_items() if k == "email"]
    assert sorted(emails) == ["ilike.%ab%", "not.is.null"]
    assert seen["order"] == (("email", False), ("id", False))
    assert seen["page"] is PAGE


def test_bundles_lookup_searches_bundle_code_and_orders_by_display_title_then_id(monkeypatch):
    repo = BundleAdminRepo()
    seen = _capture(monkeypatch, repo)

    repo.lookup("esim_50%", PAGE)

    assert seen["params"]["select"] == "id,display_title:data->>display_title,bundle_code:data->>bundle_code"
    assert seen["params"]["data->>bundle_code"] == r"ilike.%esim\_50\%%"
    assert seen["params"]["order"] == "data->>display_title"
    assert seen["order"] == (("id", False),)


def test_bundles_order_keeps_display_title_first_then_id():
    """paged() appends its order to the display-title order already on the query."""
    repo = BundleAdminRepo()
    query = repo.q("id").order("data->>display_title").order("id")

    assert query.params["order"] == "data->>display_title,id"


@pytest.mark.parametrize("repo_class, select, column", [
    (TagGroupAdminRepo, "id,name,type,group_category,is_active", "name"),
    (TagAdminRepo, "id,name,tag_group(name)", "name"),
    (PromotionRuleAdminRepo, "id,name", "name"),
])
def test_name_lookups_search_name_and_order_by_name_then_id(monkeypatch, repo_class, select, column):
    repo = repo_class()
    seen = _capture(monkeypatch, repo)

    repo.lookup('a,b"(c)', PAGE)

    assert seen["params"]["select"] == select
    assert seen["params"][column] == 'ilike.%a,b"(c)%'
    assert seen["order"] == ((column, False), ("id", False))


@pytest.mark.parametrize("repo_class", [UsersCopyAdminRepo, BundleAdminRepo, TagGroupAdminRepo, TagAdminRepo,
                                        PromotionRuleAdminRepo])
@pytest.mark.parametrize("search", [None, "", "   "])
def test_blank_search_returns_the_first_page_unfiltered(monkeypatch, repo_class, search):
    repo = repo_class()
    seen = _capture(monkeypatch, repo)

    repo.lookup(search, PageParams())

    filtered = {k for k in seen["params"] if k not in ("select", "order")}
    assert filtered <= {"email"}  # users keep only "email is not null"
    if "email" in filtered:
        assert seen["params"]["email"] == "not.is.null"


def test_wildcards_in_search_are_literal(monkeypatch):
    repo = TagAdminRepo()
    seen = _capture(monkeypatch, repo)

    repo.lookup("%", PAGE)

    assert seen["params"]["name"] == r"ilike.%\%%"


def test_database_error_becomes_502():
    repo = PromotionRuleAdminRepo()
    failing = MagicMock()
    failing.range.return_value.execute.side_effect = APIError(
        {"code": "57014", "message": "timeout", "details": None, "hint": None})

    with pytest.raises(AdminUpstreamError):
        repo.paged(failing, PAGE, order=())


# ---------- service mapping ----------

@pytest.fixture
def repos():
    return SimpleNamespace(users=MagicMock(), bundles=MagicMock(), groups=MagicMock(), tags=MagicMock(),
                           promotion_rules=MagicMock())


@pytest.fixture
def service(repos):
    return LookupService(users=repos.users, bundles=repos.bundles, groups=repos.groups, tags=repos.tags,
                         promotion_rules=repos.promotion_rules)


def test_users_label_is_the_email(service, repos):
    repos.users.lookup.return_value = Paged(rows=[{"id": "u1", "email": "a@x.com"}], total=31)

    page = service.users("a", PAGE)

    repos.users.lookup.assert_called_once_with("a", PAGE, None)
    assert page.total_count == 31
    assert [item.model_dump() for item in page.data] == [{"id": "u1", "label": "a@x.com", "email": "a@x.com"}]


def test_bundles_label_has_title_and_code(service, repos):
    repos.bundles.lookup.return_value = Paged(rows=[
        {"id": "b1", "display_title": "Europe 5GB", "bundle_code": "EU5"},
        {"id": "b2", "display_title": None, "bundle_code": "XX1"},
    ], total=2)

    items = [item.model_dump() for item in service.bundles("eu", PAGE).data]

    assert items == [{"id": "b1", "label": "Europe 5GB (EU5)", "bundle_code": "EU5"},
                     {"id": "b2", "label": "XX1", "bundle_code": "XX1"}]


def test_groups_carry_type_and_category_and_string_ids(service, repos):
    repos.groups.lookup.return_value = Paged(rows=[{"id": 7, "name": "Regions", "type": 1,
                                                    "group_category": "region"}], total=1)

    items = [item.model_dump() for item in service.groups(None, PAGE).data]

    # is_active missing (NULL) counts as active (TEOS-168)
    assert items == [{"id": "7", "label": "Regions", "type": 1, "group_category": "region", "is_active": True}]


def test_tags_label_is_name_and_group(service, repos):
    repos.tags.lookup.return_value = Paged(rows=[
        {"id": "t1", "name": "France", "tag_group": {"name": "Countries"}},
        {"id": "t2", "name": "Orphan", "tag_group": None},
    ], total=2)

    items = [item.model_dump() for item in service.tags("fr", PAGE).data]

    assert items == [{"id": "t1", "label": "France (Countries)"}, {"id": "t2", "label": "Orphan"}]


def test_promotion_rules_label_is_the_name(service, repos):
    repos.promotion_rules.lookup.return_value = Paged(rows=[{"id": "r1", "name": "Cashback/Buy/1"}], total=1)

    page = service.promotion_rules("cash", PAGE)

    assert [item.model_dump() for item in page.data] == [{"id": "r1", "label": "Cashback/Buy/1"}]
    assert page.total_count == 1
