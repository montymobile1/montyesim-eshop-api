from typing import Any

from app.config.constants import ErrorMessages


class CustomException(Exception):
    def __init__(self, name: ErrorMessages | str, details: str, code: int):
        self.name = name
        self.details = details
        self.code = code
        super().__init__(f"{name}: {details}")


class EsimHubException(CustomException):
    def __init__(self, details: str | dict | Any):
        self.name = "ESIMHub Exception"
        self.details = details
        self.code = 400
        if isinstance(details, dict):
            self.details = details["message"] or details["code"]
        else:
            self.details = str(details)
        super().__init__(name=ErrorMessages.ESIM_HUB_EXCEPTION, details=self.details, code=self.code)


class BadRequestException(CustomException):
    def __init__(self, details: str):
        super().__init__(code=400, name=ErrorMessages.REQUEST_FAILED, details=details)


class OrderFulfilmentFailedException(BadRequestException):
    """A paid order could not be fulfilled. Returned (not raised) by `BundleService.buy_bundle` /
    `top_up_bundle`, and built by the payment flows for failures they detect themselves.

    Same code, name and details as the `BadRequestException("Payment failed")` it replaces, so any
    caller that only looked at that keeps working. `definite` is True only when nothing was or can
    still be delivered (the eSIM Hub refused, or was never called); `reason` is for logs and support.
    """

    def __init__(self, details: str, definite: bool, reason: str):
        super().__init__(details)
        self.definite = definite
        self.reason = reason


class HubOrderFailedException(OrderFulfilmentFailedException):
    """The eSIM Hub order / top-up call failed (see app/services/integration/esim_hub_outcome.py)."""


class OrderNotFulfilledException(CustomException):
    """Raised to the client when a paid order could not be fulfilled (refunded or under review)."""

    def __init__(self, name: ErrorMessages, details: str):
        super().__init__(code=400, name=name, details=details)


class DatabaseException(CustomException):
    def __init__(self, details: str):
        super().__init__(code=400, name=ErrorMessages.REQUEST_FAILED, details=details)


class DCBException(CustomException):
    def __init__(self, details: str | dict | Any, error: ErrorMessages = ErrorMessages.REQUEST_FAILED):
        self.name = "DCB Exception"
        self.details = details
        self.code = 400
        if isinstance(details, dict):
            self.details = details["message"] or details["code"]
        else:
            self.details = str(details)
        super().__init__(name=error, details=self.details, code=self.code)
