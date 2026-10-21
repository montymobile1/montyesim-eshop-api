"""Reconciliation of pending LinkAja orders.

LinkAja does not retry the notify (Inform Payment, spec v1.1 §4.3). If our
endpoint is down or the notify is lost, a paid order would otherwise stay
pending and never be provisioned. This job asks LinkAja for the status of
every recent pending LinkAja order (Inquiry Status, §4.4, serviceCode 55) and
hands the answer to `LinkAjaCallbackService.reconcile_from_status`.

## No double provisioning

The job never provisions directly. It goes through the same atomic
`payment_status` pending -> success claim as the notify, so whichever of the
two arrives second finds the order already claimed and does nothing. Running
several app instances (several schedulers) is safe for the same reason.

## Scope and safety

* Started only when `LINKAJA_ENABLED` is true at startup, and each run checks
  the flag again; a deployment without LinkAja never runs it.
* Only orders aged between `LINKAJA_RECONCILE_MIN_AGE_SECONDS` (to give the
  notify a chance first) and `LINKAJA_RECONCILE_MAX_AGE_SECONDS` are queried.
* "Not found" means "not paid yet" (§5.4) and leaves the order pending; only an
  explicit failed / canceled / expired status records a failure.
* A failure for one order is logged and the run moves on to the next.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from app.config.db import OrderStatusEnum, PaymentTypeEnum
from app.models.user import UserOrderModel
from app.repo import UserOrderRepo
from app.services.integration.link_aja import LinkAjaService, is_link_aja_enabled, read_order_data
from app.services.link_aja_callback_service import LinkAjaCallbackService

DEFAULT_INTERVAL_SECONDS = 120
DEFAULT_MIN_AGE_SECONDS = 60
DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60
DEFAULT_BATCH_SIZE = 100

JOB_ID = "linkaja_reconcile_pending"


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def _parse_created_at(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class LinkAjaReconciliationService:
    """Polls LinkAja for pending orders whose notify may have been lost."""

    def __init__(self, link_aja_service: LinkAjaService = None,
                 callback_service: LinkAjaCallbackService = None,
                 user_order_repo: UserOrderRepo = None,
                 clock: Callable[[], datetime] = None):
        self.__link_aja_service = link_aja_service or LinkAjaService()
        self.__callback_service = callback_service or LinkAjaCallbackService(
            link_aja_service=self.__link_aja_service)
        self.__user_order_repo = user_order_repo or UserOrderRepo()
        self.__clock = clock or (lambda: datetime.now(timezone.utc))

    def pending_orders(self) -> List[UserOrderModel]:
        """Pending LinkAja orders inside the age window, oldest first.

        Orders without a stored LinkAja quote were never sent to LinkAja, so
        there is nothing to ask about them.
        """
        min_age = timedelta(seconds=_env_int("LINKAJA_RECONCILE_MIN_AGE_SECONDS", DEFAULT_MIN_AGE_SECONDS))
        max_age = timedelta(seconds=_env_int("LINKAJA_RECONCILE_MAX_AGE_SECONDS", DEFAULT_MAX_AGE_SECONDS))
        batch = _env_int("LINKAJA_RECONCILE_BATCH_SIZE", DEFAULT_BATCH_SIZE)
        now = self.__clock()

        # Newest first, so old abandoned orders can never crowd recent ones out of the batch.
        orders = self.__user_order_repo.list(
            where={"payment_type": PaymentTypeEnum.LINK_AJA, "payment_status": OrderStatusEnum.PENDING},
            limit=batch, order_by="created_at", desc=True)

        due = []
        for order in orders:
            created_at = _parse_created_at(order.created_at)
            if created_at is None or read_order_data(order.bundle_data) is None:
                continue
            if min_age <= now - created_at <= max_age:
                due.append(order)
        return list(reversed(due))

    async def reconcile_pending(self) -> int:
        """One reconciliation run. Returns how many orders were claimed for provisioning."""
        if not is_link_aja_enabled():
            return 0
        try:
            self.__link_aja_service.ensure_payments_available()
        except Exception as exc:
            logger.error(f"[LinkAja] reconciliation skipped: {exc}")
            return 0

        orders = self.pending_orders()
        if not orders:
            return 0
        logger.info(f"[LinkAja] reconciliation: checking {len(orders)} pending order(s)")

        claimed = 0
        for order in orders:
            try:
                status = await self.__link_aja_service.get_payment_status(order.id)
                if await self.__callback_service.reconcile_from_status(order, status):
                    claimed += 1
            except Exception as exc:
                logger.error(f"[LinkAja] reconciliation failed for order {order.id}: {exc}")
        return claimed

    def run(self) -> None:
        """Scheduler entry point: the scheduler thread has no event loop."""
        try:
            asyncio.run(self.reconcile_pending())
        except Exception as exc:
            logger.error(f"[LinkAja] reconciliation run failed: {exc}")


def start_link_aja_reconciliation() -> Optional[BackgroundScheduler]:
    """Start the job when LinkAja is enabled; return None (and do nothing) otherwise."""
    if not is_link_aja_enabled():
        return None
    service = LinkAjaReconciliationService()
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        service.run,
        trigger=IntervalTrigger(seconds=_env_int("LINKAJA_RECONCILE_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)),
        id=JOB_ID,
        name="Reconcile pending LinkAja orders",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("[LinkAja] reconciliation job started")
    return scheduler
