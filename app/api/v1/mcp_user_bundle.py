"""MCP-only purchase routes.

Isolation comes from this dedicated route, not from any caller supplied header:
``X-Request-Source`` is neither read nor trusted anywhere in this module, and no
header on the legacy route can reach this code.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi import Response as HttpResponse

from app.dependencies.mcp import idempotency_key_header, mcp_feature_enabled, mcp_secret_configured
from app.dependencies.security import bearer_token, device_token
from app.models.user import UserModel
from app.schemas.mcp import McpAssignRequest, McpPurchaseResponse
from app.schemas.response import Response
from app.services.mcp_purchase_service import McpPurchaseService

router = APIRouter()

service = McpPurchaseService()


@router.post("/user/bundle/assign", response_model=Response[McpPurchaseResponse],
             dependencies=[Depends(mcp_feature_enabled), Depends(mcp_secret_configured),
                           Depends(bearer_token), Depends(device_token)],
             summary="MCP wallet purchase (idempotent)",
             description="Wallet-only, idempotent bundle purchase for MCP clients. "
                         "Requires an authenticated eSIM Bearer token, X-Device-Id and Idempotency-Key. "
                         "Settled in the system currency only; omit X-Currency or send the system currency.")
async def mcp_assign(mcp_request: McpAssignRequest, request: Request, http_response: HttpResponse,
                     user: Annotated[UserModel, Depends(bearer_token)],
                     idempotency_key: Annotated[str, Depends(idempotency_key_header)],
                     x_device_id: str = Header(None),
                     # Deliberately not defaulted to DEFAULT_CURRENCY: that is a presentation
                     # default (EUR here) and never priced an MCP purchase. Absent means
                     # "the system currency", resolved in the service.
                     x_currency: str = Header(None),
                     accept_language: str = Header("en")) -> Response[McpPurchaseResponse]:
    result = await service.assign_wallet_bundle(user=user, device_id=x_device_id, mcp_request=mcp_request,
                                                idempotency_key=idempotency_key, x_currency=x_currency,
                                                locale=accept_language, request=request)
    http_response.status_code = result.http_status
    http_response.headers["X-Idempotent-Replay"] = "true" if result.idempotent_replay else "false"
    return result.envelope
