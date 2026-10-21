"""The ``/api/v1/mcp/...`` purchase surface.

Three routes, all additive. No legacy route is renamed, re-pointed or given an MCP
condition, and nothing outside this namespace changes behaviour when the MCP flags move.

Authentication is the existing ``bearer_token`` dependency -- deliberately *not*
``bearer_token_anonymous``, which the legacy assign route uses to support guest checkout.
The user is resolved from the verified token and from nowhere else: none of the request
bodies has a field that could carry a user id, and the endpoints never read one.
"""

import os
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response as HttpResponse

from app.dependencies.security import bearer_token, device_token
from app.models.user import UserModel
from app.schemas.mcp import McpAssignRequest, McpAssignResponse
from app.schemas.mcp_card import McpCardCheckoutRequest, McpCardCheckoutResponse, McpCardStatusResponse
from app.schemas.response import Response
from app.services.mcp_card_service import McpCardCheckoutService
from app.services.mcp_purchase_service import McpPurchaseService

router = APIRouter()

purchase_service = McpPurchaseService()
card_service = McpCardCheckoutService()


@router.post("/bundle/assign", response_model=Response[McpAssignResponse],
             dependencies=[Depends(bearer_token), Depends(device_token)])
async def mcp_assign(assign_request: McpAssignRequest, request: Request, http_response: HttpResponse,
                     user: Annotated[UserModel, Depends(bearer_token)],
                     x_device_id: str = Header(None),
                     x_currency: str = Header(os.getenv("SYSTEM_CURRENCY", "USD")),
                     accept_language: str = Header("en"),
                     idempotency_key: str = Header(None, alias="Idempotency-Key")):
    """Buy one bundle from the caller's wallet through the existing purchase flow.

    ``Idempotency-Key`` is accepted so the MCP service does not have to change, but it is
    **not** honoured: making it durable would require a new table, which this branch
    deliberately does not add. The response always reports ``idempotent_replay: false``.
    See ``docs/mcp-endpoints.md``.
    """
    result = await purchase_service.assign_wallet_bundle(
        user=user, device_id=x_device_id, mcp_request=assign_request,
        x_currency=x_currency, locale=accept_language, request=request)
    # The envelope already carries the outcome code; mirror it onto the transport so a
    # 424 escalation is not delivered as an HTTP 200.
    http_response.status_code = result.responseCode
    return result


@router.post("/bundle/card/checkout", response_model=Response[McpCardCheckoutResponse],
             dependencies=[Depends(bearer_token), Depends(device_token)])
async def mcp_card_checkout(checkout_request: McpCardCheckoutRequest,
                            user: Annotated[UserModel, Depends(bearer_token)],
                            x_device_id: str = Header(None),
                            x_currency: str = Header(os.getenv("SYSTEM_CURRENCY", "USD")),
                            accept_language: str = Header("en"),
                            idempotency_key: str = Header(None, alias="Idempotency-Key")):
    """Open a hosted payment page for one bundle and return the link.

    Nothing is charged here and nothing is provisioned here. The existing
    signature-verified Stripe webhook completes the purchase after the user pays.
    """
    return await card_service.create_checkout(
        user=user, request=checkout_request, x_currency=x_currency, device_id=x_device_id)


@router.get("/bundle/card/status/{payment_reference}", response_model=Response[McpCardStatusResponse],
            dependencies=[Depends(bearer_token), Depends(device_token)])
async def mcp_card_status(payment_reference: str,
                          user: Annotated[UserModel, Depends(bearer_token)],
                          x_device_id: str = Header(None),
                          x_currency: str = Header(os.getenv("SYSTEM_CURRENCY", "USD")),
                          accept_language: str = Header("en")):
    """Read what happened to one card payment. Read-only, and scoped to the caller."""
    return card_service.get_status(user=user, payment_reference=payment_reference)
