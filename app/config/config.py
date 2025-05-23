import os
import random
from io import BytesIO

import qrcode
import stripe
from dotenv import load_dotenv
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

# Gmail SMTP configuration
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = os.getenv("SMTP_PORT", 587)

# Login credentials
USERNAME = os.getenv("SMTP_USERNAME", "<EMAIL>")
PASSWORD = os.getenv("SMTP_PASSWORD", "<PASSWORD>")

if not any([SUPABASE_URL, SUPABASE_KEY, STRIPE_PUBLIC_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_SECRET_KEY]):
    logger.error(
        "missing environment variables: SUPABASE_URL,SUPABASE_KEY,STRIPE_PUBLIC_KEY,STRIPE_WEBHOOK_SECRET,STRIPE_SECRET_KEY are required to run the project")
    # sys.exit(1)

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
    send_otp_url = os.getenv("DCB_SEND_OTP_API")
    charge_url = os.getenv("DCB_CHARGE_API")
    verify_otp_url = os.getenv("DCB_VERIFY_CHARGE_API")
    return DCBService(send_otp_url=send_otp_url, charge_url=charge_url, verify_otp_url=verify_otp_url,
                      api_key=os.getenv("DCB_API_KEY"))


def authenticate(email: str, referral_code: str):
    return supabase_client().auth.sign_in_with_otp(credentials={
        "email": email,
        "options": {
            "data": {
                "referral_code": referral_code
            }
        }
    })


def create_payment_intent(user_bundle_order: UserOrderModel, user_email: str,
                          metadata: dict) -> PaymentIntent:
    try:
        logger.info(f"Creating payment intent for request: {user_bundle_order}")
        customers = stripe.Customer.list(email=user_email)
        if not customers:
            customer = stripe.Customer.create(email=user_email)
        else:
            customer = customers.get("data")[0]
        payment_intent = stripe.PaymentIntent.create(
            amount=user_bundle_order.amount,
            currency=user_bundle_order.currency,
            # receipt_email=user_email,
            payment_method_types=["card"],
            description=f"Bundle order ({user_bundle_order.order_type}) for bundle {user_bundle_order.bundle_id}",
            metadata=metadata,
            customer=customer.id
        )
        logger.debug(f"Payment intent:  {payment_intent}")
        return payment_intent

    except stripe.error.StripeError as e:
        raise CustomException(code=400, name="Payment Intent Exception",
                              details=f"Error while creating payment intent {str(e)}")


def create_wallet_top_up_intent(user_email: str, amount: float, currency: str, metadata: dict) -> PaymentIntent:
    try:
        logger.info(f"Creating payment intent for wallet top-up: ")
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
    import smtplib
    from email.message import EmailMessage

    # Email content
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = os.getenv("SMTP_SENDER", "noreply@esim.com")
    msg['To'] = recipients
    msg.set_content(html_content)
    msg.add_alternative(html_content, subtype='html')
    if attachment:
        msg.get_payload()[1].add_related(attachment.read(), maintype='image', subtype='png', cid='qr_code')

    # Sending the email
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()  # Secure the connection
            server.login(USERNAME, PASSWORD)
            server.send_message(msg)
            logger.info("Email sent successfully.")
    except Exception as e:
        logger.info(f"Failed to send email: {e}")


def generate_qr_code(qr_data: str) -> BytesIO:
    qr = qrcode.make(qr_data)
    buffer = BytesIO()
    qr.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def generate_otp():
    return str(random.randint(100000, 999999))
