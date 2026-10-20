"""MS-5081 repositories: filters, order and hidden columns. Queries are built with the real PostgREST
client (dummy URL) and inspected; ``paged`` is replaced so nothing is sent over the network."""
import pytest

from app.repo.admin.base import Paged
from app.repo.admin.contact_us_repo import ContactUsAdminRepo
from app.repo.admin.device_repo import DeviceAdminRepo
from app.repo.admin.user_order_repo import UserOrderAdminRepo
from app.schemas.admin.common import PageParams

PAGE = PageParams(page=2, page_size=10)


def _capture(monkeypatch, repo, rows=None, total=0):
    seen = {}

    def fake_paged(query, page, order=None):
        seen.update(params=dict(query.params.multi_items()), page=page, order=list(order) if order else None)
        return Paged(rows=rows or [], total=total)

    monkeypatch.setattr(repo, "paged", fake_paged)
    return seen


def test_orders_default_excludes_wallet_top_ups_newest_first(monkeypatch):
    repo = UserOrderAdminRepo()
    seen = _capture(monkeypatch, repo)

    repo.search(PAGE)

    assert seen["params"] == {"select": "*", "order_type": "neq.Wallet_Top_Up"}
    assert seen["order"] == [("created_at", True), ("id", True)]
    assert seen["page"] is PAGE


def test_orders_filters_and_amount_sort_with_stable_tie_breakers(monkeypatch):
    repo = UserOrderAdminRepo()
    seen = _capture(monkeypatch, repo)

    repo.search(PAGE, user_id="u1", status="success", sort="amount", descending=False)

    assert seen["params"]["user_id"] == "eq.u1"
    assert seen["params"]["order_status"] == "eq.success"
    assert seen["order"] == [("amount", False), ("created_at", True), ("id", True)]


def test_orders_ascending_created_at(monkeypatch):
    repo = UserOrderAdminRepo()
    seen = _capture(monkeypatch, repo)

    repo.search(PAGE, sort="created_at", descending=False)

    assert seen["order"] == [("created_at", False), ("id", True)]


def test_orders_never_return_the_otp(monkeypatch):
    repo = UserOrderAdminRepo()
    _capture(monkeypatch, repo, rows=[{"id": "o1", "otp": "123456", "amount": 500}], total=1)

    result = repo.search(PAGE)

    assert result.rows == [{"id": "o1", "amount": 500}]
    assert result.total == 1


def test_orders_sort_column_is_checked(monkeypatch):
    repo = UserOrderAdminRepo()
    with pytest.raises(ValueError):
        # the real paged() validates column names; the service only passes enum values
        repo.paged(repo.q(), PAGE, [("amount;drop", True)])


def test_devices_filter_by_user_newest_first_without_fcm_token(monkeypatch):
    repo = DeviceAdminRepo()
    seen = _capture(monkeypatch, repo, rows=[{"id": 1, "fcm_token": "secret", "manufacturer": "X"}], total=1)

    result = repo.search(PAGE, user_id="u1")

    assert seen["params"] == {"select": "*", "user_id": "eq.u1"}
    assert seen["order"] is None  # paged() default: created_at desc, id desc
    assert result.rows == [{"id": 1, "manufacturer": "X"}]


def test_devices_without_user_has_no_filter(monkeypatch):
    repo = DeviceAdminRepo()
    seen = _capture(monkeypatch, repo)

    repo.search(PAGE)

    assert seen["params"] == {"select": "*"}


def test_contact_us_search_is_literal(monkeypatch):
    repo = ContactUsAdminRepo()
    seen = _capture(monkeypatch, repo)

    repo.search(PAGE, search="50%_a")

    assert seen["params"]["select"] == "id,email,content,created_at"
    # % and _ are escaped for LIKE, then the backslashes are escaped again inside the quoted value
    assert seen["params"]["or"] == r'(email.ilike."%50\\%\\_a%")'


def test_contact_us_blank_search_lists_everything(monkeypatch):
    repo = ContactUsAdminRepo()
    seen = _capture(monkeypatch, repo)

    repo.search(PAGE, search="  ")

    assert "or" not in seen["params"]
