import os
import secrets
from io import BytesIO

import qrcode
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader, Template
from loguru import logger
from pydantic import EmailStr
from supabase import create_client, Client
from supabase.lib.client_options import SyncClientOptions

from app.config.helper import get_config
from app.services.integration.dcb_service import DCBService
from app.services.integration.esim_hub_service import EsimHubService

ROOT_PATH = os.path.abspath(os.curdir)
env = os.getenv("ENVIRONMENT")
if not env:
    load_dotenv(f"{ROOT_PATH}/.env")
else:
    load_dotenv(f"{ROOT_PATH}/.env.{env}")

SUPABASE_URL: str = os.getenv("SUPABASE_URL")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY")
STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLIC_KEY = os.getenv("STRIPE_PUBLIC_KEY")

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = os.getenv("SMTP_PORT", 587)
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "false").lower() in ("true", "1", "yes")

USERNAME = os.getenv("SMTP_USERNAME", "<EMAIL>")
PASSWORD = os.getenv("SMTP_PASSWORD", "<PASSWORD>")

if not any([SUPABASE_URL, SUPABASE_KEY, STRIPE_PUBLIC_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_SECRET_KEY]):
    logger.error(
        "missing environment variables: SUPABASE_URL,SUPABASE_KEY,STRIPE_PUBLIC_KEY,STRIPE_WEBHOOK_SECRET,STRIPE_SECRET_KEY are required to run the project")


def validate_required_env_vars():
    required_vars = {
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_KEY": SUPABASE_KEY,
        "STRIPE_PUBLIC_KEY": STRIPE_PUBLIC_KEY,
        "STRIPE_WEBHOOK_SECRET": STRIPE_WEBHOOK_SECRET,
        "STRIPE_SECRET_KEY": STRIPE_SECRET_KEY
    }

    missing_vars = [var for var, value in required_vars.items() if not value]
    if missing_vars:
        error_msg = f"Missing required environment variables: {', '.join(missing_vars)}"
        logger.error(error_msg)
        raise EnvironmentError(error_msg)


validate_required_env_vars()


def supabase_client(url: str = None, key: str = None) -> Client:
    if url and key:
        return create_client(url, key,
                             options=SyncClientOptions(auto_refresh_token=False))
    return create_client(SUPABASE_URL, SUPABASE_KEY,
                         options=SyncClientOptions(auto_refresh_token=False))


def esim_hub_service_instance():
    return EsimHubService(
        base_url=os.getenv("ESIM_HUB_BASE_URL"),
        api_key=get_config("ESIM_HUB_API_KEY"),
        tenant_key=os.getenv("ESIM_HUB_TENANT_KEY"))


def dcb_service_instance() -> DCBService:
    provider = get_config("DEFAULT_DCB_PROVIDER", "NONE").upper()
    if provider == "MONTY":
        from app.services.integration.monty_dcb import MontyDCBService
        return MontyDCBService(base_url=os.getenv("DCB_SEND_OTP_URL", ""))
    elif provider == "DCB_HUB":
        from app.services.integration.hub_dcb_service import HubDcbService
        return HubDcbService()
    return DCBService(send_otp_url=os.getenv("DCB_SEND_OTP_URL", ""), verify_otp_url="", api_key="", charge_url="")


def authenticate(email: str | EmailStr, data: dict):
    return supabase_client().auth.sign_in_with_otp(credentials={
        "email": email,
        "options": {
            "data": data
        }
    })


def send_email(subject: str, html_content: str, recipients: str, attachment: BytesIO = None):
    """
    Send an email with optional attachment.
    
    Args:
        subject (str): Email subject
        html_content (str): HTML content of the email
        recipients (str): Comma-separated list of recipient email addresses
        attachment (BytesIO, optional): Optional attachment to include in the email
        
    Raises:
        ValueError: If required email configuration is missing
        smtplib.SMTPException: If there's an error sending the email
    """
    if not all([SMTP_SERVER, SMTP_PORT, USERNAME, PASSWORD]):
        raise ValueError("Missing required email configuration")

    import smtplib
    from email.utils import formatdate, formataddr
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email import encoders

    try:
        # Create message
        msg = MIMEMultipart('mixed')  # Use 'mixed' for attachments
        msg['Subject'] = subject
        sender_email = get_config("SMTP_SENDER", "noreply@esim.com")
        sender_name = get_config("SMTP_SENDER_NAME", "Esim Support")
        msg['From'] = formataddr((sender_name, sender_email))
        msg['To'] = recipients
        msg['Date'] = formatdate(localtime=True)

        # Add text and HTML content as a subpart
        alt_part = MIMEMultipart('alternative')
        text_content = "Please view this email in an HTML-compatible email client."
        alt_part.attach(MIMEText(text_content, 'plain'))
        alt_part.attach(MIMEText(html_content, 'html'))
        msg.attach(alt_part)

        # Add attachment if provided
        if attachment:
            attachment.seek(0)
            img = MIMEBase('image', 'png')
            img.set_payload(attachment.read())
            encoders.encode_base64(img)
            img.add_header('Content-Disposition', 'attachment', filename='qr_code.png')
            msg.attach(img)
        logger.info(f"opening SMTP connection to {SMTP_SERVER}:{SMTP_PORT}")
        # Send email
        if SMTP_USE_TLS:
            with smtplib.SMTP_SSL(SMTP_SERVER, int(SMTP_PORT), timeout=10) as server:
                server.login(USERNAME, PASSWORD)
                server.send_message(msg)
                logger.info(f"Email sent successfully to {recipients}")
        else:
            with smtplib.SMTP(SMTP_SERVER, int(SMTP_PORT), timeout=10) as server:
                server.starttls()
                server.login(USERNAME, PASSWORD)
                server.send_message(msg)
                logger.info(f"Email sent successfully to {recipients}")
    except smtplib.SMTPException as e:
        logger.error(f"Failed to send email: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error while sending email: {str(e)}")
        raise


def generate_qr_code(qr_data: str) -> BytesIO:
    qr = qrcode.make(qr_data)
    buffer = BytesIO()
    qr.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def generate_otp():
    return str(secrets.randbelow(900000) + 100000)


def get_email_template(template_name: str) -> Template | None:
    """
    Load an email template from the template's directory.
    """
    try:
        env = Environment(loader=FileSystemLoader(os.getenv("EMAIL_TEMPLATES_PATH", "app/email_templates")))
        return env.get_template(template_name)
    except Exception as e:
        logger.error(f"Error loading email template {template_name}: {str(e)}")
        return None
