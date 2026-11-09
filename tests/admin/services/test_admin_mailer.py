"""Admin portal emails through the API's SMTP (English only): templates, logo header, escaping, SMTP check."""
import pytest

from app.services.admin import admin_mailer
from app.services.admin.admin_account_service import SUBJECT_PASSWORD_RESET, TEMPLATE_PASSWORD_RESET
from app.services.admin.device_verification_service import SUBJECT_SIGN_IN_CODE, TEMPLATE_SIGN_IN_CODE
from app.services.admin.trusted_device_service import TEMPLATE_DEVICE_REMOVED, TEMPLATE_NEW_DEVICE

TEMPLATES = [TEMPLATE_SIGN_IN_CODE, TEMPLATE_PASSWORD_RESET, TEMPLATE_NEW_DEVICE, TEMPLATE_DEVICE_REMOVED]
DATA = {"code": "042917", "minutes": 10, "link": "https://p.supabase.co/auth/v1/verify?token=t&type=recovery",
        "device": "Chrome on Windows", "browser": "Chrome 130", "os": "Windows 10", "ip": "1.2.3.4",
        "time": "2026-11-09 10:00 UTC", "trusted": True, "max_trusted": 5, "new_device": "Edge on macOS"}


@pytest.fixture(autouse=True)
def templates_of_this_checkout(monkeypatch):
    monkeypatch.delenv("EMAIL_TEMPLATES_PATH", raising=False)
    monkeypatch.setattr(admin_mailer, "brand", lambda: {"base_url": "https://esim.example.com", "esim_name": "Monty"})


def test_every_admin_template_renders_with_the_logo_and_its_values():
    for template in TEMPLATES:
        html = admin_mailer.render(template, {**admin_mailer.brand(), **DATA})
        assert "https://esim.example.com/logo/logo.png" in html
        assert "{{" not in html and "{%" not in html
    assert "042917" in admin_mailer.render(TEMPLATE_SIGN_IN_CODE, DATA)
    assert DATA["link"].replace("&", "&amp;") in admin_mailer.render(TEMPLATE_PASSWORD_RESET, DATA)


def test_send_now_escapes_values_and_sends_the_email(monkeypatch):
    sent = []
    monkeypatch.setattr(admin_mailer, "deliver", lambda subject, html, to: sent.append((subject, html, to)))

    admin_mailer.send_now(TEMPLATE_PASSWORD_RESET, SUBJECT_PASSWORD_RESET, "amy@x.com",
                          {"link": 'https://x/verify?a=1&b="2"'})

    ((subject, html, to),) = sent
    assert to == "amy@x.com" and subject == SUBJECT_PASSWORD_RESET
    assert 'href="https://x/verify?a=1&amp;b=&#34;2&#34;"' in html and 'b="2"' not in html


def test_send_now_raises_when_the_email_cannot_be_sent(monkeypatch):
    def down(*args):
        raise OSError("smtp down")

    monkeypatch.setattr(admin_mailer, "deliver", down)
    with pytest.raises(OSError):
        admin_mailer.send_now(TEMPLATE_SIGN_IN_CODE, SUBJECT_SIGN_IN_CODE, "a@x.com", {"code": "1", "minutes": 10})


@pytest.mark.parametrize("username, password, configured", [("", "", False), ("u", "", False), ("u", "p", True)])
def test_smtp_configured_needs_username_and_password(monkeypatch, username, password, configured):
    monkeypatch.setenv("SMTP_USERNAME", username)
    monkeypatch.setenv("SMTP_PASSWORD", password)
    assert admin_mailer.smtp_configured() is configured
