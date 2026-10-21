"""ReferralAdminService and ReferralAdminRepo (MS-5056) with the database mocked."""
import io
import json
from datetime import date
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from postgrest.exceptions import APIError

from app.exceptions.admin import AdminUpstreamError, AdminValidationError
from app.repo.admin.base import AuditContext, Paged
from app.repo.admin.referral_repo import ReferralAdminRepo
from app.schemas.admin.common import PageParams
from app.schemas.admin.referrals import AdminReferralExportRequest, AdminReferralFilters
from app.services.admin.referral_service import (EXPORT_CHUNK_SIZE, EXPORT_COLUMNS, ReferralAdminService,
                                                 export_file_name, export_row_cap, neutralise)

openpyxl = pytest.importorskip("openpyxl", reason="openpyxl (requirements.txt) is needed to read the export back")

CTX = AuditContext(actor_id="11111111-1111-1111-1111-111111111111", ip="1.2.3.4")
USER_ID = UUID("33333333-3333-3333-3333-333333333333")


def _row(n=1, **extra):
    return {"id": f"0a0a0a0a-0000-0000-0000-{n:012d}", "created_at": "2026-10-20T10:00:00",
            "payment_time": "2026-10-20T10:05:00", "referral_code": f"CODE{n}", "amount": 2.5, "currency": "EUR",
            "modified_amount": 9.5, "order_status": "success", "status": "completed",
            "referrer": {"id": "r1", "email": "owner@example.com"},
            "referred": {"id": str(USER_ID), "email": "buyer@example.com"}, **extra}


def _sheet(content: bytes):
    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
    return list(workbook[workbook.sheetnames[0]].iter_rows(values_only=True))


@pytest.fixture
def repo():
    return MagicMock(spec=ReferralAdminRepo)


@pytest.fixture
def audit():
    return MagicMock(name="audit-service")


@pytest.fixture
def svc(repo, audit):
    return ReferralAdminService(repo=repo, audit=audit)


# ---------- filters ----------

def test_filters_rpc_params_and_audit_filters_drop_blanks():
    filters = AdminReferralFilters(user_id=USER_ID, search="  abc ", from_email="   ", date=date(2026, 10, 20))

    assert filters.rpc_params() == {"p_user_id": str(USER_ID), "p_search": "abc", "p_from_email": None,
                                    "p_date": "2026-10-20"}
    assert filters.audit_filters() == {"user_id": str(USER_ID), "search": "abc", "date": "2026-10-20"}
    assert AdminReferralFilters().audit_filters() == {}


def test_export_request_forbids_unknown_fields():
    with pytest.raises(ValueError):
        AdminReferralExportRequest(page=1)


# ---------- list ----------

def test_list_shapes_rows_and_passes_page(svc, repo):
    repo.search.return_value = Paged(rows=[_row(1), _row(2, referrer=None, referred={"id": "u2", "email": None})],
                                     total=7)
    filters = AdminReferralFilters(search="code")
    page = PageParams(page=2, page_size=5)

    result = svc.list(filters, page)

    repo.search.assert_called_once_with(filters, 5, 5)
    assert result.total_count == 7 and result.facets is None
    assert result.data[0]["referrer"] == {"id": "r1", "email": "owner@example.com"}
    assert result.data[0]["referred"] == {"id": str(USER_ID), "email": "buyer@example.com"}
    assert result.data[0]["amount"] == 2.5 and result.data[0]["referral_code"] == "CODE1"
    assert result.data[1]["referrer"] is None
    assert result.data[1]["referred"] == {"id": "u2", "email": None}


# ---------- export ----------

def test_export_builds_xlsx_with_headers_rows_and_audit(svc, repo, audit, monkeypatch):
    monkeypatch.setenv("ADMIN_EXPORT_ROW_CAP", "50000")
    repo.search.return_value = Paged(rows=[_row(1), _row(2)], total=2)
    filters = AdminReferralFilters(user_id=USER_ID, search="code", date=date(2026, 10, 20))

    export = svc.export(filters, CTX)

    repo.search.assert_called_once_with(filters, EXPORT_CHUNK_SIZE, 0)
    assert export.row_count == 2
    assert export.file_name.startswith("referrals-") and export.file_name.endswith(".xlsx")
    rows = _sheet(export.content)
    assert rows[0] == tuple(header for header, _ in EXPORT_COLUMNS)
    assert rows[1] == ("CODE1", 2.5, "EUR", 9.5, "buyer@example.com", "owner@example.com",
                       "0a0a0a0a-0000-0000-0000-000000000001", "success", "completed",
                       "2026-10-20T10:00:00", "2026-10-20T10:05:00")
    assert len(rows) == 3
    audit.log.assert_called_once()
    args, kwargs = audit.log.call_args
    assert args == (CTX, "user_order", "EXPORT")
    assert kwargs["rows_affected"] == 2
    assert kwargs["filters"] == {"user_id": str(USER_ID), "search": "code", "date": "2026-10-20"}
    assert kwargs["file_name"] == export.file_name


def test_export_reads_in_chunks_of_1000(svc, repo, audit):
    repo.search.side_effect = [
        Paged(rows=[_row(n) for n in range(1000)], total=2300),
        Paged(rows=[_row(n) for n in range(1000, 2000)], total=2300),
        Paged(rows=[_row(n) for n in range(2000, 2300)], total=2300),
    ]
    filters = AdminReferralFilters()

    export = svc.export(filters, CTX)

    assert [call.args for call in repo.search.call_args_list] == [
        (filters, 1000, 0), (filters, 1000, 1000), (filters, 1000, 2000)]
    assert export.row_count == 2300
    assert len(_sheet(export.content)) == 2301
    assert audit.log.call_args.kwargs["rows_affected"] == 2300


def test_export_over_the_cap_is_422_too_many_rows_and_not_audited(svc, repo, audit, monkeypatch):
    monkeypatch.setenv("ADMIN_EXPORT_ROW_CAP", "10")
    repo.search.return_value = Paged(rows=[_row(n) for n in range(11)], total=11)

    with pytest.raises(AdminValidationError) as err:
        svc.export(AdminReferralFilters(), CTX)

    assert json.loads(err.value.details) == {"code": "too_many_rows", "total": 11, "cap": 10}
    assert repo.search.call_count == 1  # only the first chunk is read
    audit.log.assert_not_called()


def test_export_at_the_cap_is_allowed(svc, repo, audit, monkeypatch):
    monkeypatch.setenv("ADMIN_EXPORT_ROW_CAP", "2")
    repo.search.return_value = Paged(rows=[_row(1), _row(2)], total=2)

    assert svc.export(AdminReferralFilters(), CTX).row_count == 2


def test_export_neutralises_formula_cells(svc, repo):
    repo.search.return_value = Paged(rows=[
        _row(1, referral_code="=HYPERLINK(\"http://evil\")", referrer={"id": "r", "email": "+cmd@example.com"},
             referred={"id": "u", "email": "-x@example.com"}, status="@SUM(1)", currency="\tEUR"),
    ], total=1)

    rows = _sheet(svc.export(AdminReferralFilters(), CTX).content)

    assert rows[1][0] == "'=HYPERLINK(\"http://evil\")"
    assert rows[1][4] == "'-x@example.com"
    assert rows[1][5] == "'+cmd@example.com"
    assert rows[1][8] == "'@SUM(1)"
    assert rows[1][2] == "'\tEUR"
    assert rows[1][1] == 2.5  # numbers stay numbers


def test_export_with_no_rows_has_headers_only(svc, repo, audit):
    repo.search.return_value = Paged(rows=[], total=0)

    export = svc.export(AdminReferralFilters(), CTX)

    assert export.row_count == 0
    assert _sheet(export.content) == [tuple(header for header, _ in EXPORT_COLUMNS)]
    assert audit.log.call_args.kwargs["rows_affected"] == 0


def test_export_audit_failure_propagates(svc, repo, audit):
    repo.search.return_value = Paged(rows=[_row(1)], total=1)
    audit.log.side_effect = AdminUpstreamError("DATABASE_ERROR")

    with pytest.raises(AdminUpstreamError):
        svc.export(AdminReferralFilters(), CTX)


@pytest.mark.parametrize("value, expected", [
    ("=1+1", "'=1+1"), ("+1", "'+1"), ("-1", "'-1"), ("@x", "'@x"), ("\tx", "'\tx"), ("\rx", "'\rx"),
    ("plain", "plain"), ("a=b", "a=b"), ("", ""), (None, None), (-1, -1), (2.5, 2.5), (True, True),
])
def test_neutralise(value, expected):
    assert neutralise(value) == expected


@pytest.mark.parametrize("env, expected", [(None, 50000), ("123", 123), ("abc", 50000), ("0", 1)])
def test_export_row_cap_reads_env(monkeypatch, env, expected):
    if env is None:
        monkeypatch.delenv("ADMIN_EXPORT_ROW_CAP", raising=False)
    else:
        monkeypatch.setenv("ADMIN_EXPORT_ROW_CAP", env)
    assert export_row_cap() == expected


def test_export_file_name_is_time_stamped():
    from datetime import datetime
    assert export_file_name(datetime(2026, 10, 21, 12, 0, 0)) == "referrals-20261021-120000.xlsx"


# ---------- repository ----------

def _repo_with_client():
    repo = ReferralAdminRepo.__new__(ReferralAdminRepo)
    repo.table_name = "user_order"
    repo.client = MagicMock()
    return repo


def test_repo_calls_rpc_with_the_agreed_parameter_names():
    repo = _repo_with_client()
    repo.client.rpc.return_value.execute.return_value.data = {"data": [_row(1)], "total": 42}
    filters = AdminReferralFilters(user_id=USER_ID, search="abc", from_email="buyer@", date=date(2026, 10, 20))

    result = repo.search(filters, 20, 40)

    repo.client.rpc.assert_called_once_with("admin_referral_transactions", {
        "p_user_id": str(USER_ID), "p_search": "abc", "p_from_email": "buyer@", "p_date": "2026-10-20",
        "p_limit": 20, "p_offset": 40,
    })
    assert result.total == 42 and result.rows[0]["referral_code"] == "CODE1"


def test_repo_accepts_a_list_wrapped_result_and_empty_results():
    repo = _repo_with_client()
    repo.client.rpc.return_value.execute.return_value.data = [{"data": [_row(1)], "total": 1}]
    assert repo.search(AdminReferralFilters(), 10, 0).total == 1

    repo.client.rpc.return_value.execute.return_value.data = None
    assert repo.search(AdminReferralFilters(), 10, 0) == Paged(rows=[], total=0)


def test_repo_maps_ad422_to_validation_error():
    repo = _repo_with_client()
    repo.client.rpc.return_value.execute.side_effect = APIError(
        {"code": "AD422", "message": "p_limit must be 1..1000 and p_offset >= 0", "details": None, "hint": None})

    with pytest.raises(AdminValidationError):
        repo.search(AdminReferralFilters(), 5000, 0)
