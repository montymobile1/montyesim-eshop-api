from enum import StrEnum


class ErrorEnums(StrEnum):
    INVALID_CREDENTIALS = "Invalid credentials"
    USER_NOT_FOUND = "User not found"
    INSUFFICIENT_BALANCE = "Insufficient balance"
    INVALID_ORDER_STATUS = "Invalid order status"
    PAYMENT_FAILED = "Payment failed"
    PROMOTION_NOT_FOUND = "Promotion not found"
    BUNDLE_NOT_AVAILABLE = "Bundle not available"
    NOTIFICATION_SEND_FAILED = "Notification send failed"
    UNAUTHORIZED_ACCESS = "Unauthorized access"
    INVALID_REQUEST = "Invalid request"
    BEARER_TOKEN_REQUIRED= "Bearer Token is required for this operation"
    BEARER_TOKEN_EXPIRED = "Bearer Token is expired"
    ANONYMOUS_USER_NOT_ALLOWED = "Anonymous user is not allowed to perform this operation"
    TOKEN_INTROSPECTION_FAILED = "Token introspection failed"
    INVALID_JSON_FORMAT_IN_BUNDLE = "Invalid JSON format in bundle_data"
    