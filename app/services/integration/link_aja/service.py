"""LinkAja provider service - the orchestration seam for the whole integration.

`UserBundleService` and `LinkAjaCallbackService` talk only to this class, using
only the neutral DTOs in `dto.py`. Neither of them knows that LinkAja speaks
HTTP, what its endpoints are, or what its status strings look like.

## Contract source

"LinkAja Applink SNAP" interface specification v1.1 (2025-05-08). Where the
older LinkAja "Applink SNAP" Postman collection disagrees with it, the
specification wins:

    create payment  §4.2  urlParams[] / type PAY_RETURN   (collection: urlParam / partnerApplink)
    inform payment  §4.3  inbound notify, answered with 2005600 + approvalCode
    inquiry status  §4.4  serviceCode "55"                (collection: "54")
    statuses        §5.4  00 success, 04 refunded, 06 failed, 07 not found
    signatures      §5.1  token RSA, service HMAC, notify RSA with LinkAja's key

## What still fails closed

Inbound notify verification needs LinkAja's public key
(`LINKAJA_NOTIFY_PUBLIC_KEY[_PATH]`). Until it is configured every notify is
rejected and nothing provisions.
"""

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from loguru import logger

from .amount import LINKAJA_CURRENCY, to_linkaja_amount
from .client import LinkAjaHttpClient
from .config import LinkAjaConfig
from .dto import (
    LinkAjaCallbackItem,
    LinkAjaCallbackResult,
    LinkAjaPaymentRequest,
    LinkAjaPaymentResult,
    LinkAjaPaymentStatus,
    LinkAjaStatusResult,
    map_provider_status,
)
from .order_data import LinkAjaOrderData
from .return_url import notify_url_for_order
from .exceptions import (
    LinkAjaCallbackInvalidException,
    LinkAjaCallbackVerificationException,
    LinkAjaConfigurationException,
    LinkAjaDisabledException,
    LinkAjaProviderException,
)
from .sanitize import redact
from .signature import (
    external_id,
    load_private_key,
    load_public_key,
    serialize_body,
    service_signature,
    snap_timestamp,
    token_signature,
    trx_date,
    verify_notify_signature,
)
from .token_cache import TokenCache, shared_token_cache

# SNAP service codes / response codes (spec v1.1). responseCode = HTTP status
# + service code + case code (§5.2).
STATUS_SERVICE_CODE = "55"                 # §4.4 inquiry status, p.17-18
CREATE_PAYMENT_SUCCESS_CODE = "2005400"    # §4.2
STATUS_SUCCESS_CODE = "2005500"            # §4.4
STATUS_NOT_FOUND_CODE = "4045501"          # §5.2 "Transaction Not Found" (HTTP 404)
NOTIFY_SUCCESS_CODE = "2005600"            # §4.3
NOTIFY_SUCCESS_MESSAGE = "Successful"
URL_PARAM_TYPE = "PAY_RETURN"              # §4.2 p.11

# The notify is a POST (§4.3); the method is part of the string-to-sign.
NOTIFY_METHOD = "POST"
NOTIFY_SIGNATURE_HEADER = "x-signature"
NOTIFY_TIMESTAMP_HEADER = "x-timestamp"


def _parse_expires_in(value: Any) -> Optional[float]:
    """`expiresIn` is a string of seconds (p.10: "899"). Unparseable -> None (no caching)."""
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def parse_amount(value: Any) -> Optional[Decimal]:
    """Decimal from a SNAP amount string: "6000", "6000.00" and 6000 all equal."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


class LinkAjaService:
    """Provider client for LinkAja.

    Constructing this is always safe and never touches the network or raises,
    even with no LinkAja configuration present - that is what keeps application
    startup and the Card / Wallet / DCB flows unaffected.
    """

    def __init__(self, config: Optional[LinkAjaConfig] = None,
                 client: Optional[LinkAjaHttpClient] = None,
                 token_cache: Optional[TokenCache] = None):
        self.__config = config or LinkAjaConfig.from_env()
        self.__client = client or LinkAjaHttpClient(self.__config)
        self.__token_cache = token_cache or shared_token_cache(self.__config.base_url,
                                                               self.__config.client_id)

    @property
    def config(self) -> LinkAjaConfig:
        return self.__config

    @property
    def client(self) -> LinkAjaHttpClient:
        return self.__client

    @property
    def token_cache(self) -> TokenCache:
        return self.__token_cache

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------

    def is_enabled(self) -> bool:
        return self.__config.enabled

    def ensure_enabled(self) -> None:
        """Fail closed unless this deployment has opted into LinkAja."""
        if not self.__config.enabled:
            raise LinkAjaDisabledException(
                details="Payment type LinkAja is not enabled on this deployment")

    def ensure_payments_available(self) -> None:
        """Enable-gate plus configuration validation, in that order.

        Order matters: a deployment that has not opted in should get the clean
        400 "not enabled", not a 503 about missing credentials it was never
        expected to have.
        """
        self.ensure_enabled()
        self.__config.validate_for_payment()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self) -> str:
        """Return a valid B2B access token, fetching one only when the cached
        token is missing or about to expire (spec p.10: 900 s lifetime)."""
        self.ensure_payments_available()
        return await self.__token_cache.get(self._fetch_token)

    async def _fetch_token(self) -> tuple[str, Optional[float]]:
        method, headers, body = self._build_auth_request()
        payload = await self.__client.request_json(
            method=method, path=self.__config.auth_path,
            operation="authentication", json_body=body, headers=headers)
        return self._parse_auth_response(payload), _parse_expires_in(payload.get("expiresIn"))

    def _build_auth_request(self):
        """§4.1 / §5.1.1: SHA256withRSA over "<clientKey>|<timestamp>"."""
        timestamp = snap_timestamp()
        headers = {
            "Content-Type": "application/json",
            "X-TIMESTAMP": timestamp,
            "X-CLIENT-KEY": self.__config.client_id,
            "X-SIGNATURE": token_signature(self._private_key(), self.__config.client_id, timestamp),
        }
        return "POST", headers, {"grantType": "client_credentials"}

    def _parse_auth_response(self, payload: dict) -> str:
        token = payload.get("accessToken")
        if not isinstance(token, str) or not token:
            raise LinkAjaProviderException(
                details=f"LinkAja authentication returned no access token "
                        f"(responseCode={payload.get('responseCode')})")
        return token

    def _private_key(self):
        pem = self.__config.private_key
        if not pem and self.__config.private_key_path:
            try:
                with open(self.__config.private_key_path, encoding="utf-8") as key_file:
                    pem = key_file.read()
            except OSError as exc:
                raise LinkAjaConfigurationException(
                    details="LinkAja private key file (LINKAJA_PRIVATE_KEY_PATH) could not be read") from exc
        return load_private_key(pem)

    def _notify_public_key(self):
        pem = self.__config.notify_public_key
        if not pem and self.__config.notify_public_key_path:
            try:
                with open(self.__config.notify_public_key_path, encoding="utf-8") as key_file:
                    pem = key_file.read()
            except OSError as exc:
                raise LinkAjaConfigurationException(
                    details="LinkAja notify public key file (LINKAJA_NOTIFY_PUBLIC_KEY_PATH) "
                            "could not be read") from exc
        return load_public_key(pem)

    # ------------------------------------------------------------------
    # Signed service calls
    # ------------------------------------------------------------------

    def _auth_headers(self, token: str, method: str, path: str, body: bytes) -> dict:
        """Headers for every non-token call (§4.2 / §4.4 / §4.5)."""
        timestamp = snap_timestamp()
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-TIMESTAMP": timestamp,
            "X-SIGNATURE": service_signature(self.__config.client_secret, method, path, token, body, timestamp),
            "X-PARTNER-ID": self.__config.partner_id,
            "X-EXTERNAL-ID": external_id(),
            "CHANNEL-ID": self.__config.channel_id,
        }

    async def _signed_post(self, path: str, body: dict, operation: str,
                           extra_headers: Optional[dict] = None,
                           tolerated_statuses: tuple[int, ...] = ()) -> dict:
        """POST a signed body. On a 401 (§5.2: invalid/expired token) the cached
        token is dropped and the call is retried once with a fresh one."""
        content = serialize_body(body)
        for attempt in (1, 2):
            token = await self.authenticate()
            headers = self._auth_headers(token, "POST", self._signing_path(path), content)
            headers.update(extra_headers or {})
            try:
                return await self.__client.request_json(
                    method="POST", path=path, operation=operation, json_body=body,
                    headers=headers, content=content, tolerated_statuses=tolerated_statuses)
            except LinkAjaProviderException as exc:
                if exc.status_code == 401 and attempt == 1:
                    logger.warning(f"[LinkAja] {operation} rejected the access token; "
                                   f"refreshing it and retrying once")
                    self.__token_cache.invalidate()
                    continue
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _signing_path(path: str) -> str:
        # §5.1.2: the signature covers the request path (EndpointUrl).
        return "/" + path.lstrip("/")

    # ------------------------------------------------------------------
    # Payment creation (§4.2)
    # ------------------------------------------------------------------

    async def create_payment(self, payment_request: LinkAjaPaymentRequest) -> LinkAjaPaymentResult:
        """Initiate an Applink payment and return the normalized result.

        Works unchanged for both Assign and Top-up orders: `merchant_order_id`
        is simply the `user_order.id` of whichever order type is being paid for,
        so enabling LinkAja top-ups later needs no change in this package.
        """
        self.ensure_payments_available()
        logger.info(f"[LinkAja] creating payment for order {payment_request.merchant_order_id} "
                    f"({payment_request.amount_minor_units} minor units {payment_request.currency})")

        # Built before authenticating so a conversion/config error costs no HTTP call.
        body = self._build_create_payment_body(payment_request)
        extra = {"User-Agent": self.__config.user_agent} if self.__config.user_agent else None
        payload = await self._signed_post(self.__config.create_payment_path, body,
                                          "payment creation", extra_headers=extra)
        result = self._parse_create_payment(payload)
        # Echo exactly what was asked for, so the caller can persist it and the
        # notify can be reconciled against it later.
        return result.model_copy(update={"amount": body["amount"]["value"],
                                         "usd_to_idr_rate": self.__config.usd_to_idr_rate})

    def _build_create_payment_body(self, payment_request: LinkAjaPaymentRequest) -> dict:
        """Request body of §4.2 (p.11-12). `urlParams` is the v1.1 name (p.2
        revision history). LinkAja confirmed its `url` is our notify endpoint
        (LINKAJA_NOTIFY_URL), with type PAY_RETURN and isDeeplink "N". The order
        id is appended (?order_id=...) because LinkAja also opens this URL to
        send the customer back; see return_url.py."""
        value = to_linkaja_amount(payment_request.amount_minor_units, payment_request.currency,
                                  self.__config.usd_to_idr_rate)
        return {
            "partnerReferenceNo": payment_request.merchant_order_id,
            "merchantId": self.__config.merchant_id,
            "terminalId": self.__config.terminal_id,
            "amount": {"value": value, "currency": LINKAJA_CURRENCY},
            "urlParams": [{
                "url": notify_url_for_order(payment_request.notify_url or self.__config.notify_url,
                                            payment_request.merchant_order_id),
                "type": URL_PARAM_TYPE,
                "isDeeplink": self.__config.url_param_is_deeplink,
            }],
            "additionalInfo": {
                "trxDate": trx_date(),
                "terminalName": self.__config.terminal_name,
                "items": [{
                    "id": "1",
                    "name": payment_request.description or self.__config.merchant_id,
                    "unitPrice": value,
                    "qty": "1",
                }],
            },
        }

    def _parse_create_payment(self, payload: dict) -> LinkAjaPaymentResult:
        """Response of §4.2 (p.12): responseCode 2005400, referenceNo, appRedirectUrl.
        Anything else fails closed."""
        code = payload.get("responseCode")
        reference = payload.get("referenceNo")
        redirect_url = payload.get("appRedirectUrl")
        if code != CREATE_PAYMENT_SUCCESS_CODE or not reference or not redirect_url:
            logger.error(f"[LinkAja] unexpected payment creation response: responseCode={code} "
                         f"responseMessage={payload.get('responseMessage')}")
            raise LinkAjaProviderException(
                details=f"LinkAja payment creation was not successful (responseCode={code})")
        # Created is not paid: the payment stays PENDING until verified.
        return LinkAjaPaymentResult(provider_reference=str(reference), payment_url=str(redirect_url),
                                    status=LinkAjaPaymentStatus.PENDING)

    # ------------------------------------------------------------------
    # Status verification (§4.4)
    # ------------------------------------------------------------------

    async def get_payment_status(self, merchant_order_id: str) -> LinkAjaStatusResult:
        """Inquiry status, looked up by OUR reference (`originalPartnerReferenceNo`
        = `user_order.id`), not LinkAja's.

        A 404 is a documented answer here (4045501 "Transaction Not Found"), not
        a transport failure, so it is tolerated and mapped to NOT_FOUND.
        """
        self.ensure_payments_available()
        body = {
            "originalPartnerReferenceNo": merchant_order_id,
            "serviceCode": STATUS_SERVICE_CODE,
            "merchantId": self.__config.merchant_id,
            "additionalInfo": {"terminalId": self.__config.terminal_id},
        }
        payload = await self._signed_post(self.__config.payment_status_path, body, "status verification",
                                          tolerated_statuses=(404,))
        return self._parse_status_response(payload, merchant_order_id)

    def _parse_status_response(self, payload: dict, merchant_order_id: str) -> LinkAjaStatusResult:
        """Response of §4.4 (p.18) plus the §5.4 status list.

        NOT_FOUND (responseCode 4045501, or latestTransactionStatus "07") means
        LinkAja has no completed transaction for our reference: for a created
        payment that is "not paid yet", so the caller must leave the order
        pending rather than fail it.
        """
        code = str(payload.get("responseCode") or "")
        if code == STATUS_NOT_FOUND_CODE:
            logger.info(f"[LinkAja] status inquiry for order {merchant_order_id}: transaction not found "
                        f"(not paid yet)")
            return LinkAjaStatusResult(merchant_order_id=merchant_order_id, provider_status="07",
                                       status=LinkAjaPaymentStatus.NOT_FOUND,
                                       status_description=str(payload.get("responseMessage") or ""))
        if code != STATUS_SUCCESS_CODE:
            logger.error(f"[LinkAja] unexpected status inquiry response: responseCode={code} "
                         f"responseMessage={payload.get('responseMessage')}")
            raise LinkAjaProviderException(
                details=f"LinkAja status inquiry was not successful (responseCode={code})")

        echoed = payload.get("originalPartnerReferenceNo")
        if echoed is not None and str(echoed) != merchant_order_id:
            logger.error(f"[LinkAja] status inquiry for order {merchant_order_id} answered for another "
                         f"reference")
            raise LinkAjaProviderException(
                details="LinkAja status inquiry answered for a different partner reference")

        raw_status = payload.get("latestTransactionStatus")
        status = map_provider_status(raw_status)
        if status == LinkAjaPaymentStatus.UNKNOWN:
            logger.error(f"[LinkAja] status inquiry for order {merchant_order_id} returned undocumented "
                         f"latestTransactionStatus={raw_status!r}")
        return LinkAjaStatusResult(
            merchant_order_id=merchant_order_id,
            provider_reference=str(payload.get("originalReferenceNo") or ""),
            status=status,
            provider_status=None if raw_status is None else str(raw_status),
            status_description=None if payload.get("transactionStatusDesc") is None
            else str(payload.get("transactionStatusDesc")),
            approval_code=None if payload.get("approvalCode") is None else str(payload.get("approvalCode")),
        )

    # ------------------------------------------------------------------
    # Callback handling (§4.3 Inform Payment)
    # ------------------------------------------------------------------

    def verify_callback(self, raw_body: bytes, headers: Mapping[str, str],
                        request_path: str = "", query_string: str = "",
                        request_url: str = "") -> LinkAjaCallbackResult:
        """Authenticate and parse an inbound LinkAja notify.

        Security-critical and fail-closed. An unverified callback must never
        reach provisioning, so this raises rather than returning an unverified
        result: there is no code path that yields a `LinkAjaCallbackResult` with
        `verified=False` from here.

        `request_path`, `query_string` and `request_url` describe the request
        LinkAja actually made. Unless `LINKAJA_NOTIFY_ENDPOINT_URL` pins the
        EndpointUrl, it is built from them in each form listed in
        `LINKAJA_NOTIFY_SIGNATURE_FORMS` (default: the path, no query string).
        """
        self.ensure_enabled()
        self.__config.validate_for_callback()
        self._verify_signature(raw_body, headers, request_path, query_string, request_url)
        return self._parse_callback_body(raw_body)

    def _endpoint_candidates(self, request_path: str, query_string: str, request_url: str) -> list:
        """(form, EndpointUrl) pairs to verify against, in the configured order.

        `full_url` takes scheme and host from LINKAJA_NOTIFY_URL (the URL we
        gave LinkAja), so a TLS-terminating proxy cannot change it; the request
        URL is only the fallback.
        """
        if self.__config.notify_endpoint_url:
            return [("pinned", self.__config.notify_endpoint_url)]
        if not request_path:
            return []
        with_query = f"{request_path}?{query_string}" if query_string else request_path
        origin = urlsplit(self.__config.notify_url or request_url)
        full_url = f"{origin.scheme}://{origin.netloc}{with_query}" if origin.scheme and origin.netloc else ""
        values = {"path": request_path, "path_query": with_query, "full_url": full_url}
        return [(form, values[form]) for form in self.__config.signature_forms() if values[form]]

    def _verify_signature(self, raw_body: bytes, headers: Mapping[str, str], request_path: str = "",
                          query_string: str = "", request_url: str = "") -> None:
        """§5.1.3: X-SIGNATURE = Base64(SHA256withRSA(privateKey,
        "POST:<EndpointUrl>:<lowerhex(sha256(minify(body)))>:<X-TIMESTAMP>")),
        checked against LinkAja's public key."""
        lowered = {str(k).lower(): v for k, v in headers.items()}
        signature = (lowered.get(NOTIFY_SIGNATURE_HEADER) or "").strip()
        timestamp = (lowered.get(NOTIFY_TIMESTAMP_HEADER) or "").strip()
        if not signature or not timestamp:
            logger.warning("[LinkAja] notify rejected: X-SIGNATURE or X-TIMESTAMP header missing")
            raise LinkAjaCallbackVerificationException(
                details="LinkAja notify is missing the X-SIGNATURE or X-TIMESTAMP header")

        candidates = self._endpoint_candidates(request_path, query_string, request_url)
        if not candidates:
            raise LinkAjaCallbackVerificationException(
                details="LinkAja notify EndpointUrl is unknown: LINKAJA_NOTIFY_ENDPOINT_URL is not set "
                        "and the request path was not supplied")

        public_key = self._notify_public_key()
        for form, endpoint_url in candidates:
            if verify_notify_signature(public_key, signature, NOTIFY_METHOD, endpoint_url, raw_body, timestamp):
                logger.info(f"[LinkAja] notify signature verified (EndpointUrl form={form})")
                return
        # Log the shape, never the signature: a logged signature is replayable.
        tried = ", ".join(f"{form}={endpoint_url}" for form, endpoint_url in candidates)
        logger.warning(f"[LinkAja] notify rejected: invalid signature (tried {tried}; "
                       f"timestamp={timestamp}, body={len(raw_body)} bytes)")
        raise LinkAjaCallbackVerificationException(details="LinkAja notify signature is invalid")

    def _parse_callback_body(self, raw_body: bytes) -> LinkAjaCallbackResult:
        """Body of §4.3 (p.14-16). Only authenticated bodies reach here."""
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise LinkAjaCallbackInvalidException(
                details="LinkAja notify body is not valid JSON",
                snap_response_code="4005600", snap_response_message="Bad Request") from exc
        if not isinstance(payload, dict):
            raise LinkAjaCallbackInvalidException(
                details="LinkAja notify body is not a JSON object",
                snap_response_code="4005600", snap_response_message="Bad Request")

        logger.debug(f"[LinkAja] notify payload={redact(payload)}")

        amount = payload.get("amount") if isinstance(payload.get("amount"), dict) else {}
        additional = payload.get("additionalInfo") if isinstance(payload.get("additionalInfo"), dict) else {}

        order_id = self._mandatory(payload, "originalPartnerReferenceNo")
        merchant_id = self._mandatory(payload, "merchantId")
        raw_status = self._mandatory(payload, "latestTransactionStatus")
        raw_amount = self._mandatory(amount, "amount.value", key="value")
        currency = self._mandatory(amount, "amount.currency", key="currency")
        approval_code = self._mandatory(additional, "additionalInfo.approvalCode", key="approvalCode")

        amount_value = parse_amount(raw_amount)
        if amount_value is None or amount_value <= 0:
            raise LinkAjaCallbackInvalidException(
                details="LinkAja notify amount.value is not a valid amount",
                snap_response_code="4005601", snap_response_message="Invalid Field Format amount.value")
        if currency.upper() != LINKAJA_CURRENCY:
            raise LinkAjaCallbackInvalidException(
                details=f"LinkAja notify currency {currency} is not {LINKAJA_CURRENCY}",
                snap_response_code="4005601", snap_response_message="Invalid Field Format amount.currency")
        if merchant_id != self.__config.merchant_id:
            logger.error(f"[LinkAja] notify for order {order_id} carries another merchantId")
            raise LinkAjaCallbackInvalidException(
                details="LinkAja notify merchantId does not match this merchant",
                snap_response_code="4045608", snap_response_message="Invalid Merchant")

        status = map_provider_status(raw_status)
        if status == LinkAjaPaymentStatus.UNKNOWN:
            logger.error(f"[LinkAja] notify for order {order_id} carries undocumented "
                         f"latestTransactionStatus={raw_status!r}")

        return LinkAjaCallbackResult(
            merchant_order_id=order_id,
            provider_reference=self._optional(payload, "originalReferenceNo"),
            status=status,
            verified=True,
            provider_status=raw_status,
            merchant_id=merchant_id,
            amount=amount_value,
            currency=currency.upper(),
            created_time=self._optional(payload, "createdTime"),
            finished_time=self._optional(payload, "finishedTime"),
            approval_code=approval_code,
            terminal_id=self._optional(additional, "terminalId"),
            terminal_name=self._optional(additional, "terminalName"),
            items=self._parse_items(additional.get("items")),
        )

    @staticmethod
    def _mandatory(container: Mapping[str, Any], label: str, key: Optional[str] = None) -> str:
        value = container.get(key or label)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise LinkAjaCallbackInvalidException(
                details=f"LinkAja notify is missing mandatory field {label}",
                snap_response_code="4005602", snap_response_message=f"Invalid Mandatory Field {label}")
        return str(value).strip()

    @staticmethod
    def _optional(container: Mapping[str, Any], key: str) -> Optional[str]:
        value = container.get(key)
        return None if value is None else str(value)

    @staticmethod
    def _parse_items(raw_items: Any) -> list[LinkAjaCallbackItem]:
        if not isinstance(raw_items, list):
            return []
        items = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            items.append(LinkAjaCallbackItem(
                id=str(raw.get("id") or ""), name=str(raw.get("name") or ""),
                unit_price=parse_amount(raw.get("unitPrice")), qty=str(raw.get("qty") or "")))
        return items

    # ------------------------------------------------------------------
    # Amount quote
    # ------------------------------------------------------------------

    def quote_amount(self, amount_minor_units: int, currency: str) -> LinkAjaOrderData:
        """The IDR amount `create_payment` will send for an order, with the rate
        it is derived from - persisted on the order before the payment is
        created, and the only thing a notify's amount is reconciled against."""
        self.ensure_payments_available()
        return LinkAjaOrderData(
            amount=to_linkaja_amount(amount_minor_units, currency, self.__config.usd_to_idr_rate),
            currency=LINKAJA_CURRENCY,
            usd_to_idr_rate=self.__config.usd_to_idr_rate,
            order_amount_minor_units=int(amount_minor_units),
            quoted_at=snap_timestamp(),
        )

    # ------------------------------------------------------------------
    # Status normalization
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_status(provider_status: Optional[str]) -> LinkAjaPaymentStatus:
        """Expose the provider->internal status mapping as a service operation."""
        return map_provider_status(provider_status)
