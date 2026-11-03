"""QA bugs of 2026-10-26 (backend, batch A) at service and schema level: TEOS-148, 150, 153, 165, 166, 210,
211, 214, 215."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.exceptions.admin import AdminNotFound
from app.repo.admin.base import AuditContext
from app.repo.admin.promotion_rule_repo import RULE_SELECT
from app.repo.admin.user_repo import UserAdminRepo, UserWalletAdminRepo
from app.schemas.admin.bundles import AdminBundleBulkStatus, AdminBundleStatusPatch
from app.schemas.admin.common import PageParams
from app.schemas.admin.promotion_rules import AdminPromotionRuleBody
from app.schemas.admin.users import AdminUserBan, AdminUserUnban
from app.schemas.admin.vouchers import AdminVoucherCreate
from app.services.admin.promotion_rule_service import shape_rule
from app.services.admin.user_service import UserAdminService
from app.services.admin.voucher_service import VoucherAdminService, _is_expired, end_of_day

USER_ID = "33333333-3333-3333-3333-333333333333"
CTX = AuditContext(actor_id="11111111-1111-1111-1111-111111111111", ip="1.2.3.4", reason="fraud")
PAGE = PageParams(page=1, page_size=10)


# ---------- TEOS-148 / TEOS-166: reason 3..500 after trimming ----------

REASON_MODELS = [
    lambda reason: AdminUserBan(reason=reason),
    lambda reason: AdminUserUnban(reason=reason),
    lambda reason: AdminBundleStatusPatch(is_active=False, reason=reason),
    lambda reason: AdminBundleBulkStatus(is_active=False, reason=reason),
]


@pytest.mark.parametrize("make", REASON_MODELS)
@pytest.mark.parametrize("reason", ["", "a", "ab", "   ", "  ab  ", "x" * 501])
def test_reason_outside_3_to_500_after_trimming_is_refused(make, reason):
    with pytest.raises(ValidationError):
        make(reason)


@pytest.mark.parametrize("make", REASON_MODELS)
def test_reason_of_3_and_500_characters_is_accepted_and_trimmed(make):
    assert make("  abc  ").reason == "abc"
    assert make("x" * 500).reason == "x" * 500


def test_reason_message_names_the_minimum():
    with pytest.raises(ValidationError) as exc:
        AdminUserBan(reason="ab")
    assert "at least 3 characters" in str(exc.value)


@pytest.mark.parametrize("model", [AdminBundleStatusPatch, AdminBundleBulkStatus])
def test_bundle_activation_reason_stays_optional_but_short_one_is_refused(model):
    assert model(is_active=True).reason is None
    with pytest.raises(ValidationError):
        model(is_active=True, reason="ok")
    with pytest.raises(ValidationError):
        model(is_active=False)  # still required to deactivate


# ---------- TEOS-150 / TEOS-153: user service ----------

@pytest.fixture
def repo():
    return MagicMock(spec=UserAdminRepo)


@pytest.fixture
def audit():
    return MagicMock()


@pytest.fixture
def devices():
    return MagicMock()


@pytest.fixture
def svc(repo, audit, devices):
    client = MagicMock(name="supabase-client")
    client.auth.admin.update_user_by_id.return_value = SimpleNamespace(
        user=SimpleNamespace(id=USER_ID, email="jane@example.com",
                             banned_until=datetime(2027, 1, 1, tzinfo=timezone.utc)))
    return UserAdminService(repo=repo, wallets=MagicMock(spec=UserWalletAdminRepo), devices=devices, audit=audit,
                            client_factory=lambda: client, session_revoker=MagicMock())


def _user(**extra):
    return {"id": USER_ID, "email": "jane@example.com", "is_banned": False, "banned_until": None, **extra}


def test_ban_audit_row_has_previous_banned_until_null_for_a_never_banned_user(svc, repo, audit):
    repo.get.return_value = _user()

    svc.ban(USER_ID, AdminUserBan(reason="fraud"), CTX)

    kwargs = audit.log.call_args.kwargs
    assert "previous_banned_until" in kwargs and kwargs["previous_banned_until"] is None
    assert kwargs["banned_until"] == "2027-01-01T00:00:00+00:00"


def test_ban_audit_row_keeps_the_lapsed_ban_end_as_previous_banned_until(svc, repo, audit):
    # TEOS-150 retest 2026-10-27: an earlier ban that already ended is still stored, not nulled
    repo.get.return_value = _user(banned_until="2020-01-01T00:00:00+00:00")

    svc.ban(USER_ID, AdminUserBan(reason="fraud"), CTX)

    assert audit.log.call_args.kwargs["previous_banned_until"] == "2020-01-01T00:00:00+00:00"


def test_ban_audit_row_keeps_a_lapsed_ban_end_given_as_datetime(svc, repo, audit):
    repo.get.return_value = _user(banned_until=datetime(2021, 5, 4, 3, 2, 1, tzinfo=timezone.utc))

    svc.ban(USER_ID, AdminUserBan(reason="fraud"), CTX)

    assert audit.log.call_args.kwargs["previous_banned_until"] == "2021-05-04T03:02:01+00:00"


def test_ban_revoke_retry_row_carries_the_running_ban_as_previous_banned_until(svc, repo, audit):
    until = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    repo.get.return_value = _user(is_banned=True, banned_until=until)
    repo.latest_ban_audit.return_value = {"id": 7, "operation_details": {"hours_banned": 24, "ban_duration": "24h",
                                                                         "sessions_revoked": False}}

    svc.ban(USER_ID, AdminUserBan(reason="retry"), CTX)

    kwargs = audit.log.call_args.kwargs
    assert kwargs["revoke_retry"] is True and kwargs["previous_banned_until"] == until


@pytest.mark.parametrize("call", [
    lambda svc: svc.list_devices(USER_ID, PAGE),
    lambda svc: svc.list_wallet_transactions(USER_ID, PAGE),
    lambda svc: svc.list_referrals(USER_ID, PAGE),
])
def test_user_sub_lists_of_an_unknown_user_are_404(svc, repo, devices, call):
    repo.get.return_value = None

    with pytest.raises(AdminNotFound) as exc:
        call(svc)

    assert exc.value.details == "user_not_found"
    devices.list.assert_not_called()
    repo.wallet_transactions.assert_not_called()
    repo.referrals.assert_not_called()


def test_user_sub_lists_of_a_user_without_rows_are_empty(svc, repo, devices):
    from app.repo.admin.base import Paged
    from app.schemas.admin.common import AdminPage
    repo.get.return_value = _user()
    devices.list.return_value = AdminPage(data=[], total_count=0)
    repo.wallet_transactions.return_value = Paged(rows=[], total=0)
    repo.referrals.return_value = Paged(rows=[], total=0)

    assert svc.list_devices(USER_ID, PAGE).total_count == 0
    assert svc.list_wallet_transactions(USER_ID, PAGE).data == []
    assert svc.list_referrals(USER_ID, PAGE).data == []


# ---------- TEOS-210: promotion_count ----------

def test_rule_select_counts_the_promotions_in_the_same_query():
    assert "promotion(count)" in RULE_SELECT


def test_shape_rule_carries_promotion_count_for_used_and_unused_rules():
    used = shape_rule({"id": "r1", "promotion": [{"count": 28}]})
    unused = shape_rule({"id": "r2", "promotion": [{"count": 0}]})
    legacy = shape_rule({"id": "r3"})

    assert used["promotion_count"] == 28
    assert unused["promotion_count"] == 0
    assert legacy["promotion_count"] == 0


# ---------- TEOS-211 / TEOS-215: strict numbers ----------

RULE_BODY = {"promotion_rule_action_id": 1, "promotion_rule_event_id": 1, "max_usage": 5, "beneficiary": 1}


@pytest.mark.parametrize("value", ["5", 5.0, True, 0, 2_147_483_648])
def test_max_usage_must_be_a_json_integer_in_range(value):
    with pytest.raises(ValidationError):
        AdminPromotionRuleBody.model_validate_json(_json({**RULE_BODY, "max_usage": value}))


def test_max_usage_integer_is_accepted():
    assert AdminPromotionRuleBody.model_validate_json(_json(RULE_BODY)).max_usage == 5


@pytest.mark.parametrize("value", ["5", "5.5", True, 0, -1])
def test_voucher_amount_must_be_a_positive_json_number(value):
    with pytest.raises(ValidationError):
        AdminVoucherCreate.model_validate_json(_json({"amount": value}))


@pytest.mark.parametrize("value", [5, 5.5, 0.01])
def test_voucher_amount_number_is_accepted(value):
    assert AdminVoucherCreate.model_validate_json(_json({"amount": value})).amount == value


def _json(data) -> str:
    import json
    return json.dumps(data)


# ---------- TEOS-214: a voucher expiring today is valid until the end of the day ----------

def test_end_of_day_is_the_last_microsecond_of_the_day():
    assert end_of_day(date(2026, 10, 26)) == "2026-10-26T23:59:59.999999"


def test_voucher_created_for_today_is_stored_at_the_end_of_today_and_is_not_expired():
    repo, settings = MagicMock(), MagicMock()
    settings.value_of.return_value = None
    repo.insert_audited.side_effect = lambda row, ctx: {"id": 1, "is_used": False, "is_active": True, **row}
    svc = VoucherAdminService(repo=repo, users=MagicMock(), settings=settings)
    today = datetime.now(timezone.utc).date()

    result = svc.create(AdminVoucherCreate(code="TODAY1", amount=1, expired_at=today.isoformat()), CTX)

    stored = repo.insert_audited.call_args.args[0]["expired_at"]
    assert stored == f"{today.isoformat()}T23:59:59.999999"
    assert result["is_expired"] is False
    assert _is_expired({"is_used": False, "expired_at": stored}) is False


def test_voucher_stored_at_midnight_before_today_is_still_expired():
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    assert _is_expired({"is_used": False, "expired_at": end_of_day(yesterday)}) is True


def test_voucher_with_yesterday_as_expiry_is_still_refused():
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    with pytest.raises(ValidationError):
        AdminVoucherCreate(amount=1, expired_at=yesterday.isoformat())


# ---------- TEOS-165: customer contact-us ----------

@pytest.fixture
def app_service():
    with patch("app.services.app_service.BannerRepo"), patch("app.services.app_service.ConfigRepo"), \
            patch("app.services.app_service.esim_hub_service_instance"), \
            patch("app.services.app_service.ContactUsRepo"), patch("app.services.app_service.DeviceRepo"):
        from app.services.app_service import AppService
        service = AppService()
    contact_repo = MagicMock()
    contact_repo.create.return_value = {"id": 1}
    service._AppService__contact_us_repo = contact_repo
    return service, contact_repo


def test_contact_us_stores_the_message_as_typed_and_escapes_it_in_the_support_email(app_service):
    from app.schemas.app import ContactUsRequest
    service, contact_repo = app_service
    content = 'Tom & Jerry <a href="https://evil.example">click</a> <img src=x>'
    request = ContactUsRequest(email="o'neil@example.com", content=content)

    with patch("app.services.app_service.send_email") as send_email, \
            patch("app.services.app_service.get_config", return_value="-"):
        import asyncio
        asyncio.run(service.contact_us(request))

    contact_repo.create.assert_called_once_with({"email": "o'neil@example.com", "content": content})
    html_body = send_email.call_args.kwargs["html_content"]
    assert "Tom &amp; Jerry &lt;a href=&quot;https://evil.example&quot;&gt;click&lt;/a&gt; &lt;img src=x&gt;" in html_body
    assert "<a href" not in html_body and "<img" not in html_body
    assert "o&#x27;neil@example.com" in html_body
    assert "&amp;amp;" not in html_body  # escaped once, from the original values
