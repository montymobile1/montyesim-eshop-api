from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from app.config.constants import TopUpRefundStatus, TopUpReservationStatus
from app.config.db import DatabaseTables
from app.exceptions import DatabaseException
from app.models.user import UserWalletModel, UserWalletTransactionModel, UserWalletTopUpReservationModel, \
    UserWalletTopUpRefundModel
from app.repo.base_repo import BaseRepository
from app.schemas.user_wallet import DailyTopUpUsage, TopUpCreditResult, TopUpReservationResult

RESERVE_TOP_UP_FUNCTION = "reserve_wallet_top_up_daily_limit"
COMPLETE_RESERVATION_FUNCTION = "complete_wallet_top_up_reservation"

# a user cannot realistically create more top-up rows than this inside a single day
DAILY_USAGE_MAX_ROWS = 10000


def naive_utc_iso(moment: datetime = None) -> str:
    """Render a moment the way the `timestamp` columns store it: naive UTC."""
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment.isoformat()


class UserWalletRepo(BaseRepository):

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_WALLET, UserWalletModel)

    def reserve_daily_top_up(self, user_id: str, amount: Decimal, currency: str, source: str, success_status: str,
                             window_start: datetime, window_end: datetime, expires_at: datetime, max_count: int,
                             max_amount: Decimal, order_id: Optional[str] = None) -> TopUpReservationResult:
        """Validate the daily limits and hold one count slot and `amount` of capacity.

        The database function locks the user's wallet row, sums the completed top-ups and
        the still active reservations of the window, rejects the request when a limit
        would be broken and otherwise inserts the pending reservation, all in a single
        transaction. `amount` and `max_amount` are expressed in the wallet currency.
        """
        try:
            response = self.client.rpc(RESERVE_TOP_UP_FUNCTION, params={
                "p_user_id": user_id,
                "p_amount": float(amount),
                "p_currency": currency,
                "p_source": source,
                "p_success_status": success_status,
                "p_window_start": window_start.isoformat(),
                "p_window_end": window_end.isoformat(),
                "p_expires_at": expires_at.isoformat(),
                "p_max_count": max_count,
                "p_max_amount": float(max_amount),
                "p_order_id": order_id,
            }).execute()
            return TopUpReservationResult.model_validate(response.data)
        except Exception as e:
            raise DatabaseException(str(e))

    def complete_top_up_reservation(self, user_id: str, amount: Decimal, source: str, success_status: str,
                                    payment_reference: Optional[str], window_start: datetime, window_end: datetime,
                                    max_count: int, max_amount: Decimal,
                                    order_id: Optional[str] = None) -> TopUpCreditResult:
        """Settle a payment that succeeded at the provider, in a single transaction.

        The database function locks the wallet row and either credits the wallet (the
        reservation still holds its capacity, or the daily limits still have room), reports
        that the payment was already settled, or writes the refund record of a payment that
        cannot be credited without breaking the limits.
        """
        try:
            response = self.client.rpc(COMPLETE_RESERVATION_FUNCTION, params={
                "p_user_id": user_id,
                "p_amount": float(amount),
                "p_source": source,
                "p_success_status": success_status,
                "p_payment_reference": payment_reference,
                "p_window_start": window_start.isoformat(),
                "p_window_end": window_end.isoformat(),
                "p_max_count": max_count,
                "p_max_amount": float(max_amount),
                "p_order_id": order_id,
            }).execute()
            return TopUpCreditResult.model_validate(response.data)
        except Exception as e:
            raise DatabaseException(str(e))


class UserWalletTransactionRepo(BaseRepository):
    DAILY_USAGE_MAX_ROWS = DAILY_USAGE_MAX_ROWS

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_WALLET_TRANSACTION, UserWalletTransactionModel)

    def list_since(self, where: dict, since: str, limit: int = 1000) -> List[UserWalletTransactionModel]:
        try:
            query = self.table.select("*")
            for key, value in where.items():
                query = query.eq(key, value)
            query = query.gte("created_at", since).order("created_at", desc=False).limit(limit)
            response = query.execute()
            return [self.model(**item) for item in response.data] if response.data else []
        except Exception as e:
            raise DatabaseException(str(e))

    def get_daily_top_up_usage(self, wallet_id: str, currency: str, source: str, status: str,
                               window_start: datetime, window_end: datetime) -> DailyTopUpUsage:
        """Count and sum the wallet transactions of one wallet inside [window_start, window_end).

        Only the given transaction `source` and `status` are taken into account, so pending,
        failed, cancelled or rejected transactions and non top-up credits never contribute.
        Amounts are aggregated with `Decimal` and stay expressed in the wallet `currency`.
        """
        try:
            response = (self.table.select("amount")
                        .eq("wallet_id", wallet_id)
                        .eq("source", source)
                        .eq("status", status)
                        .gte("created_at", window_start.isoformat())
                        .lt("created_at", window_end.isoformat())
                        .limit(DAILY_USAGE_MAX_ROWS)
                        .execute())
            rows = response.data or []
            total = sum((Decimal(str(row["amount"])) for row in rows), Decimal("0"))
            return DailyTopUpUsage(successful_topup_count=len(rows), successful_topup_amount=total,
                                   currency=currency)
        except Exception as e:
            raise DatabaseException(str(e))


class UserWalletTopUpReservationRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_WALLET_TOP_UP_RESERVATION, UserWalletTopUpReservationModel)

    def get_active_daily_usage(self, wallet_id: str, window_start: datetime,
                               window_end: datetime) -> tuple[int, Decimal]:
        """Count and sum the reservations of the window that still hold capacity.

        Only pending reservations hold capacity. Completing, cancelling or expiring a
        reservation moves it out of that state, so a completed reservation and the wallet
        transaction it produced are never counted twice.
        """
        try:
            response = (self.table.select("amount")
                        .eq("wallet_id", wallet_id)
                        .eq("status", TopUpReservationStatus.PENDING)
                        .gte("created_at", window_start.isoformat())
                        .lt("created_at", window_end.isoformat())
                        .limit(DAILY_USAGE_MAX_ROWS)
                        .execute())
            rows = response.data or []
            total = sum((Decimal(str(row["amount"])) for row in rows), Decimal("0"))
            return len(rows), total
        except Exception as e:
            raise DatabaseException(str(e))

    def list_stale_pending(self, user_id: str, now: datetime,
                           limit: int = 50) -> List[UserWalletTopUpReservationModel]:
        """Pending reservations of a user that are past their TTL.

        Those that already carry a provider payment are reconciled against the provider
        instead of being expired blindly, which is why they are returned rather than
        released here.
        """
        try:
            response = (self.table.select("*")
                        .eq("user_id", user_id)
                        .eq("status", TopUpReservationStatus.PENDING)
                        .lt("expires_at", now.isoformat())
                        .limit(limit)
                        .execute())
            return [self.model(**item) for item in response.data] if response.data else []
        except Exception as e:
            raise DatabaseException(str(e))

    def extend_expiry(self, reservation_id: str, expires_at: datetime):
        """Keep holding the capacity of a payment the provider still reports as payable."""
        return self.update(reservation_id, {"expires_at": naive_utc_iso(expires_at)})

    def release(self, status: str, order_id: str = None, payment_reference: str = None,
                reservation_id: str = None) -> List[dict]:
        """Free the capacity of a pending reservation whose payment will never succeed.

        Only pending reservations are touched, so replaying a failed/cancelled callback is
        a no-op and a completed reservation is never reverted.
        """
        where = {"status": TopUpReservationStatus.PENDING}
        # one identifier is enough, they all point at a single reservation
        if reservation_id:
            where["id"] = reservation_id
        elif order_id:
            where["order_id"] = order_id
        elif payment_reference:
            where["payment_reference"] = payment_reference
        else:
            return []
        return self.update_by(where=where, data={"status": status, "updated_at": naive_utc_iso()}) or []


class UserWalletTopUpRefundRepo(BaseRepository):
    """Reconciliation records of paid top-ups that must be refunded instead of credited."""

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_WALLET_TOP_UP_REFUND, UserWalletTopUpRefundModel)

    def get_by_payment_reference(self, payment_reference: str) -> Optional[UserWalletTopUpRefundModel]:
        return self.get_first_by(where={"payment_reference": payment_reference})

    def list_retryable(self, limit: int = 50) -> List[UserWalletTopUpRefundModel]:
        """Refund records whose provider refund did not succeed yet, oldest first."""
        return self.list_in(where={}, filter={"status": [TopUpRefundStatus.PENDING, TopUpRefundStatus.FAILED]},
                            limit=limit, order_by="created_at")

    def mark_succeeded(self, refund_id: str, provider_refund_reference: str, attempt_count: int):
        return self.update(refund_id, {
            "status": TopUpRefundStatus.SUCCEEDED,
            "provider_refund_reference": provider_refund_reference,
            "attempt_count": attempt_count,
            "last_error": None,
            "last_attempt_at": naive_utc_iso(),
            "updated_at": naive_utc_iso(),
        })

    def mark_failed(self, refund_id: str, error: str, attempt_count: int):
        return self.update(refund_id, {
            "status": TopUpRefundStatus.FAILED,
            "attempt_count": attempt_count,
            # keep the error short, it is stored and logged
            "last_error": str(error)[:500],
            "last_attempt_at": naive_utc_iso(),
            "updated_at": naive_utc_iso(),
        })
