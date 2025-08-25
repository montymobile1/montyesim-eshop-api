from enum import StrEnum


class ErrorMessages(StrEnum):
    REFRESH_TOKEN_MISSING = "X-Refresh-Token Header is missing"
    BEARER_TOKEN_REQUIRED = "Bearer Token is required for this operation"
    DEVICE_ID_MISSING = "X-Device-ID Header is missing"
    INVALID_JSON_DATA = "Invalid JSON format in bundle_data"
    INVALID_OTP = "Invalid OTP provided"
    ORDER_NOT_FOUND = "Order not found"
    PAYMENT_FAILED = "Payment failed"
    ORDER_FAILED = "Order Failed Please try again"
    BUNDLE_NOT_AVAILABLE = "Bundle Not Available Now Try Again Later"


class PaymentIntentEvents(StrEnum):
    SUCCEEDED = "payment_intent.succeeded"
    FAILED = "payment_intent.failed"


class PaymentStatusEnum(StrEnum):
    COMPLETED = "COMPLETED"
    PENDING = "PENDING"


class UserWalletTransactionSource(StrEnum):
    CASHBACK = "Cashback"
    TOPUP = "TOP-UP"
    VOUCHER = "Voucher"
    PURCHASE_BUNDLE = "Purchase-Bundle"
    TOP_UP_BUNDLE = "TOP-UP-Bundle"
