import os
import secrets
from io import BytesIO

import qrcode
import stripe
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader, Template
from loguru import logger
from stripe import PaymentIntent, Charge
from supabase import create_client, Client
from supabase.lib.client_options import SyncClientOptions

from app.exceptions import CustomException
from app.models.user import UserOrderModel
from app.schemas.bundle import PaymentDetailsDTO
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

stripe.api_key = STRIPE_SECRET_KEY


def supabase_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY,
                         options=SyncClientOptions(auto_refresh_token=False))


def esim_hub_service_instance():
    return EsimHubService(
        base_url=os.getenv("ESIM_HUB_BASE_URL"),
        api_key=os.getenv("ESIM_HUB_API_KEY"),
        tenant_key=os.getenv("ESIM_HUB_TENANT_KEY"))


def dcb_service_instance():
    return DCBService(send_otp_url="", verify_otp_url="", api_key="", charge_url="")


def authenticate(email: str, referral_code: str):
    return supabase_client().auth.sign_in_with_otp(credentials={
        "email": email,
        "options": {
            "data": {
                "referral_code": referral_code,
                "display_name": email,
            }
        }
    })


def create_payment_intent(user_bundle_order: UserOrderModel, user_email: str,
                          metadata: dict, ip_address: str = None) -> PaymentIntent:
    try:
        logger.info(f"Creating payment intent for request: {user_bundle_order}")
        customers = stripe.Customer.list(email=user_email)
        if not customers:
            customer = stripe.Customer.create(email=user_email)
        else:
            customer = customers.get("data")[0]
        order_amount = user_bundle_order.modified_amount if user_bundle_order.modified_amount else user_bundle_order.amount
        if os.getenv("STRIPE_AUTOMATIC_TAX", "false").lower() in ("true", "1", "yes"):
            logger.info(f"Automatic tax calculation enabled, calculating tax for amount {order_amount}")
            tax = calculate_tax(currency=user_bundle_order.currency, amount=order_amount,
                                tax_code=os.getenv("STRIPE_TAX_CODE", "txcd_10103101"),
                                tax_behavior="inclusive", request_ip=ip_address,
                                reference=f"bundle:{user_bundle_order.bundle_id}")
            if tax:
                order_amount = tax.amount_total
                metadata = {**metadata, "tax_calculation": tax.id},

                logger.info(
                    f"applying tax calculation: {tax.id} for order {user_bundle_order.id} with amount {order_amount}")
        payment_intent = stripe.PaymentIntent.create(
            amount=order_amount,
            currency=user_bundle_order.currency,
            payment_method_types=["card"],
            description=f"Bundle order ({user_bundle_order.order_type}) for bundle {user_bundle_order.bundle_id}",
            metadata=metadata,
            customer=customer.id
        )
        return payment_intent

    except stripe.error.StripeError as e:
        raise CustomException(code=400, name="Payment Intent Exception",
                              details=f"Error while creating payment intent {str(e)}")


def calculate_tax(currency: str, amount: float, reference: str, tax_code: str,
                  tax_behavior: str, request_ip: str = None) -> stripe.tax.Calculation | None:
    try:
        calc = stripe.tax.Calculation.create(
            currency=currency,
            line_items=[{
                "amount": int(amount),  # smallest currency unit
                "reference": reference,
                "tax_code": tax_code,
                "tax_behavior": tax_behavior,  # "exclusive" => tax added on top
            }],
            customer_details={
                # "address": customer_address,
                # "address_source": "billing",  # or "billing" depending on your flow
                "ip_address": request_ip,
            }
        )
        return calc
    except stripe.error.StripeError as e:
        logger.error(f"Error calculating tax: {str(e)}")
        return None


def create_wallet_top_up_intent(user_email: str, amount: float, currency: str, metadata: dict) -> PaymentIntent:
    try:
        logger.info("Creating payment intent for wallet top-up")
        customers = stripe.Customer.list(email=user_email)
        if not customers:
            customer = stripe.Customer.create(email=user_email)
        else:
            customer = customers.get("data")[0]
        payment_intent = stripe.PaymentIntent.create(
            amount=amount,
            currency=currency,
            payment_method_types=["card"],
            description=f"Topup for user {user_email} for amount {amount} {currency}",
            metadata=metadata,
            customer=customer.id
        )
        logger.debug(f"Payment intent:  {payment_intent}")
        return payment_intent

    except stripe.error.StripeError as e:
        raise CustomException(code=400, name="Payment Intent Exception",
                              details=f"Error while creating payment intent {str(e)}")


def create_payment_ephemeral(customer_id: str):
    try:
        ephemeral = stripe.EphemeralKey.create(
            customer=customer_id,
            stripe_version='2024-09-30.acacia',
        )
        logger.info("Ephemeral created: %s", ephemeral)
        return ephemeral
    except stripe.error.StripeError as e:
        raise CustomException(code=400, name="Ephemeral Creation Exception",
                              details=f"Error while creating ephemeral key: {str(e)}")


def stripe_get_payment_details(intent_code) -> PaymentDetailsDTO | None:
    if not intent_code:
        return None
    payment: PaymentIntent = stripe.PaymentIntent.retrieve(intent_code)
    if not payment.latest_charge:
        return None
    charge_id = payment.latest_charge
    charge: Charge = stripe.Charge.retrieve(charge_id)
    address_details = charge.billing_details.address
    if address_details:
        address = (address_details.get("country") or "N/A") + "," + (address_details.get("postal_code") or "N/A")
    else:
        address = "N/A"
    card_number = charge.payment_method_details.card.get("last4")
    card_type = charge.payment_method_details.get("type")
    card_brand = charge.payment_method_details.card.get("brand").title()
    card_display = f"{card_brand} ****{card_number}"
    return PaymentDetailsDTO.model_validate({
        "id": payment.id,
        "description": "",
        "payment_method": card_type,
        "card_number": card_number,
        "receipt_email": charge.receipt_email,
        "address": address,
        "card_display": card_display,
        "display_brand": card_brand,
        "country": charge.payment_method_details.card.get("country"),
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
        sender_email = os.getenv("SMTP_SENDER", "noreply@esim.com")
        sender_name = os.getenv("SMTP_SENDER_NAME", "Esim Support")
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
