"""MCP-only Stripe hosted-checkout routes (Phase 5A).

Isolation comes from this dedicated module and its own feature flag. No legacy router
imports anything here, and nothing here can be reached from a legacy path.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Request
from fastapi import Response as HttpResponse

from app.config.mcp_card_constants import PAYMENT_REFERENCE_MAX_LENGTH
from app.dependencies.mcp import (
    idempotency_key_header,
    mcp_card_feature_enabled,
    mcp_secret_configured,
)
from app.dependencies.security import bearer_token, device_token
from app.models.user import UserModel
from app.schemas.mcp_card import (
    McpCardCheckoutRequest,
    McpCardCheckoutResponse,
    McpCardStatusResponse,
)
from app.schemas.response import Response
from app.services.mcp_card_checkout_service import McpCardCheckoutService

router = APIRouter()

service = McpCardCheckoutService()


@router.post("/user/bundle/card/checkout", response_model=Response[McpCardCheckoutResponse],
             dependencies=[Depends(mcp_card_feature_enabled), Depends(mcp_secret_configured),
                           Depends(bearer_token), Depends(device_token)],
             summary="MCP card checkout (Stripe hosted, idempotent)",
             description="Creates one Stripe-hosted Checkout Session for a bundle and returns "
                         "its URL. Card details never reach this backend. Requires an "
                         "authenticated eSIM Bearer token, X-Device-Id and Idempotency-Key. "
                         "Settled in the system currency only.")
async def mcp_card_checkout(card_request: McpCardCheckoutRequest, request: Request,
                            http_response: HttpResponse,
                            user: Annotated[UserModel, Depends(bearer_token)],
                            idempotency_key: Annotated[str, Depends(idempotency_key_header)],
                            x_device_id: str = Header(None),
                            x_currency: str = Header(None)) -> Response[McpCardCheckoutResponse]:
    result = await service.create_checkout(user=user, request=card_request,
                                           idempotency_key=idempotency_key, x_currency=x_currency,
                                           device_id=x_device_id)
    http_response.status_code = result.http_status
    http_response.headers["X-Idempotent-Replay"] = "true" if result.idempotent_replay else "false"
    return result.envelope


@router.get("/user/bundle/card/status/{payment_reference}",
            response_model=Response[McpCardStatusResponse],
            dependencies=[Depends(mcp_card_feature_enabled), Depends(bearer_token),
                          Depends(device_token)],
            summary="MCP card payment status",
            description="Owner-scoped status of one MCP card checkout. A reference belonging "
                        "to another user is indistinguishable from one that does not exist.")
async def mcp_card_status(user: Annotated[UserModel, Depends(bearer_token)],
                          payment_reference: str = Path(..., min_length=1,
                                                        max_length=PAYMENT_REFERENCE_MAX_LENGTH),
                          x_device_id: str = Header(None)) -> Response[McpCardStatusResponse]:
    return service.get_status(user=user, payment_reference=payment_reference)
