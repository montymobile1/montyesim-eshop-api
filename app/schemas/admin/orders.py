"""Query parameters of GET /api/v1/admin/orders (MS-5081).

``status``, ``sort`` and ``direction`` are closed lists: any other value is a 422, so the client
can never choose the column the database sorts on (fixes the orders half of MS-5068).
"""
from enum import Enum


class AdminOrderStatus(str, Enum):
    success = "success"
    failure = "failure"
    pending = "pending"
    canceled = "canceled"


class AdminOrderSort(str, Enum):
    created_at = "created_at"
    amount = "amount"


class AdminSortDirection(str, Enum):
    asc = "asc"
    desc = "desc"
