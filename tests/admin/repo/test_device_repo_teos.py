"""DeviceAdminRepo (TEOS-115): hidden columns, platform / logged-in filters, sort and facets.
Queries are built with the real PostgREST client (dummy URL) and inspected; nothing is sent."""
from types import SimpleNamespace

import pytest

from app.repo.admin.base import Paged
from app.repo.admin.device_repo import HIDDEN_COLUMNS, DeviceAdminRepo
from app.schemas.admin.common import PageParams

PAGE = PageParams(page=1, page_size=20)


def _capture_page(monkeypatch, repo, rows=None):
    seen = {}

    def fake_paged(query, page, order=None):
        seen.update(params=dict(query.params.multi_items()), order=list(order) if order else None)
        return Paged(rows=rows or [], total=len(rows or []))

    monkeypatch.setattr(repo, "paged", fake_paged)
    return seen


def test_hidden_columns_are_push_token_and_ip():
    assert set(HIDDEN_COLUMNS) == {"fcm_token", "originated_ip"}


def test_rows_never_carry_push_token_or_ip(monkeypatch):
    repo = DeviceAdminRepo()
    _capture_page(monkeypatch, repo, rows=[{"id": 1, "fcm_token": "t", "originated_ip": "1.2.3.4", "os": "ios"}])

    result = repo.search(PAGE)

    assert result.rows == [{"id": 1, "os": "ios"}]


def test_platform_is_a_case_insensitive_equality_on_os(monkeypatch):
    repo = DeviceAdminRepo()
    seen = _capture_page(monkeypatch, repo)

    repo.search(PAGE, platform="ios", logged_in="yes")

    assert seen["params"] == {"select": "*", "os": "ilike.ios", "is_logged_in": "eq.True"}


@pytest.mark.parametrize("kwargs", [{"platform": "windows"}, {"logged_in": "maybe"}, {"sort": "fcm_token"}])
def test_unknown_values_are_refused(monkeypatch, kwargs):
    repo = DeviceAdminRepo()
    _capture_page(monkeypatch, repo)
    with pytest.raises(ValueError):
        repo.search(PAGE, **kwargs)


def test_sort_by_last_login_desc_breaks_ties(monkeypatch):
    repo = DeviceAdminRepo()
    seen = _capture_page(monkeypatch, repo)

    repo.search(PAGE, sort="timestamp_login", descending=True)

    assert seen["order"] == [("timestamp_login", True), ("created_at", True), ("id", True)]


def test_facets_count_each_platform_with_the_other_filters(monkeypatch):
    repo = DeviceAdminRepo()
    queries = []

    def fake_run(call):
        # the lambda closes over the query; read its params through the closure
        query = call.__closure__[0].cell_contents
        params = dict(query.params.multi_items())
        queries.append(params)
        return SimpleNamespace(count={None: 10, "ilike.android": 6, "ilike.ios": 4}[params.get("os")])

    monkeypatch.setattr(repo, "_run", fake_run)

    facets = repo.platform_facets(user_id="u1", logged_in="no")

    assert facets == {"all": 10, "android": 6, "ios": 4}
    assert len(queries) == 3
    for params in queries:
        assert params["select"] == "id"
        assert params["user_id"] == "eq.u1" and params["is_logged_in"] == "eq.False"
