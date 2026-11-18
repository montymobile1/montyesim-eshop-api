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
* `LINKAJA_RECONCILE_ENABLED=false` keeps the job from starting on a deployment
  that should not poll (e.g. a local instance sharing QA's database). Unset or
  any other value keeps today's behaviour: the job runs.
* Only orders aged between `LINKAJA_RECONCILE_MIN_AGE_SECONDS` (to give the
  notify a chance first) and `LINKAJA_RECONCILE_MAX_AGE_SECONDS` are queried.
* "Not found" means "not paid yet" (§5.4) and leaves the order pending; only an
  explicit failed / canceled / expired status records a failure.
* Expiry: the payment URL lives 5 minutes. A pending order older than
  `LINKAJA_RECONCILE_EXPIRE_AFTER_SECONDS` (default 30 min) that LinkAja still
  reports as not found was never paid and is canceled, which also stops it
  being polled. Any other answer keeps it pending until it leaves the window.
* A failure for one order is logged and the run moves on to the next.

## One poller per host

The lifespan hook starts this job in every uvicorn worker (uvicorn takes its
worker count from `WEB_CONCURRENCY`), and workers started together tick in the
same second: each pending order was queried once per worker, and each worker's
own access token kept invalidating the others'. So a run only goes ahead in the
worker holding an exclusive `flock` on `LINKAJA_RECONCILE_LOCK_FILE` (default:
a file in the temp directory). The lock is held for the life of the process
and released by the OS when it exits, so another worker takes over on its next
tick. It covers the workers of one host or container, not several instances.
If the lock cannot be used at all the job runs as before: duplicates are safe
(see above), a job that never runs is not.

## approvalCode backfill

LinkAja may answer an inquiry for a paid order without an `approvalCode` and
return it only later. After the pending pass, each run therefore re-asks for
recent successful LinkAja orders that still have no stored code (within
`LINKAJA_RECONCILE_MAX_AGE_SECONDS`) and records it. That pass only ever writes
the code: it never provisions and never changes payment state.
"""

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

try:
    import fcntl
except ImportError:  # pragma: no cover - not on Windows
    fcntl = None

from app.config.db import OrderStatusEnum, PaymentTypeEnum
from app.models.user import UserOrderModel
from app.repo import UserOrderRepo
from app.services.integration.link_aja import LinkAjaService, is_link_aja_enabled, read_order_data
from app.services.link_aja_callback_service import LinkAjaCallbackService

DEFAULT_INTERVAL_SECONDS = 120
DEFAULT_MIN_AGE_SECONDS = 60
DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60
DEFAULT_BATCH_SIZE = 100
DEFAULT_EXPIRE_AFTER_SECONDS = 30 * 60

JOB_ID = "linkaja_reconcile_pending"
LOCK_FILE_NAME = "montyesim-linkaja-reconcile.lock"

_FALSY = ("false", "0", "no", "off")


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


def is_reconciliation_enabled() -> bool:
    """LINKAJA_RECONCILE_ENABLED: only an explicit false/0/no/off disables the job."""
    return (os.getenv("LINKAJA_RECONCILE_ENABLED") or "").strip().lower() not in _FALSY


class HostLeaderLock:
    """Non-blocking exclusive `flock`, kept for the life of the process once taken."""

    def __init__(self, path: Optional[str] = None):
        self.__path = path or (os.getenv("LINKAJA_RECONCILE_LOCK_FILE") or "").strip() or os.path.join(
            tempfile.gettempdir(), LOCK_FILE_NAME)
        self.__fd: Optional[int] = None
        self.__unusable = False

    def acquire(self) -> bool:
        """True when this process is (or just became) the leader."""
        if self.__fd is not None or self.__unusable:
            return True
        if fcntl is None:
            return self.__give_up("file locking is not available on this platform")
        try:
            fd = os.open(self.__path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            return self.__give_up(f"cannot open {self.__path} ({type(exc).__name__})")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.__fd = fd
        logger.info(f"[LinkAja] reconciliation: this worker (pid {os.getpid()}) runs the job")
        return True

    def release(self) -> None:
        if self.__fd is not None:
            os.close(self.__fd)  # closing the descriptor drops the flock
            self.__fd = None

    def __give_up(self, reason: str) -> bool:
        logger.warning(f"[LinkAja] reconciliation lock unusable: {reason}; every worker will run the job")
        self.__unusable = True
        return True


class LinkAjaReconciliationService:
    """Polls LinkAja for pending orders whose notify may have been lost."""

    def __init__(self, link_aja_service: LinkAjaService = None,
                 callback_service: LinkAjaCallbackService = None,
                 user_order_repo: UserOrderRepo = None,
                 clock: Callable[[], datetime] = None,
                 leader_lock: Optional[HostLeaderLock] = None):
        self.__link_aja_service = link_aja_service or LinkAjaService()
        self.__callback_service = callback_service or LinkAjaCallbackService(
            link_aja_service=self.__link_aja_service)
        self.__user_order_repo = user_order_repo or UserOrderRepo()
        self.__clock = clock or (lambda: datetime.now(timezone.utc))
        self.__leader_lock = leader_lock or HostLeaderLock()

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

        expire_after = timedelta(seconds=_env_int("LINKAJA_RECONCILE_EXPIRE_AFTER_SECONDS",
                                                  DEFAULT_EXPIRE_AFTER_SECONDS))
        now = self.__clock()
        claimed = 0
        for order in orders:
            try:
                status = await self.__link_aja_service.get_payment_status(order.id)
                if await self.__callback_service.reconcile_from_status(order, status):
                    claimed += 1
                elif now - _parse_created_at(order.created_at) >= expire_after:
                    self.__callback_service.expire_unpaid_from_status(order, status)
            except Exception as exc:
                logger.error(f"[LinkAja] reconciliation failed for order {order.id}: {exc}")
        return claimed

    def orders_missing_approval_code(self) -> List[UserOrderModel]:
        """Recent successful LinkAja orders whose stored quote has no approvalCode yet."""
        max_age = timedelta(seconds=_env_int("LINKAJA_RECONCILE_MAX_AGE_SECONDS", DEFAULT_MAX_AGE_SECONDS))
        batch = _env_int("LINKAJA_RECONCILE_BATCH_SIZE", DEFAULT_BATCH_SIZE)
        now = self.__clock()

        orders = self.__user_order_repo.list(
            where={"payment_type": PaymentTypeEnum.LINK_AJA, "payment_status": OrderStatusEnum.SUCCESS},
            limit=batch, order_by="created_at", desc=True)

        due = []
        for order in orders:
            if order.payment_status != OrderStatusEnum.SUCCESS:
                continue
            created_at = _parse_created_at(order.created_at)
            quote = read_order_data(order.bundle_data)
            if created_at is None or quote is None or quote.approval_code:
                continue
            if now - created_at <= max_age:
                due.append(order)
        return due

    async def backfill_approval_codes(self) -> int:
        """One backfill pass. Returns how many approval codes were recorded."""
        if not is_link_aja_enabled():
            return 0
        try:
            self.__link_aja_service.ensure_payments_available()
        except Exception as exc:
            logger.error(f"[LinkAja] approvalCode backfill skipped: {exc}")
            return 0

        orders = self.orders_missing_approval_code()
        if not orders:
            return 0
        logger.info(f"[LinkAja] approvalCode backfill: checking {len(orders)} paid order(s)")

        recorded = 0
        for order in orders:
            try:
                status = await self.__link_aja_service.get_payment_status(order.id)
                if self.__callback_service.record_approval_code_from_status(order, status):
                    recorded += 1
            except Exception as exc:
                logger.error(f"[LinkAja] approvalCode backfill failed for order {order.id}: {exc}")
        return recorded

    async def run_once(self) -> None:
        await self.reconcile_pending()
        await self.backfill_approval_codes()

    def run(self) -> None:
        """Scheduler entry point: the scheduler thread has no event loop.

        Does nothing unless this worker holds the host lock (see the module docstring).
        """
        if not self.__leader_lock.acquire():
            return
        try:
            asyncio.run(self.run_once())
        except Exception as exc:
            logger.error(f"[LinkAja] reconciliation run failed: {exc}")


def start_link_aja_reconciliation() -> Optional[BackgroundScheduler]:
    """Start the job when LinkAja is enabled; return None (and do nothing) otherwise."""
    if not is_link_aja_enabled():
        return None
    if not is_reconciliation_enabled():
        logger.info("[LinkAja] reconciliation job not started: LINKAJA_RECONCILE_ENABLED is false")
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
