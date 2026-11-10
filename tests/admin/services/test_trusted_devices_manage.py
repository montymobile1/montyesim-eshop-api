"""TEOS-255 (BE-2): trusted device rows, revocation, the 5-device limit, emails and audit (service level).

No database and no network: an in-memory twin of ``AdminDeviceRepo``, a fake audit, a fake mailer and a
synchronous "background" runner.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.exceptions.admin import AdminNotFound
from app.repo.admin.base import AuditContext
from app.repo.admin.trusted_device_repo import iso
from app.schemas.admin.trusted_devices import AdminDeviceInfo
from app.services.admin.admin_account_service import AdminAccountService
from app.services.admin.trusted_device_service import (MAX_TRUSTED, REASON_ACCESS_REVOKED, REASON_SIGN_OUT_OTHERS,
                                                       REASON_SUPER_ADMIN, REASON_TRUSTED_LIMIT, REASON_USER,
                                                       TRUST_DAYS, TrustedDeviceService, smtp_configured)
from tests.admin.conftest import make_auth_user

ADMIN_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
KEY = "0b0c7d5e-4a1f-4c1e-9d4e-2f8a6c1b3e7d"
CTX = AuditContext(actor_id=ADMIN_ID, ip="10.0.0.1")


def info(browser="Chrome", os_name="Windows", key=KEY):
    return AdminDeviceInfo(device_key=key, browser=browser, browser_version="130.0", os=os_name,
                           os_version="10.0", cpu_cores=8, memory_gb=8, touch_points=0, gpu="ANGLE",
                           language="en-US", timezone="Asia/Beirut", screen="1920x1080", color_depth=24)


class FakeDeviceRepo:
    """In-memory twin of AdminDeviceRepo (same method names and answers)."""

    def __init__(self, clock):
        self.clock = clock
        self.rows = {}
        self.ended = []
        self.end_fails = False

    def add(self, admin_id=ADMIN_ID, device_hash=None, trusted=True, session_id=None, used_days_ago=0,
            name=None, revoked=False, browser="Chrome", ip="1.1.1.1"):
        row_id = str(uuid.uuid4())
        used = self.clock() - timedelta(days=used_days_ago)
        self.rows[row_id] = {
            "id": row_id, "admin_id": admin_id, "device_hash": device_hash or f"hash-{row_id}",
            "device_info": {"browser": browser, "browser_version": "1", "os": "Windows", "os_version": "11",
                            "timezone": "UTC", "screen": "1x1", "language": "en", "color_depth": 24},
            "name": name, "trusted_until": iso(self.clock() + timedelta(days=10)) if trusted else None,
            "session_id": session_id, "first_ip": ip, "last_ip": ip, "created_at": iso(used),
            "last_used_at": iso(used), "revoked_at": iso(self.clock()) if revoked else None,
            "revoked_by": None, "revoke_reason": None}
        return self.rows[row_id]

    def _active(self, admin_id):
        return [r for r in self.rows.values() if r["admin_id"] == admin_id and r["revoked_at"] is None]

    def active_by_hash(self, admin_id, device_hash):
        return next((dict(r) for r in self._active(admin_id) if r["device_hash"] == device_hash), None)

    def by_session(self, session_id):
        rows = [r for r in self.rows.values() if r["session_id"] == session_id]
        rows.sort(key=lambda r: (r["revoked_at"] is not None, -(datetime.fromisoformat(r["revoked_at"]).timestamp()
                                                               if r["revoked_at"] else 0)))
        return dict(rows[0]) if rows else None

    def list_active(self, admin_id, limit=100):
        rows = sorted(self._active(admin_id), key=lambda r: r["last_used_at"], reverse=True)
        return [dict(r) for r in rows[:limit]]

    def get_active(self, admin_id, device_id):
        row = self.rows.get(device_id)
        return dict(row) if row and row["admin_id"] == admin_id and row["revoked_at"] is None else None

    def trusted_active(self, admin_id):
        now = self.clock()
        rows = [r for r in self._active(admin_id)
                if r["trusted_until"] and datetime.fromisoformat(r["trusted_until"]) > now]
        return [dict(r) for r in sorted(rows, key=lambda r: r["last_used_at"])]

    def insert(self, row):
        row_id = str(uuid.uuid4())
        stored = {"id": row_id, "name": None, "created_at": iso(self.clock()), "revoked_at": None,
                  "revoked_by": None, "revoke_reason": None, **row}
        self.rows[row_id] = stored
        return dict(stored)

    def update(self, device_id, patch):
        if device_id not in self.rows:
            return None
        self.rows[device_id].update(patch)
        return dict(self.rows[device_id])

    def mark_revoked(self, device_ids, revoked_by, reason):
        out = []
        for device_id in device_ids:
            row = self.rows.get(device_id)
            if row and row["revoked_at"] is None:
                row.update(revoked_at=iso(self.clock()), revoked_by=revoked_by, revoke_reason=reason)
                out.append(dict(row))
        return out

    def record_token_iat(self, device_id, iat):
        row = self.rows.get(device_id)
        if row is None or (row.get("last_token_iat") is not None and row["last_token_iat"] >= iat):
            return None
        row["last_token_iat"] = iat
        return dict(row)

    def end_session(self, session_id):
        if self.end_fails:
            raise RuntimeError("db down")
        self.ended.append(session_id)
        return {"sessions": 1, "refresh_tokens": 1}


class FakeAudit:
    def __init__(self, fail=False):
        self.rows = []
        self.fail = fail

    def log(self, ctx, table, operation, **kwargs):
        if self.fail:
            raise RuntimeError("audit down")
        self.rows.append(SimpleNamespace(ctx=ctx, table=table, operation=operation, **kwargs))

    def actions(self):
        return [row.action for row in self.rows]


class FakeMailer:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def __call__(self, subject, html_content, recipient):
        if self.fail:
            raise RuntimeError("smtp down")
        self.sent.append(SimpleNamespace(subject=subject, html=html_content, to=recipient))


@pytest.fixture
def clock():
    return lambda: NOW


@pytest.fixture
def repo(clock):
    return FakeDeviceRepo(clock)


@pytest.fixture
def audit():
    return FakeAudit()


@pytest.fixture
def mailer():
    return FakeMailer()


def make_service(repo, audit, mailer, clock, smtp=True, background=None, renderer=None):
    kwargs = {}
    if renderer is not None:
        kwargs["renderer"] = renderer
    return TrustedDeviceService(repo_factory=lambda: repo, audit_factory=lambda: audit, mailer=mailer,
                                background=background or (lambda job: job()), smtp_check=lambda: smtp,
                                clock=clock, **kwargs)


@pytest.fixture
def service(repo, audit, mailer, clock):
    return make_service(repo, audit, mailer, clock)


def assert_no_secrets(audit_rows):
    for row in audit_rows:
        text = repr(vars(row))
        assert KEY not in text and "device_hash" not in text and "code" not in text.replace("code_", "")


# ---------- sign-in hooks (used by BE-1) ----------

def test_find_trusted_only_answers_rows_still_trusted(service, repo):
    trusted = repo.add(device_hash="h1", trusted=True)
    repo.add(device_hash="h2", trusted=False)
    expired = repo.add(device_hash="h3", trusted=True)
    expired["trusted_until"] = iso(NOW - timedelta(seconds=1))

    assert service.find_trusted(ADMIN_ID, "h1")["id"] == trusted["id"]
    assert service.find_trusted(ADMIN_ID, "h2") is None
    assert service.find_trusted(ADMIN_ID, "h3") is None
    assert service.find_trusted(OTHER_ID, "h1") is None
    assert service.find_trusted(ADMIN_ID, "unknown") is None


def test_signed_in_trusted_moves_the_row_to_the_new_session_and_ends_the_old_one(service, repo, audit, mailer):
    row = repo.add(session_id="old-session", trusted=True)

    updated = service.signed_in_trusted(row, "new-session", "9.9.9.9", info(browser="Firefox"))

    assert updated["session_id"] == "new-session"
    assert updated["last_ip"] == "9.9.9.9"
    assert updated["last_used_at"] == iso(NOW)
    assert updated["last_sign_in_at"] == iso(NOW)  # the Admins page "Last sign-in"
    assert updated["device_info"]["browser"] == "Firefox"
    assert "device_key" not in updated["device_info"]
    assert repo.ended == ["old-session"]
    assert audit.actions() == ["device_signed_in"]
    assert mailer.sent == []


def test_register_verified_trusted_creates_the_row_for_30_days(service, repo, audit, mailer):
    row = service.register_verified(ADMIN_ID, "admin@example.com", "h1", info(), "s1", "5.5.5.5", True,
                                    "  My   laptop ", CTX)

    stored = repo.rows[row["id"]]
    assert stored["last_sign_in_at"] == iso(NOW)
    assert stored["trusted_until"] == iso(NOW + timedelta(days=TRUST_DAYS))
    assert stored["session_id"] == "s1"
    assert stored["first_ip"] == stored["last_ip"] == "5.5.5.5"
    assert stored["name"] == "My laptop"
    assert audit.actions() == ["device_verified"]
    assert audit.rows[0].trusted is True and audit.rows[0].operation == "INSERT"
    assert len(mailer.sent) == 1


def test_register_verified_without_trust_stores_no_trust_and_updates_the_existing_row(service, repo):
    existing = repo.add(device_hash="h1", trusted=True, session_id="old", name="Kept")

    row = service.register_verified(ADMIN_ID, "admin@example.com", "h1", info(), "s2", "6.6.6.6", False, None, CTX)

    assert row["id"] == existing["id"]
    assert repo.rows[existing["id"]]["trusted_until"] is None
    assert repo.rows[existing["id"]]["name"] == "Kept"
    assert repo.rows[existing["id"]]["first_ip"] == "1.1.1.1"
    assert repo.ended == ["old"]
    assert len(repo.rows) == 1


def test_session_device_move_and_clear(service, repo):
    row = repo.add(session_id="s1")
    revoked = repo.add(session_id="s2", revoked=True)

    assert service.session_device("s1")["id"] == row["id"]
    assert service.session_device("s2")["id"] == revoked["id"]
    assert service.session_device("nope") is None
    assert service.session_device("") is None

    service.move_session(row["id"], "s3")
    assert repo.rows[row["id"]]["session_id"] == "s3"

    service.clear_session("s3")
    assert repo.rows[row["id"]]["session_id"] is None
    assert repo.rows[row["id"]]["trusted_until"] is not None


def test_touch_sets_last_use_and_ip_without_audit(service, repo, audit):
    row = repo.add(used_days_ago=3, ip="1.1.1.1")

    service.touch(row["id"], "2.2.2.2")

    assert repo.rows[row["id"]]["last_used_at"] == iso(NOW)
    assert repo.rows[row["id"]]["last_ip"] == "2.2.2.2"
    assert repo.rows[row["id"]].get("last_sign_in_at") is None  # a refresh is not a sign-in
    assert audit.rows == []


def test_touch_never_raises(service, repo):
    def broken(*args):
        raise RuntimeError("db down")

    repo.update = broken

    service.touch(str(uuid.uuid4()), "2.2.2.2")


def test_one_repo_per_service_instance(repo, audit, mailer, clock):
    built = []
    service = TrustedDeviceService(repo_factory=lambda: built.append(1) or repo, audit_factory=lambda: audit,
                                   mailer=mailer, background=lambda job: job(), smtp_check=lambda: True,
                                   clock=clock)
    assert built == []  # lazy: nothing built at import / construction
    repo.add(session_id="s1")

    for _ in range(3):
        service.session_device("s1")
    service.list_devices(ADMIN_ID)
    service.touch(str(uuid.uuid4()), None)

    assert built == [1]


# ---------- case 5: GET /me/devices ----------

def test_case5_list_marks_the_callers_row_current(service, repo):
    mine = repo.add(session_id="me", used_days_ago=1)
    other = repo.add(session_id="other", trusted=False, used_days_ago=0)
    idle = repo.add(session_id=None, used_days_ago=3)
    repo.add(revoked=True)
    repo.add(admin_id=OTHER_ID)

    rows = service.list_devices(ADMIN_ID, "me")

    assert [r.id for r in rows] == [other["id"], mine["id"], idle["id"]]
    by_id = {r.id: r for r in rows}
    assert by_id[mine["id"]].current is True and by_id[mine["id"]].trusted is True
    assert by_id[other["id"]].current is False and by_id[other["id"]].trusted is False
    assert by_id[idle["id"]].signed_in is False and by_id[mine["id"]].signed_in is True
    assert by_id[mine["id"]].browser == "Chrome" and by_id[mine["id"]].last_ip == "1.1.1.1"


# ---------- case 6: rename ----------

def test_case6_rename_collapses_spaces_and_audits(service, repo, audit):
    row = repo.add(name="Old", session_id="me")

    renamed = service.rename(ADMIN_ID, row["id"], "  Work   PC ", CTX, current_session_id="me")

    assert renamed.name == "Work PC" and renamed.current is True
    assert repo.rows[row["id"]]["name"] == "Work PC"
    assert audit.rows[0].action == "device_renamed"
    assert audit.rows[0].old == {"name": "Old"} and audit.rows[0].new == {"name": "Work PC"}


# ---------- cases 7 / 31: revoke ends the session ----------

def test_case7_revoke_marks_the_row_and_ends_its_session(service, repo, audit):
    row = repo.add(session_id="victim")

    service.revoke(ADMIN_ID, row["id"], REASON_USER, CTX)

    stored = repo.rows[row["id"]]
    assert stored["revoked_at"] is not None and stored["revoke_reason"] == REASON_USER
    assert stored["revoked_by"] == ADMIN_ID
    assert repo.ended == ["victim"]
    # the per-request check sees the revoked row and answers device_revoked
    assert service.session_device("victim")["revoked_at"] is not None
    assert audit.rows[0].action == "device_revoked" and audit.rows[0].reason == REASON_USER
    assert_no_secrets(audit.rows)


def test_case31_end_session_failure_keeps_the_row_revoked(service, repo):
    row = repo.add(session_id="victim")
    repo.end_fails = True

    service.revoke(ADMIN_ID, row["id"], REASON_USER, CTX)

    assert repo.rows[row["id"]]["revoked_at"] is not None
    assert service.session_device("victim")["revoked_at"] is not None


def test_revoke_audit_failure_does_not_undo_the_revoke(repo, mailer, clock):
    service = make_service(repo, FakeAudit(fail=True), mailer, clock)
    row = repo.add(session_id="victim")

    service.revoke(ADMIN_ID, row["id"], REASON_USER, CTX)

    assert repo.rows[row["id"]]["revoked_at"] is not None


# ---------- case 8: sign out all other devices ----------

def test_case8_revoke_all_keeps_the_current_device(service, repo, audit):
    mine = repo.add(session_id="me")
    a = repo.add(session_id="a")
    b = repo.add(session_id=None)
    other_admin = repo.add(admin_id=OTHER_ID, session_id="x")

    count = service.revoke_all(ADMIN_ID, REASON_SIGN_OUT_OTHERS, CTX, except_device_id=mine["id"])

    assert count == 2
    assert repo.rows[mine["id"]]["revoked_at"] is None
    assert repo.rows[a["id"]]["revoked_at"] and repo.rows[b["id"]]["revoked_at"]
    assert repo.rows[other_admin["id"]]["revoked_at"] is None
    assert repo.ended == ["a"]
    assert audit.actions() == ["devices_revoked"]
    assert audit.rows[0].count == 2 and audit.rows[0].reason == REASON_SIGN_OUT_OTHERS


def test_revoke_all_with_nothing_to_revoke_writes_no_audit(service, repo, audit):
    mine = repo.add(session_id="me")

    assert service.revoke_all(ADMIN_ID, REASON_SIGN_OUT_OTHERS, CTX, except_device_id=mine["id"]) == 0
    assert audit.rows == []


# ---------- case 9 / 23: another admin's device ----------

def test_case9_super_admin_lists_and_revokes_another_admins_device(service, repo, audit):
    theirs = repo.add(admin_id=OTHER_ID, session_id="their-session")

    rows = service.list_devices(OTHER_ID, "my-session")
    assert [r.id for r in rows] == [theirs["id"]] and rows[0].current is False

    service.revoke(OTHER_ID, theirs["id"], REASON_SUPER_ADMIN, CTX)
    assert repo.rows[theirs["id"]]["revoke_reason"] == REASON_SUPER_ADMIN
    assert repo.rows[theirs["id"]]["revoked_by"] == ADMIN_ID
    assert repo.ended == ["their-session"]
    assert audit.rows[0].by == ADMIN_ID and audit.rows[0].admin_id == OTHER_ID


def test_case23_device_of_another_admin_is_404(service, repo):
    theirs = repo.add(admin_id=OTHER_ID, session_id="x")
    revoked = repo.add(revoked=True)

    for call in (lambda: service.revoke(ADMIN_ID, theirs["id"], REASON_USER, CTX),
                 lambda: service.rename(ADMIN_ID, theirs["id"], "x", CTX),
                 lambda: service.revoke(ADMIN_ID, revoked["id"], REASON_USER, CTX),
                 lambda: service.revoke(ADMIN_ID, str(uuid.uuid4()), REASON_USER, CTX)):
        with pytest.raises(AdminNotFound) as error:
            call()
        assert '"code": "device_not_found"' in error.value.details
    assert repo.rows[theirs["id"]]["revoked_at"] is None
    assert repo.ended == []


# ---------- case 10: 5-device limit ----------

def test_case10_sixth_trusted_device_revokes_the_least_recently_used(service, repo, audit, mailer):
    olds = [repo.add(session_id=f"s{days}", used_days_ago=days, name=f"PC {days}") for days in (1, 2, 9, 3, 4)]
    untrusted = repo.add(trusted=False, used_days_ago=20)

    row = service.register_verified(ADMIN_ID, "admin@example.com", "new", info(browser="Edge"), "s-new",
                                    "7.7.7.7", True, None, CTX)

    lru = olds[2]
    assert repo.rows[lru["id"]]["revoked_at"] is not None
    assert repo.rows[lru["id"]]["revoke_reason"] == REASON_TRUSTED_LIMIT
    assert repo.ended == ["s9"]
    assert sum(1 for r in olds if repo.rows[r["id"]]["revoked_at"] is None) == 4
    assert repo.rows[untrusted["id"]]["revoked_at"] is None
    assert len(repo.trusted_active(ADMIN_ID)) == MAX_TRUSTED
    assert repo.rows[row["id"]]["revoked_at"] is None
    assert audit.actions() == ["device_verified", "device_revoked"]
    assert audit.rows[1].reason == REASON_TRUSTED_LIMIT
    subjects = [mail.subject for mail in mailer.sent]
    assert len(subjects) == 2
    removed = next(mail for mail in mailer.sent if "removed" in mail.subject)
    assert "PC 9" in removed.html and "Edge on Windows" in removed.html


def test_trusting_a_device_with_four_others_revokes_nothing(service, repo, mailer):
    for days in range(4):
        repo.add(used_days_ago=days)

    service.register_verified(ADMIN_ID, "admin@example.com", "new", info(), "s", "1.1.1.1", True, None, CTX)

    assert all(r["revoked_at"] is None for r in repo.rows.values())
    assert len(mailer.sent) == 1


def test_untrusted_sign_in_never_applies_the_limit(service, repo):
    for days in range(5):
        repo.add(used_days_ago=days)

    service.register_verified(ADMIN_ID, "admin@example.com", "new", info(), "s", "1.1.1.1", False, None, CTX)

    assert all(r["revoked_at"] is None for r in repo.rows.values())


# ---------- case 11: new-device email ----------

def test_case11_new_device_email_has_browser_os_ip_and_time(service, mailer):
    service.register_verified(ADMIN_ID, "admin@example.com", "h", info(browser="Firefox", os_name="macOS"),
                              "s", "8.8.4.4", True, None, CTX)

    assert len(mailer.sent) == 1
    mail = mailer.sent[0]
    assert mail.to == "admin@example.com"
    for text in ("Firefox 130.0", "macOS 10.0", "8.8.4.4", "2026-11-05 10:00 UTC"):
        assert text in mail.html
    assert KEY not in mail.html
    assert "eSIM" not in mail.html


def test_email_values_from_the_browser_are_html_escaped(service, mailer):
    service.register_verified(ADMIN_ID, "admin@example.com", "h", info(browser="<script>x</script>"), "s",
                              "1.1.1.1", False, None, CTX)

    assert "<script>" not in mailer.sent[0].html
    assert "&lt;script&gt;" in mailer.sent[0].html


def test_no_email_when_smtp_is_not_configured(repo, audit, mailer, clock):
    service = make_service(repo, audit, mailer, clock, smtp=False)
    for days in range(5):
        repo.add(used_days_ago=days + 1)

    service.register_verified(ADMIN_ID, "admin@example.com", "h", info(), "s", "1.1.1.1", True, None, CTX)

    assert mailer.sent == []
    assert sum(1 for r in repo.rows.values() if r["revoked_at"]) == 1


def test_smtp_configured_needs_username_and_password(monkeypatch):
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    assert smtp_configured() is False
    monkeypatch.setenv("SMTP_USERNAME", "user")
    assert smtp_configured() is False
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    assert smtp_configured() is True


def test_mailer_failure_does_not_fail_the_sign_in(repo, audit, clock):
    service = make_service(repo, audit, FakeMailer(fail=True), clock)

    row = service.register_verified(ADMIN_ID, "admin@example.com", "h", info(), "s", "1.1.1.1", True, None, CTX)

    assert repo.rows[row["id"]]["session_id"] == "s"


def test_background_runner_failure_does_not_fail_the_sign_in(repo, audit, mailer, clock):
    def broken(job):
        raise RuntimeError("no thread")

    service = make_service(repo, audit, mailer, clock, background=broken)

    row = service.register_verified(ADMIN_ID, "admin@example.com", "h", info(), "s", "1.1.1.1", True, None, CTX)

    assert row["session_id"] == "s"


def test_email_is_sent_in_the_background(repo, audit, mailer, clock):
    jobs = []
    service = make_service(repo, audit, mailer, clock, background=jobs.append)

    service.register_verified(ADMIN_ID, "admin@example.com", "h", info(), "s", "1.1.1.1", True, None, CTX)

    assert mailer.sent == [] and len(jobs) == 1
    jobs[0]()
    assert len(mailer.sent) == 1


def test_templates_render_with_the_real_files(repo, audit, mailer, clock):
    """The real ``admin_*.htm`` templates (default renderer) render every value."""
    service = TrustedDeviceService(repo_factory=lambda: repo, audit_factory=lambda: audit, mailer=mailer,
                                   background=lambda job: job(), smtp_check=lambda: True, clock=clock)
    for days in range(5):
        repo.add(used_days_ago=days + 1, name=f"Old {days}")

    service.register_verified(ADMIN_ID, "admin@example.com", "h", info(), "s", "4.4.4.4", True, None, CTX)

    assert len(mailer.sent) == 2
    new_mail = next(mail for mail in mailer.sent if "New sign-in" in mail.subject)
    removed = next(mail for mail in mailer.sent if "removed" in mail.subject)
    assert "4.4.4.4" in new_mail.html and "Chrome 130.0" in new_mail.html
    assert "Old 4" in removed.html and "{{" not in removed.html


def test_emails_have_the_logo_header(repo, audit, mailer, clock, monkeypatch):
    """Like the customer emails: {BASE_URL}/logo/logo.png at the top; no broken image when BASE_URL is unknown."""
    import app.services.admin.trusted_device_service as module
    service = TrustedDeviceService(repo_factory=lambda: repo, audit_factory=lambda: audit, mailer=mailer,
                                   background=lambda job: job(), smtp_check=lambda: True, clock=clock)
    monkeypatch.setattr(module, "_brand", lambda: {"base_url": "https://esim.example.com", "esim_name": "Monty eSIM"})
    service.register_verified(ADMIN_ID, "admin@example.com", "h1", info(), "s1", "4.4.4.4", False, None, CTX)
    assert 'src="https://esim.example.com/logo/logo.png"' in mailer.sent[-1].html

    monkeypatch.setattr(module, "_brand", lambda: {"base_url": "", "esim_name": "Monty eSIM"})
    service.register_verified(ADMIN_ID, "admin@example.com", "h2", info(), "s2", "4.4.4.4", False, None, CTX)
    assert "logo.png" not in mailer.sent[-1].html


# ---------- case 12: one audit row per event ----------

def test_case12_one_audit_row_per_event_and_no_secret(service, repo, audit):
    row = service.register_verified(ADMIN_ID, "admin@example.com", "h", info(), "s1", "1.1.1.1", True, None, CTX)
    service.signed_in_trusted(row, "s2", "1.1.1.1", info())
    service.rename(ADMIN_ID, row["id"], "Laptop", CTX)
    other = repo.add(session_id="o")
    service.revoke(ADMIN_ID, other["id"], REASON_USER, CTX)
    repo.add(session_id="p")
    service.revoke_all(ADMIN_ID, REASON_SIGN_OUT_OTHERS, CTX, except_device_id=row["id"])

    assert audit.actions() == ["device_verified", "device_signed_in", "device_renamed", "device_revoked",
                               "devices_revoked"]
    assert all(r.table == "admin_devices" for r in audit.rows)
    assert_no_secrets(audit.rows)


# ---------- remove admin (TEOS-307 revoke access) revokes every device ----------

class FakeAccountAudit:
    def __init__(self):
        self.rows = []

    def log(self, ctx, table, operation, **kwargs):
        self.rows.append(kwargs)


def account_service(device_revoker):
    client = MagicMock(name="supabase-client")
    client.auth.admin.get_user_by_id.return_value = SimpleNamespace(user=make_auth_user(user_id=OTHER_ID))
    return AdminAccountService(client_factory=lambda: client, audit_factory=FakeAccountAudit,
                               device_revoker=device_revoker)


def test_revoke_access_revokes_every_device_of_the_admin(repo, audit, mailer, clock):
    devices = make_service(repo, audit, mailer, clock)
    a = repo.add(admin_id=OTHER_ID, session_id="a")
    b = repo.add(admin_id=OTHER_ID, session_id="b", trusted=False)

    result = account_service(lambda user_id, ctx: devices.revoke_all(user_id, REASON_ACCESS_REVOKED, ctx)) \
        .revoke_access(OTHER_ID, CTX)

    assert result.previous_role is not None
    assert repo.rows[a["id"]]["revoke_reason"] == repo.rows[b["id"]]["revoke_reason"] == REASON_ACCESS_REVOKED
    assert sorted(repo.ended) == ["a", "b"]
    assert audit.rows[0].action == "devices_revoked" and audit.rows[0].count == 2


def test_revoke_access_still_succeeds_when_the_devices_cannot_be_revoked():
    def broken(user_id, ctx):
        raise RuntimeError("db down")

    result = account_service(broken).revoke_access(OTHER_ID, CTX)

    assert result.id == OTHER_ID


def test_admin_account_allows_the_callers_own_account():
    client = MagicMock(name="supabase-client")
    client.auth.admin.get_user_by_id.return_value = SimpleNamespace(user=make_auth_user(user_id=ADMIN_ID))
    accounts = AdminAccountService(client_factory=lambda: client, device_revoker=lambda *a: 0)

    assert str(accounts.admin_account(ADMIN_ID).id) == ADMIN_ID

    client.auth.admin.get_user_by_id.return_value = SimpleNamespace(user=make_auth_user(role=None))
    with pytest.raises(AdminNotFound):
        accounts.admin_account(ADMIN_ID)


# ---------- last_token_iat (refresh outside the API) and the repo's conditional updates ----------


def test_record_token_keeps_the_max_and_ignores_none(service, repo):
    row = repo.add(session_id="s1")

    service.record_token(row["id"], 2000)
    service.record_token(row["id"], 1500)  # stored late, out of order
    service.record_token(row["id"], None)

    assert repo.rows[row["id"]]["last_token_iat"] == 2000
    service.record_token(row["id"], 2600)
    assert repo.rows[row["id"]]["last_token_iat"] == 2600


def test_record_token_raises_when_the_store_fails(repo, audit, mailer, clock):
    broken = MagicMock(name="repo")
    broken.record_token_iat.side_effect = RuntimeError("db down")
    service = make_service(broken, audit, mailer, clock)

    with pytest.raises(RuntimeError):
        service.record_token("d1", 10)


@pytest.fixture
def db_table(monkeypatch):
    client = MagicMock(name="db")
    monkeypatch.setattr("app.repo.base_repo.supabase_client", lambda: client)
    return client.table.return_value


def test_repo_record_token_iat_only_raises_the_value(db_table):
    from app.repo.admin.trusted_device_repo import AdminDeviceRepo
    query = db_table.update.return_value.eq.return_value.or_.return_value
    query.execute.return_value = SimpleNamespace(data=[{"id": "d1", "last_token_iat": 1234}])

    assert AdminDeviceRepo().record_token_iat("d1", 1234)["last_token_iat"] == 1234
    db_table.update.assert_called_once_with({"last_token_iat": 1234})
    db_table.update.return_value.eq.assert_called_once_with("id", "d1")
    db_table.update.return_value.eq.return_value.or_.assert_called_once_with(
        "last_token_iat.is.null,last_token_iat.lt.1234")

    query.execute.return_value = SimpleNamespace(data=[])  # a higher value is already stored
    assert AdminDeviceRepo().record_token_iat("d1", 1000) is None


def test_repo_fail_open_kills_only_open_verifications_of_the_admin(db_table):
    from app.repo.admin.trusted_device_repo import AdminDeviceVerificationRepo
    chain = db_table.update.return_value.eq.return_value
    final = chain.is_.return_value.is_.return_value.gt.return_value
    final.execute.return_value = SimpleNamespace(data=[{"id": "c1"}, {"id": "c2"}])

    assert AdminDeviceVerificationRepo().fail_open(ADMIN_ID) == 2
    assert list(db_table.update.call_args.args[0]) == ["failed_at"]
    db_table.update.return_value.eq.assert_called_once_with("admin_id", ADMIN_ID)
    assert chain.is_.call_args.args == ("consumed_at", "null")
    assert chain.is_.return_value.is_.call_args.args == ("failed_at", "null")
    assert chain.is_.return_value.is_.return_value.gt.call_args.args[0] == "expires_at"
