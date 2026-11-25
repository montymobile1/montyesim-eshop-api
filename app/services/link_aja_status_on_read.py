"""On-demand LinkAja status check for GET /user/my-esim-by-order/{order_id}.

The customer's frontend polls that endpoint right after paying, usually before
the reconciliation job has run. For a pending LinkAja order this asks LinkAja
once (Inquiry Status, at most 5 s, at most once every 5 s per order and worker)
and, on 00, settles it through the reconciliation job's atomic claim + async
provisioning (`LinkAjaCallbackService.settle_by_inquiry`), so it can never
provision twice. While the order is not ready the endpoint answers HTTP 202
`pending` instead of 400, so the frontend knows to retry.

Scope: this repository serves every client, so all of it applies ONLY when
`applies_to(order)` is true: payment_type LinkAja, LINKAJA_ENABLED=true and
LINKAJA_STATUS_ON_READ_ENABLED=true (default false). Every other order, and
every LinkAja order while either flag is off, takes today's code path unchanged.
"""

import os
import threading
import time
from typing import Callable, Dict, Optional

from fastapi.responses import JSONResponse
from loguru import logger

from app.config.constants import ErrorMessages
from app.config.db import OrderStatusEnum, PaymentTypeEnum
from app.i18n import translate
from app.models.user import UserOrderModel
from app.services.integration.link_aja import is_link_aja_enabled

INQUIRY_TIMEOUT_SECONDS = 5.0
MIN_INTERVAL_SECONDS = 5.0
RETRY_AFTER_SECONDS = 5

_TRUTHY = ("true", "1", "yes", "on")


def is_status_on_read_enabled() -> bool:
    """LINKAJA_STATUS_ON_READ_ENABLED: off unless explicitly true/1/yes/on."""
    return (os.getenv("LINKAJA_STATUS_ON_READ_ENABLED") or "").strip().lower() in _TRUTHY


def applies_to(order: UserOrderModel) -> bool:
    """The single gate. Checked on payment_type first, so a non-LinkAja order reads no flag."""
    return (order.payment_type == PaymentTypeEnum.LINK_AJA
            and is_link_aja_enabled() and is_status_on_read_enabled())


def is_not_ready(order: UserOrderModel) -> bool:
    """Payment still pending, or paid and the eSIM still being provisioned."""
    return (order.payment_status == OrderStatusEnum.PENDING
            or (order.payment_status == OrderStatusEnum.SUCCESS and order.order_status == OrderStatusEnum.PENDING))


def pending_response(order: UserOrderModel) -> JSONResponse:
    """HTTP 202 in the usual envelope: not an error, retry after a few seconds.

    `title` / `message` are translated like the error responses; `developerMessage`
    carries the stable code (PAYMENT_PENDING or ORDER_PENDING).
    """
    payment_pending = order.payment_status == OrderStatusEnum.PENDING
    code = f"{ErrorMessages.PAYMENT_}PENDING" if payment_pending else f"{ErrorMessages.ORDER_}PENDING"
    text = translate(code)
    return JSONResponse(
        status_code=202,
        headers={"Retry-After": str(RETRY_AFTER_SECONDS), "Cache-Control": "no-store"},
        content={"status": "pending", "totalCount": 0, "data": None, "title": text, "message": text,
                 "developerMessage": code, "responseCode": 202})


class InquiryThrottle:
    """At most one inquiry per order every MIN_INTERVAL_SECONDS, per worker process."""

    def __init__(self, interval: float = MIN_INTERVAL_SECONDS, clock: Callable[[], float] = time.monotonic):
        self.__interval = interval
        self.__clock = clock
        self.__last: Dict[str, float] = {}
        self.__lock = threading.Lock()

    def allow(self, order_id: str) -> bool:
        now = self.__clock()
        with self.__lock:
            last = self.__last.get(order_id)
            if last is not None and now - last < self.__interval:
                return False
            if len(self.__last) > 1000:  # keep memory bounded: drop expired entries
                self.__last = {k: v for k, v in self.__last.items() if now - v < self.__interval}
            self.__last[order_id] = now
            return True


class LinkAjaStatusOnRead:
    def __init__(self, callback_service=None, throttle: Optional[InquiryThrottle] = None):
        self.__callback_service = callback_service
        self.__throttle = throttle or InquiryThrottle()

    def __callbacks(self):
        if self.__callback_service is None:
            # Built on first LinkAja use only; imported here to keep this module light.
            from app.services.link_aja_callback_service import LinkAjaCallbackService
            self.__callback_service = LinkAjaCallbackService()
        return self.__callback_service

    async def check(self, order: UserOrderModel) -> Optional[JSONResponse]:
        """Run for an order that `applies_to`. Returns the 202 pending response, or None to
        let the endpoint continue normally (paid and provisioned, or failed/canceled)."""
        if order.payment_status == OrderStatusEnum.PENDING and self.__throttle.allow(order.id):
            try:
                # Mutates `order` in memory when it claims it (payment_status -> success).
                await self.__callbacks().settle_by_inquiry(order, timeout=INQUIRY_TIMEOUT_SECONDS,
                                                           source="status on read")
            except Exception as exc:  # the read must never fail because of the check
                logger.error(f"[LinkAja] status on read for order {order.id} could not run: {type(exc).__name__}")
        return pending_response(order) if is_not_ready(order) else None


_checker: Optional[LinkAjaStatusOnRead] = None


def checker() -> LinkAjaStatusOnRead:
    """Process-wide instance, so the per-order throttle is shared by every request."""
    global _checker
    if _checker is None:
        _checker = LinkAjaStatusOnRead()
    return _checker
