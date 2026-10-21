"""TEOS-117 orders repository: payment status, date range, literal search, facets, order_status sort.
Queries are built with the real PostgREST client (dummy URL) and inspected; nothing is sent."""
from datetime import datetime
from types import SimpleNamespace

from app.repo.admin.base import Paged
from app.repo.admin.user_order_repo import UserOrderAdminRepo
from app.schemas.admin.common import PageParams

PAGE = PageParams(page=1, page_size=20)
ORDER_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"


def _capture(monkeypatch, repo):
    seen = {}

    def fake_paged(query, page, order=None):
        seen.update(params=list(query.params.multi_items()), order=list(order) if order else None)
        return Paged(rows=[], total=0)

    monkeypatch.setattr(repo, "paged", fake_paged)
    return seen


def test_all_filters(monkeypatch):
    repo = UserOrderAdminRepo()
    seen = _capture(monkeypatch, repo)

    repo.search(PAGE, user_id="u1", status="failure", payment_status="success",
                created_from=datetime(2026, 1, 1), created_to=datetime(2026, 2, 1))

    params = seen["params"]
    assert ("user_id", "eq.u1") in params
    assert ("payment_status", "eq.success") in params
    assert ("order_status", "eq.failure") in params
    assert ("order_type", "neq.Wallet_Top_Up") in params
    assert ("created_at", "gte.2026-01-01T00:00:00") in params
    assert ("created_at", "lt.2026-02-01T00:00:00") in params


def test_search_matches_promo_and_bundle_code_literally(monkeypatch):
    repo = UserOrderAdminRepo()
    seen = _capture(monkeypatch, repo)

    repo.search(PAGE, search="  a,b)%_.c  ")

    pattern = r'"%a,b)\\%\\_.c%"'  # like() escapes % and _, quoted() doubles the backslash
    assert ("or", f"(promo_code.ilike.{pattern},bundle_id.ilike.{pattern})") in seen["params"]


def test_search_with_a_full_uuid_also_matches_the_order_id(monkeypatch):
    repo = UserOrderAdminRepo()
    seen = _capture(monkeypatch, repo)

    repo.search(PAGE, search=ORDER_ID)

    value = dict(seen["params"])["or"]
    assert f"id.eq.{ORDER_ID}" in value
    assert value.startswith("(promo_code.ilike.")


def test_blank_search_is_ignored(monkeypatch):
    repo = UserOrderAdminRepo()
    seen = _capture(monkeypatch, repo)

    repo.search(PAGE, search="   ")

    assert "or" not in dict(seen["params"])


def test_order_status_sort_with_tie_breakers(monkeypatch):
    repo = UserOrderAdminRepo()
    seen = _capture(monkeypatch, repo)

    repo.search(PAGE, sort="order_status", descending=False)

    assert seen["order"] == [("order_status", False), ("created_at", True), ("id", True)]


def test_facets_count_every_tab_without_the_status_filter(monkeypatch):
    repo = UserOrderAdminRepo()
    queries = []
    counts = {None: 10, "success": 6, "pending": 2, "canceled": 1, "failure": 1}

    def fake_run(call):
        query = call.__defaults__[0]
        params = dict(query.params.multi_items())
        queries.append(params)
        status = params.get("order_status", "eq.")[3:] or None
        return SimpleNamespace(count=counts[status], data=None)

    monkeypatch.setattr(repo, "_run", fake_run)

    facets = repo.facets(user_id="u1", payment_status="success", search="EU")

    assert facets == {"all": 10, "success": 6, "pending": 2, "canceled": 1, "failed": 1}
    assert len(queries) == 5
    for params in queries:
        assert params["user_id"] == "eq.u1"
        assert params["payment_status"] == "eq.success"
        assert params["order_type"] == "neq.Wallet_Top_Up"
        assert "or" in params
    assert "order_status" not in queries[0]
    assert [q.get("order_status") for q in queries[1:]] == ["eq.success", "eq.pending", "eq.canceled", "eq.failure"]


def test_bundle_code_falls_back_to_bundle_id(monkeypatch):
    repo = UserOrderAdminRepo()
    monkeypatch.setattr(repo, "paged", lambda query, page, order=None: Paged(
        rows=[{"id": "o1", "bundle_id": "EU-5GB", "bundle_data": "-"}], total=1))

    row = repo.search(PAGE).rows[0]

    assert row == {"id": "o1", "bundle_id": "EU-5GB", "bundle_name": None, "bundle_code": "EU-5GB"}
