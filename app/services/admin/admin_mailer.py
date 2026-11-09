"""Admin portal emails, sent through the API's own SMTP (``SMTP_*``), never through Supabase's sender.

Used by the sign-in code (``DeviceVerificationService``), the password reset link (``AdminAccountService``) and
the device notifications (``TrustedDeviceService``). The admin emails are in English only.

- ``smtp_configured``: the SMTP account is set (``SMTP_USERNAME`` and ``SMTP_PASSWORD``). Without it the
  sign-in code and the reset link are unavailable (503); the device notifications are skipped.
- the logo header comes from the app settings ``BASE_URL`` / ``ESIM_NAME``, like the customer emails.
"""
import html
import os
from typing import Any, Dict, Optional

#: ``app/email_templates`` of this checkout, used when ``EMAIL_TEMPLATES_PATH`` is not set (or empty)
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             "email_templates")


def smtp_configured() -> bool:
    """The API mailer is used only when the SMTP account is set (config.py has placeholder defaults)."""
    return bool((os.getenv("SMTP_USERNAME") or "").strip() and (os.getenv("SMTP_PASSWORD") or "").strip())


def brand() -> Dict[str, str]:
    """The logo header of the emails, like the customer emails: ``{base_url}/logo/logo.png`` and the eSIM name, from
    the app settings (``BASE_URL``, ``ESIM_NAME``), else the environment."""
    def setting(key: str, default: str) -> str:
        try:
            from app.config.helper import get_config
            value = get_config(key, None)
        except Exception:
            value = None
        return str(value or os.getenv(key) or default)
    return {"base_url": html.escape(setting("BASE_URL", "").rstrip("/")),
            "esim_name": html.escape(setting("ESIM_NAME", "Monty eSIM"))}


def render(template_name: str, data: Dict[str, Any]) -> Optional[str]:
    """Like ``config.get_email_template`` (same folder setting) but an empty setting falls back to the
    templates of this checkout. The values are HTML-escaped by the caller."""
    from jinja2 import Environment, FileSystemLoader
    folder = (os.getenv("EMAIL_TEMPLATES_PATH") or "").strip() or TEMPLATES_DIR
    return Environment(loader=FileSystemLoader(folder)).get_template(template_name).render(data=data)


def deliver(subject: str, html_content: str, recipient: str) -> None:
    """One email through the API's SMTP (``app.config.config.send_email``). Raises when it cannot be sent."""
    from app.config.config import send_email
    send_email(subject=subject, html_content=html_content, recipients=recipient)


def send_now(template: str, subject: str, recipient: str, data: Dict[str, Any]) -> None:
    """Renders the template (values HTML-escaped, logo header added) and sends it now. Raises when it cannot be
    rendered or sent: the caller answers with an error."""
    safe = {key: html.escape(value) if isinstance(value, str) else value for key, value in data.items()}
    content = render(template, {**brand(), **safe})
    if not content:
        raise RuntimeError(f"email template '{template}' could not be rendered")
    deliver(subject, content, recipient)
