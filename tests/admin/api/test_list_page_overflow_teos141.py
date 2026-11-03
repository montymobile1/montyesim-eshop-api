"""TEOS-141 (TEOS-109 case 22, AUTH-020): a page whose offset does not fit an SQL ``integer``.

The admin list SQL functions take ``p_offset`` (bundles: ``p_page``) as ``integer``, so
``(page - 1) * page_size > 2,147,483,647`` used to overflow and answer 502 DATABASE_ERROR. ``PageParams``
now turns such a page into the last page that fits, which is past every row too: the list answers
exactly like page=1000 (200, no rows, the real total and facets).
"""
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_SUPER_ADMIN
from app.dependencies.admin_security import admin_user
from app.exceptions.admin import AdminUpstreamError
from app.repo.admin.audit_log_repo import AuditLogRepo
from app.repo.admin.base import AdminBaseRepository
from app.repo.admin.bundle_repo import BundleAdminRepo
from app.repo.admin.promotion_repo import PromotionAdminRepo
from app.repo.admin.user_repo import UserAdminRepo
from app.schemas.admin.audit_log import AdminAuditLogPage
from app.schemas.admin.common import MAX_SQL_INT, AdminPage, PageParams
from tests.admin.conftest import make_admin

LAST_FITTING_PAGE = 107374183  # (107374183 - 1) * 20 = 2,147,483,640
TOTAL = 3  # rows behind every list; each test page is past them

#: (route module, service method, path, facets the endpoint returns, totals)
ENDPOINTS = [
    ("users", "list", "/api/v1/admin/users", {"all": 3, "banned": 1, "active": 2}, None),
    ("orders", "list", "/api/v1/admin/orders", {"all": 3, "success": 2, "failed": 1}, None),
    ("bundles", "list", "/api/v1/admin/bundles", {"all": 3, "active": 2, "inactive": 1},
     {"total": 3, "active_count": 2, "inactive_count": 1}),
    ("devices", "list", "/api/v1/admin/devices", {"all": 3, "android": 2, "ios": 1}, None),
    ("vouchers", "list", "/api/v1/admin/vouchers", {"all": 3, "active": 3, "inactive": 0}, None),
    ("audit_log", "list", "/api/v1/admin/audit-log", {"by_table": {"promotion": 3}}, None),
    ("contact_us", "list", "/api/v1/admin/contact-us", None, None),
    ("groups", "list", "/api/v1/admin/groups", {"all": 3, "active": 3, "inactive": 0}, None),
    ("promotions", "list", "/api/v1/admin/promotions",
     {"all": 3, "active": 1, "inactive": 1, "expired": 1, "scheduled": 0}, None),
]
IDS = [e[0] for e in ENDPOINTS]


def _page_arg(args, kwargs) -> PageParams:
    return next(a for a in [*args, *kwargs.values()] if isinstance(a, PageParams))


def _database(facets, totals, received):
    """Stands in for the list SQL: an ``integer`` offset or page that overflows fails like it does on QA
    (502 DATABASE_ERROR); otherwise the page (here always past the rows) with the real total and facets."""
    def answer(*args, **kwargs):
        page = _page_arg(args, kwargs)
        received.append(page)
        if page.offset > MAX_SQL_INT or page.page > MAX_SQL_INT:
            raise AdminUpstreamError("DATABASE_ERROR")
        page_class = AdminAuditLogPage if facets and "by_table" in facets else AdminPage
        return page_class(data=[], total_count=TOTAL, facets=facets, totals=totals)
    return answer


@pytest.fixture
def as_super_admin():
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(ROLE_SUPER_ADMIN)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def endpoint(request, monkeypatch):
    module_name, method, path, facets, totals = request.param
    module = __import__(f"app.api.v1.admin.{module_name}", fromlist=["service"])
    fake = MagicMock(name=f"{module_name}-service")
    received = []
    getattr(fake, method).side_effect = _database(facets, totals, received)
    monkeypatch.setattr(module, "service", fake)
    return path, received


# ---------- every admin list endpoint QA listed ----------

@pytest.mark.parametrize("endpoint", ENDPOINTS, ids=IDS, indirect=True)
@pytest.mark.parametrize("page", [107374184, 1_000_000_000, 10**30])
def test_page_past_the_sql_integer_answers_like_page_1000(api_client, as_super_admin, endpoint, page):
    path, received = endpoint
    expected = api_client.get(path, params={"page": 1000, "page_size": 20})

    response = api_client.get(path, params={"page": page, "page_size": 20})

    assert response.status_code == 200
    assert response.json() == expected.json()
    assert response.json()["data"]["data"] == []
    assert received[-1].page == LAST_FITTING_PAGE
    assert received[-1].offset <= MAX_SQL_INT


@pytest.mark.parametrize("endpoint", ENDPOINTS, ids=IDS, indirect=True)
def test_last_page_that_fits_is_unchanged(api_client, as_super_admin, endpoint):
    path, received = endpoint

    response = api_client.get(path, params={"page": LAST_FITTING_PAGE, "page_size": 20})

    assert response.status_code == 200
    assert response.json()["data"]["data"] == []
    assert response.json()["data"]["total_count"] == TOTAL
    assert (received[-1].page, received[-1].offset) == (LAST_FITTING_PAGE, 2_147_483_640)


@pytest.mark.parametrize("endpoint", ENDPOINTS, ids=IDS, indirect=True)
@pytest.mark.parametrize("page,page_size,offset", [(1, 20, 0), (2, 20, 20), (1000, 20, 19980), (7, 100, 600)])
def test_normal_pages_reach_the_database_unchanged(api_client, as_super_admin, endpoint, page, page_size, offset):
    path, received = endpoint

    response = api_client.get(path, params={"page": page, "page_size": page_size})

    assert response.status_code == 200
    assert (received[-1].page, received[-1].page_size, received[-1].offset) == (page, page_size, offset)


@pytest.mark.parametrize("endpoint", ENDPOINTS[:1], ids=IDS[:1], indirect=True)
@pytest.mark.parametrize("params", [{"page": 0}, {"page": -1}, {"page_size": 0}, {"page_size": 101}])
def test_invalid_page_or_page_size_still_422(api_client, as_super_admin, endpoint, params):
    path, received = endpoint

    assert api_client.get(path, params=params).status_code == 422
    assert received == []


# ---------- PageParams ----------

@pytest.mark.parametrize("page_size", [1, 7, 20, 99, 100])
def test_page_params_never_exceed_sql_integer(page_size):
    page = PageParams(page=10**30, page_size=page_size)

    assert page.page <= MAX_SQL_INT
    assert page.offset <= MAX_SQL_INT
    assert page.page_size == page.limit == page_size
    # the next page would not fit any more
    assert page.offset + page_size > MAX_SQL_INT or page.page == MAX_SQL_INT


def test_page_params_keep_pages_that_fit():
    assert PageParams(page=LAST_FITTING_PAGE, page_size=20).offset == 2_147_483_640
    assert PageParams(page=3, page_size=25).offset == 50
    assert PageParams().page == 1 and PageParams().offset == 0


def test_page_params_with_page_size_one_keep_the_page_number_an_integer():
    page = PageParams(page=MAX_SQL_INT + 5, page_size=1)

    assert page.page == MAX_SQL_INT
    assert page.offset == MAX_SQL_INT - 1


# ---------- what the repositories send to the database ----------

def _capture_rpc(monkeypatch, repo, result):
    calls = []
    monkeypatch.setattr(repo, "rpc", lambda function, params: calls.append((function, params)) or result)
    return calls


def test_rpc_list_offsets_fit_sql_integer(monkeypatch):
    page = PageParams(page=1_000_000_000, page_size=20)
    users = UserAdminRepo()
    audit = AuditLogRepo()
    promotions = PromotionAdminRepo()
    sent = []
    for repo, call in ((users, lambda r: r.search(None, "all", page)),
                       (audit, lambda r: r.search(page)),
                       (promotions, lambda r: r.search(page))):
        calls = _capture_rpc(monkeypatch, repo, {})
        try:
            call(repo)
        except Exception:  # the shaping of an empty fake result does not matter here
            pass
        sent.extend(params for _, params in calls)

    assert sent and all(0 <= params["p_offset"] <= MAX_SQL_INT for params in sent)


def test_bundle_search_page_fits_sql_integer(monkeypatch):
    repo = BundleAdminRepo()
    calls = _capture_rpc(monkeypatch, repo, {"items": [], "total_count": TOTAL, "facets": {}})

    repo.search("x", None, "all", PageParams(page=10**12, page_size=20))

    params = calls[0][1]
    assert params["p_page"] == LAST_FITTING_PAGE
    assert (params["p_page"] - 1) * params["p_page_size"] <= MAX_SQL_INT


def test_postgrest_range_offset_fits_sql_integer():
    query = MagicMock(name="query")
    query.order.return_value = query
    query.range.return_value.execute.return_value = MagicMock(data=[], count=TOTAL)
    repo = AdminBaseRepository.__new__(AdminBaseRepository)
    repo.table_name = "promotion"

    result = repo.paged(query, PageParams(page=10**12, page_size=20))

    offset, end = query.range.call_args.args
    assert offset == 2_147_483_640 and end - offset + 1 == 20
    assert (result.rows, result.total) == ([], TOTAL)
