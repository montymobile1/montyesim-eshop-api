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

    assert result.rows == [{"id": "o1", "amount": 500, "bundle_name": None, "bundle_code": None}]
    assert result.total == 1


def test_orders_never_return_payment_intent_or_raw_bundle_data(monkeypatch):
    """TEOS-117: payment_intent_code and the raw bundle_data never leave the server."""
    repo = UserOrderAdminRepo()
    bundle = '{"display_title": "Europe 5GB", "bundle_code": "EU-5GB", "price": 10}'
    _capture(monkeypatch, repo, rows=[
        {"id": "o1", "amount": 500, "payment_intent_code": "pi_secret", "bundle_data": bundle, "otp": "1"},
        {"id": "o2", "amount": 100, "payment_intent_code": None, "bundle_data": None},
    ], total=2)

    result = repo.search(PAGE)

    for row in result.rows:
        assert "payment_intent_code" not in row
        assert "bundle_data" not in row
        assert "otp" not in row
    assert result.rows[0] == {"id": "o1", "amount": 500, "bundle_name": "Europe 5GB", "bundle_code": "EU-5GB"}
    assert result.rows[1] == {"id": "o2", "amount": 100, "bundle_name": None, "bundle_code": None}


@pytest.mark.parametrize("bundle_data, name, code", [
    ('{"display_title": "Europe 5GB", "bundle_code": "EU-5GB"}', "Europe 5GB", "EU-5GB"),
    ({"display_title": "Global 1GB", "bundle_code": "GL-1GB"}, "Global 1GB", "GL-1GB"),  # jsonb / dict
    ('{"bundle_code": "EU-5GB"}', None, "EU-5GB"),  # no title
    ('{"display_title": "", "bundle_code": ""}', None, None),  # empty values
    ("-", None, None),
    (None, None, None),
    ("", None, None),
    ("{not json", None, None),
    ("[1, 2]", None, None),  # valid JSON, not an object
    ("null", None, None),
    ("42", None, None),
])
def test_orders_bundle_name_and_code_extraction(monkeypatch, bundle_data, name, code):
    repo = UserOrderAdminRepo()
    _capture(monkeypatch, repo, rows=[{"id": "o1", "bundle_data": bundle_data}], total=1)

    row = repo.search(PAGE).rows[0]

    assert row == {"id": "o1", "bundle_name": name, "bundle_code": code}


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
    assert seen["params"]["or"] == r'(email.ilike."%50\\%\\_a%",content.ilike."%50\\%\\_a%")'


def test_contact_us_blank_search_lists_everything(monkeypatch):
    repo = ContactUsAdminRepo()
    seen = _capture(monkeypatch, repo)

    repo.search(PAGE, search="  ")

    assert "or" not in seen["params"]


def test_contact_us_search_matches_email_or_content(monkeypatch):
    # TEOS-118: the term is matched against the email OR the message content
    repo = ContactUsAdminRepo()
    seen = _capture(monkeypatch, repo)

    repo.search(PAGE, search=" Hello ")

    assert seen["params"]["or"] == '(email.ilike."%Hello%",content.ilike."%Hello%")'
    assert seen["order"] is None  # the default order: created_at desc, id desc


def test_contact_us_percent_is_literal_in_both_columns(monkeypatch):
    repo = ContactUsAdminRepo()
    seen = _capture(monkeypatch, repo)

    repo.search(PAGE, search="%")

    assert seen["params"]["or"] == r'(email.ilike."%\\%%",content.ilike."%\\%%")'


def test_contact_us_date_range_is_half_open(monkeypatch):
    from datetime import datetime
    repo = ContactUsAdminRepo()
    seen = {}

    def fake_paged(query, page, order=None):
        seen["created_at"] = query.params.get_list("created_at")
        return Paged(rows=[], total=0)

    monkeypatch.setattr(repo, "paged", fake_paged)

    repo.search(PAGE, created_from=datetime(2026, 10, 1), created_to=datetime(2026, 10, 22))

    assert seen["created_at"] == ["gte.2026-10-01T00:00:00", "lt.2026-10-22T00:00:00"]


def test_contact_us_no_dates_no_created_at_filter(monkeypatch):
    repo = ContactUsAdminRepo()
    seen = _capture(monkeypatch, repo)

    repo.search(PAGE)

    assert seen["params"] == {"select": "id,email,content,created_at"}
