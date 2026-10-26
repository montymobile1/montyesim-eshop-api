"""AUTH-038 (TEOS-109 shared list conventions): a page after the last one.

The lists built on PostgREST (``AdminBaseRepository.paged``) select with ``count=exact`` and a range.
PostgREST answers an offset past the total with 416 PGRST103 (checked on PostgREST v12.2.3), which
used to become 502 DATABASE_ERROR. ``paged`` now answers that page with no rows and the real total,
read with ``limit(0)`` on the same query, like the lists built on SQL functions (users, audit-log, ...).

The whole stack runs: routes, services, repositories and postgrest-py; only the HTTP calls to the
dummy Supabase host are answered by ``FakePostgrest`` below, which pages like PostgREST does.
"""
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx
import pytest

from app.config.admin_permissions import ROLE_SUPER_ADMIN
from app.dependencies.admin_security import admin_user
from app.exceptions.admin import AdminUpstreamError
from app.repo.admin.app_config_repo import AppConfigAdminRepo
from app.repo.admin.banner_repo import BannerAdminRepo
from app.repo.admin.bundle_repo import BundleAdminRepo
from app.repo.admin.contact_us_repo import ContactUsAdminRepo
from app.repo.admin.device_repo import DeviceAdminRepo
from app.repo.admin.promotion_rule_repo import PromotionRuleAdminRepo
from app.repo.admin.tag_group_repo import TagGroupAdminRepo
from app.repo.admin.user_order_repo import UserOrderAdminRepo
from app.repo.admin.voucher_repo import VoucherAdminRepo
from app.schemas.admin.common import PageParams
from tests.admin.conftest import make_admin

TOTAL = 7944  # like QA's orders: page 398 of 20 is the last one (4 rows), 399 is past it
FILTERED_TOTAL = 22
LAST_PAGE = 398
USER_ID = "33333333-3333-3333-3333-333333333333"
FILTER_MARKERS = ("zzq", USER_ID, "is_active=not.is.true")


class FakePostgrest:
    """Answers PostgREST table reads: ``count=exact`` selects and HEAD counts. The total is TOTAL, or
    FILTERED_TOTAL when the query carries one of FILTER_MARKERS (a search or filter of the test)."""

    #: The only RPCs a PostgREST list makes: the groups page reads its bundle counts (TEOS-173), none here.
    RPC_ANSWERS = {"/rest/v1/rpc/admin_group_bundle_counts": []}

    def __init__(self):
        self.reads = []  # (table, {param: value}) of every GET

    def total(self, query: str) -> int:
        return FILTERED_TOTAL if any(marker in query for marker in FILTER_MARKERS) else TOTAL

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = urlsplit(str(request.url)).path
        if path in self.RPC_ANSWERS:
            return httpx.Response(200, json=self.RPC_ANSWERS[path], request=request)
        assert path.startswith("/rest/v1/") and "/rpc/" not in path, f"unexpected call {request.method} {path}"
        table = path.rsplit("/", 1)[-1]
        query = unquote(request.url.query.decode())
        total = self.total(query)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Range": f"*/{total}"}, request=request)
        params = dict(parse_qsl(request.url.query.decode()))
        self.reads.append((table, params))
        offset, limit = int(params.get("offset", 0)), int(params.get("limit", total))
        if offset > total:
            return httpx.Response(416, headers={"Content-Range": f"*/{total}"}, request=request, json={
                "code": "PGRST103", "details": f"An offset of {offset} was requested, but there are only "
                                               f"{total} rows.", "hint": None,
                "message": "Requested range not satisfiable"})
        rows = [self.row(table, i) for i in range(offset, min(offset + limit, total))]
        content_range = f"{offset}-{offset + len(rows) - 1}/{total}" if rows else f"*/{total}"
        return httpx.Response(206 if limit < total else 200, headers={"Content-Range": content_range},
                              json=rows, request=request)

    @staticmethod
    def row(table, i):
        return {"id": i + 1, "created_at": "2026-10-01T10:00:00+00:00", "table": table}


@pytest.fixture
def postgrest(monkeypatch):
    """Every request to the dummy Supabase host goes to FakePostgrest; the TestClient is untouched."""
    fake = FakePostgrest()
    real_send = httpx.Client.send

    def send(self, request, *args, **kwargs):
        if request.url.host == "dummy.supabase.co":
            return fake(request)
        return real_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", send)
    return fake


@pytest.fixture
def as_super_admin():
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(ROLE_SUPER_ADMIN)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


#: path, query that filters the list (the fake then counts FILTERED_TOTAL rows)
LISTS = [
    ("/api/v1/admin/orders", {"user_id": USER_ID}),
    ("/api/v1/admin/bundles", {"status": "inactive"}),
    ("/api/v1/admin/devices", {"user_id": USER_ID}),
    ("/api/v1/admin/vouchers", {"search": "zzq"}),
    ("/api/v1/admin/contact-us", {"search": "zzq"}),
    ("/api/v1/admin/groups", {"search": "zzq"}),
    ("/api/v1/admin/promotion-rules", {"search": "zzq"}),
    ("/api/v1/admin/banners", {"search": "zzq"}),
    ("/api/v1/admin/settings", {"search": "zzq"}),
]
IDS = [path.rsplit("/", 1)[-1] for path, _ in LISTS]


def _page_reads(fake):
    """The reads that page a list (offset/limit), not the facet counts or lookups."""
    return [(table, params) for table, params in fake.reads if "limit" in params]


# ---------- past the last page: 200, no rows, the real total ----------

@pytest.mark.parametrize("path,filters", LISTS, ids=IDS)
@pytest.mark.parametrize("page", [LAST_PAGE + 1, 100_000, 1_000_000_000])
def test_page_past_the_last_one_answers_no_rows_and_the_real_total(api_client, as_super_admin, postgrest,
                                                                     path, filters, page):
    response = api_client.get(path, params={"page": page, "page_size": 20})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["data"] == []
    assert body["data"]["total_count"] == body["totalCount"] == TOTAL
    # the page itself (416) and then one count of the same query with limit 0
    reads = _page_reads(postgrest)
    assert [params["limit"] for _, params in reads[-2:]] == ["20", "0"]
    assert "offset" not in reads[-1][1]


@pytest.mark.parametrize("path,filters", LISTS, ids=IDS)
def test_filtered_page_past_the_last_one_answers_the_filtered_total(api_client, as_super_admin, postgrest,
                                                                      path, filters):
    response = api_client.get(path, params={**filters, "page": 3, "page_size": 20})  # 22 rows: page 3 starts at 40

    assert response.status_code == 200, response.text
    assert response.json()["data"]["data"] == []
    assert response.json()["data"]["total_count"] == FILTERED_TOTAL
    count_params = _page_reads(postgrest)[-1][1]
    count_query = "&".join(f"{key}={value}" for key, value in count_params.items())
    assert any(marker in count_query for marker in FILTER_MARKERS)  # the count keeps the filters


@pytest.mark.parametrize("path,filters", LISTS, ids=IDS)
def test_past_the_last_page_has_the_same_envelope_and_facets_as_page_one(api_client, as_super_admin,
                                                                           postgrest, path, filters):
    past = api_client.get(path, params={"page": LAST_PAGE + 1, "page_size": 20}).json()
    first = api_client.get(path, params={"page": 1, "page_size": 1}).json()

    assert past.keys() == first.keys()
    assert past["data"].keys() == first["data"].keys()
    for key in ("facets", "totals"):
        assert past["data"].get(key) == first["data"].get(key)
    assert {k: v for k, v in past.items() if k not in ("data", "totalCount")} == \
           {k: v for k, v in first.items() if k not in ("data", "totalCount")}


@pytest.mark.parametrize("path,filters", LISTS[:1], ids=IDS[:1])
def test_filter_matching_nothing_on_page_two_is_200_with_total_zero(api_client, as_super_admin, postgrest,
                                                                     monkeypatch, path, filters):
    monkeypatch.setattr(FakePostgrest, "total", lambda self, query: 0)

    response = api_client.get(path, params={"page": 2, "page_size": 20})

    assert response.status_code == 200, response.text
    assert response.json()["data"]["data"] == [] and response.json()["data"]["total_count"] == 0


# ---------- pages inside the range: one read, exactly as before ----------

REPOS = [
    ("orders", UserOrderAdminRepo, lambda r, p: r.search(p)),
    ("bundles", BundleAdminRepo, lambda r, p: r.list_plain("all", p)),
    ("devices", DeviceAdminRepo, lambda r, p: r.search(p)),
    ("vouchers", VoucherAdminRepo, lambda r, p: r.search(None, "all", p)),
    ("contact-us", ContactUsAdminRepo, lambda r, p: r.search(p)),
    ("groups", TagGroupAdminRepo, lambda r, p: r.search(None, "all", p)),
    ("promotion-rules", PromotionRuleAdminRepo, lambda r, p: r.search(None, None, None, p)),
    ("banners", BannerAdminRepo, lambda r, p: r.search(None, "all", p)),
    ("settings", AppConfigAdminRepo, lambda r, p: r.search(p)),
]
REPO_IDS = [name for name, _, _ in REPOS]


@pytest.mark.parametrize("name,repo_class,search", REPOS, ids=REPO_IDS)
@pytest.mark.parametrize("page,rows", [(1, 20), (LAST_PAGE, 4)])
def test_pages_inside_the_range_are_one_read_as_before(postgrest, name, repo_class, search, page, rows):
    result = search(repo_class(), PageParams(page=page, page_size=20))

    assert (len(result.rows), result.total) == (rows, TOTAL)
    reads = _page_reads(postgrest)
    assert len(reads) == 1
    assert (reads[0][1]["offset"], reads[0][1]["limit"]) == (str((page - 1) * 20), "20")


@pytest.mark.parametrize("name,repo_class,search", REPOS, ids=REPO_IDS)
def test_offset_equal_to_the_total_stays_one_read(postgrest, name, repo_class, search):
    # 7944 / 24 = 331 pages exactly: page 332 starts at the total, which PostgREST answers 206 []
    result = search(repo_class(), PageParams(page=332, page_size=24))

    assert (result.rows, result.total) == ([], TOTAL)
    assert len(_page_reads(postgrest)) == 1


# ---------- other database errors are still 502 ----------

def test_other_errors_of_the_page_read_are_still_502(postgrest, monkeypatch):
    def broken(self, request):
        return httpx.Response(500, json={"code": "XX000", "message": "boom", "details": None, "hint": None},
                              request=request)
    monkeypatch.setattr(FakePostgrest, "__call__", broken)

    with pytest.raises(AdminUpstreamError):
        VoucherAdminRepo().search(None, "all", PageParams(page=LAST_PAGE + 1, page_size=20))


def test_a_failing_count_after_416_is_502(postgrest, monkeypatch):
    real = FakePostgrest.__call__

    def count_fails(self, request):
        if "limit=0" in str(request.url):
            return httpx.Response(503, json={"code": "PGRST000", "message": "down", "details": None,
                                             "hint": None}, request=request)
        return real(self, request)
    monkeypatch.setattr(FakePostgrest, "__call__", count_fails)

    with pytest.raises(AdminUpstreamError):
        VoucherAdminRepo().search(None, "all", PageParams(page=LAST_PAGE + 1, page_size=20))


# ---------- the lists built on SQL functions never page through PostgREST ----------

def _users(page):
    from app.repo.admin.user_repo import UserAdminRepo
    return UserAdminRepo(), lambda repo: repo.search(None, "all", page)


def _audit(page):
    from app.repo.admin.audit_log_repo import AuditLogRepo
    return AuditLogRepo(), lambda repo: repo.search(page)


def _promotions(page):
    from app.repo.admin.promotion_repo import PromotionAdminRepo
    return PromotionAdminRepo(), lambda repo: repo.search(page)


def _referrals(page):
    from app.repo.admin.referral_repo import ReferralAdminRepo
    from app.schemas.admin.referrals import AdminReferralFilters
    return ReferralAdminRepo(), lambda repo: repo.search(AdminReferralFilters(), page.limit, page.offset)


def _usage(page):
    from app.repo.admin.promotion_usage_repo import PromotionUsageAdminRepo
    return PromotionUsageAdminRepo(), lambda repo: repo.search(page)


def _settings_history(page):  # GET /settings/history reads the audit log
    from app.repo.admin.audit_log_repo import AuditLogRepo
    return AuditLogRepo(), lambda repo: repo.search(page, table="app_config")


@pytest.mark.parametrize("build", [_users, _audit, _promotions, _referrals, _usage, _settings_history],
                         ids=["users", "audit-log", "promotions", "referrals", "promotions-usage",
                              "settings-history"])
def test_sql_function_lists_do_not_use_paged(monkeypatch, build):
    from app.repo.admin.base import AdminBaseRepository
    monkeypatch.setattr(AdminBaseRepository, "paged", lambda *a, **k: pytest.fail("paged() was used"))
    repo, search = build(PageParams(page=LAST_PAGE + 1, page_size=20))
    calls = []
    monkeypatch.setattr(repo, "rpc", lambda function, params: calls.append(function) or {})

    try:
        search(repo)
    except Exception:  # shaping the empty fake result does not matter here
        pass

    assert len(calls) == 1
