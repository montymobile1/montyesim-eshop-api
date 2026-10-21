"""Query parameters of GET /api/v1/admin/orders (MS-5081, TEOS-117).

``status``, ``payment_status``, ``sort`` and ``direction`` are closed lists: any other value is a 422,
so the client can never choose the column the database sorts on (fixes the orders half of MS-5068).

Stored values (``OrderStatusEnum`` in ``app/config/db.py``): ``success``, ``failure``, ``pending``,
``canceled``. The API name of a failed order is ``failed``; ``failure`` is still accepted (the
portal sent it before TEOS-117) and means the same thing.
"""
from enum import Enum
from typing import Dict, Optional, Tuple

#: Longest accepted ``search``.
SEARCH_MAX = 100


class AdminOrderStatus(str, Enum):
    all = "all"
    success = "success"
    pending = "pending"
    canceled = "canceled"
    failed = "failed"
    failure = "failure"  # legacy name of ``failed``


class AdminPaymentStatus(str, Enum):
    success = "success"
    pending = "pending"
    canceled = "canceled"
    failed = "failed"
    failure = "failure"  # legacy name of ``failed``


#: API status -> value stored in ``user_order.order_status`` / ``payment_status`` (``None``: no filter).
STORED_STATUS: Dict[str, Optional[str]] = {
    "all": None, "success": "success", "pending": "pending", "canceled": "canceled",
    "failed": "failure", "failure": "failure",
}

#: Tab counts returned in ``facets``: facet key -> stored ``order_status`` (``None``: every status).
FACETS: Tuple[Tuple[str, Optional[str]], ...] = (
    ("all", None), ("success", "success"), ("pending", "pending"), ("canceled", "canceled"),
    ("failed", "failure"),
)


class AdminOrderSort(str, Enum):
    created_at = "created_at"
    amount = "amount"
    order_status = "order_status"


class AdminSortDirection(str, Enum):
    asc = "asc"
    desc = "desc"
