import json
import os
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from urllib.parse import parse_qs

from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from app.exceptions import CustomException
from app.config.config import esim_hub_service_instance
from app.config.constants import ErrorMessages
from app.repo.user_order_repo import UserOrderRepo
from app.config.db import PaymentTypeEnum
from app.config.helper import get_config

router = APIRouter()

EASYPAY_INDEX_URL = "https://easypay.easypaisa.com.pk/easypay/Index.jsf"
EASYPAY_CONFIRM_URL = "https://easypay.easypaisa.com.pk/easypay/Confirm.jsf"


def html_autopost(action_url: str, fields: Dict[str, Any]) -> str:
    """Generate an HTML page that auto-posts a hidden form to `action_url` with `fields`."""
    inputs = []
    for k, v in (fields or {}).items():
        # ensure values are strings
        val = "" if v is None else str(v)
        inputs.append(f'<input type="hidden" name="{k}" value="{val}"/>')
    inputs_html = "\n".join(inputs)
    html = f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Redirecting...</title>
  </head>
  <body>
    <form id="easypay_form" method="post" action="{action_url}">
      {inputs_html}
    </form>
    <script>
      setTimeout(function() {{ document.getElementById('easypay_form').submit(); }}, 200);
    </script>
  </body>
</html>
"""
    return html


def _get_setting(key: str) -> Optional[str]:
    """Try environment then DB-config for a setting key."""
    val = os.getenv(key)
    if val:
        return val
    try:
        cfg = get_config(key)
        return cfg
    except Exception:
        return None


@router.get("/pay/easypaisa/order/{order_id}", response_class=HTMLResponse)
async def easypaisa_start(order_id: str):
    """Initiate Easypaisa payment flow for an existing order.

    This endpoint validates the order and payment type, computes the amount from the bundle
    using the esim hub service, saves a lightweight easypaisa session inside `bundle_data`
    (existing JSON column) and returns an auto-posting HTML form that posts to Easypaisa Index.
    """
    store_id = _get_setting("EASYPAISA_STORE_ID")
    backend_base = _get_setting("BACKEND_PUBLIC_BASE_URL")
    if not store_id or not backend_base:
        logger.error("EASYPAISA_STORE_ID or BACKEND_PUBLIC_BASE_URL not configured")
        raise CustomException(code=500, name=ErrorMessages.REQUEST_FAILED, details="Easypaisa not configured")

    user_order_repo = UserOrderRepo()
    order = user_order_repo.get_by_id(record_id=order_id)
    if not order:
        raise CustomException(code=404, name=ErrorMessages.ORDER_NOT_FOUND, details=ErrorMessages.ORDER_NOT_FOUND)

    # Validate payment_type equals Easypaisa. Accept both enum and string values.
    pt = getattr(order, 'payment_type', None)
    if isinstance(pt, PaymentTypeEnum):
        payment_type_value = pt.value
    else:
        payment_type_value = str(pt or '')

    if payment_type_value.upper() != 'EASYPAISA':
        raise CustomException(code=400, name=ErrorMessages.INVALID_PAYMENT_TYPE,
                              details=f"Order {order_id} is not an Easypaisa payment")

    # bundle_code is stored in `bundle_id` field according to assign flow
    bundle_code = getattr(order, 'bundle_id', None)
    if not bundle_code:
        raise CustomException(code=400, name=ErrorMessages.INVALID_INPUT, details="Bundle code missing on order")

    esim_hub = esim_hub_service_instance()
    # get bundle details from esim hub
    bundle = await esim_hub.get_bundle_by_id(bundle_id=bundle_code)
    if not bundle or not getattr(bundle, 'is_active', False):
        raise CustomException(code=400, name=ErrorMessages.BUNDLE_NOT_AVAILABLE,
                              details=ErrorMessages.BUNDLE_NOT_AVAILABLE)
    if not getattr(bundle, 'is_stockable', False):
        ok = await esim_hub.check_bundle_applicable(bundle.bundle_info_code)
        if not ok:
            raise CustomException(code=400, name=ErrorMessages.BUNDLE_NOT_AVAILABLE,
                                  details=ErrorMessages.BUNDLE_NOT_AVAILABLE)
    amount = getattr(bundle, 'original_price', None)

    # fallback to order.amount when bundle price not available
    if amount is None:
        amount = getattr(order, 'amount', None)

    if amount is None:
        raise CustomException(code=400, name=ErrorMessages.INVALID_INPUT, details='Unable to determine payment amount')

    # generate orderRefNum
    # use timezone-aware UTC now (avoid deprecated utcnow())
    now = datetime.now(timezone.utc)
    order_ref = f"ORD-{order_id}-{now.strftime('%Y%m%d%H%M%S')}"

    # Persist minimal session in existing JSON column `bundle_data` to avoid migrations
    try:
        bundle_data_raw = getattr(order, 'bundle_data', None)
        if bundle_data_raw is None or bundle_data_raw == '':
            bundle_data = {}
        else:
            if isinstance(bundle_data_raw, str):
                try:
                    bundle_data = json.loads(bundle_data_raw)
                except Exception:
                    # keep original string under _raw if it's not JSON
                    bundle_data = {"_raw_bundle_data": bundle_data_raw}
            elif isinstance(bundle_data_raw, dict):
                bundle_data = bundle_data_raw
            else:
                bundle_data = {}
        easypay_meta = bundle_data.get('easypay_session', {})
        easypay_meta.update({'orderRefNum': order_ref, 'status': 'INITIATED'})
        bundle_data['easypay_session'] = easypay_meta
        # update order record
        # order.id may be attribute name, fall back to record_id
        record_id = getattr(order, 'id', order_id)
        user_order_repo.update(record_id=record_id, data={"bundle_data": json.dumps(bundle_data)})
    except Exception as e:
        logger.error(f"Failed to persist easypay session for order {order_id}: {e}")

    post_back = f"{backend_base.rstrip('/')}/api/payments/easypaisa/token"

    fields = {
        'amount': amount,
        'storeId': store_id,
        'postBackURL': post_back,
        'orderRefNum': order_ref,
        'autoRedirect': '1'
    }
    html = html_autopost(EASYPAY_INDEX_URL, fields)
    return HTMLResponse(content=html)


@router.get("/api/payments/easypaisa/token", response_class=HTMLResponse)
async def easypaisa_token(auth_token: str = Query(...), orderRefNum: Optional[str] = Query(None),
                          orderRefNumber: Optional[str] = Query(None)):
    """Endpoint that Easypaisa will call with an auth token. Saves token and redirects to confirm endpoint."""
    ref = orderRefNum or orderRefNumber
    if not ref:
        raise CustomException(code=400, name=ErrorMessages.INVALID_INPUT, details="Missing orderRefNum")

    backend_base = _get_setting("BACKEND_PUBLIC_BASE_URL")
    if not backend_base:
        raise CustomException(code=500, name=ErrorMessages.REQUEST_FAILED, details="BACKEND_PUBLIC_BASE_URL not set")

    user_order_repo = UserOrderRepo()
    # try to find order by matching easypay_session.orderRefNum inside bundle_data
    # fallback: if order id is embedded in ref (ORD-{order_id}-...), extract it
    order_id = None
    # first try extract from ORD- prefix
    if str(ref).startswith('ORD-'):
        try:
            # ref format: ORD-{order_id}-{ts}
            parts = ref.split('-')
            if len(parts) >= 3:
                order_id = parts[1]
        except Exception:
            order_id = None

    # If we have order_id try get, else scan recent orders for matching bundle_data
    order = None
    if order_id:
        order = user_order_repo.get_by_id(record_id=order_id)
    if not order:
        # scan orders may be expensive; attempt best-effort scan by listing where payment_status != null and limit small
        try:
            orders = user_order_repo.list(where={}, limit=100)
            for o in orders:
                bd = getattr(o, 'bundle_data', None)
                if not bd:
                    continue
                try:
                    bd_obj = json.loads(bd) if isinstance(bd, str) else bd
                    es = bd_obj.get('easypay_session', {})
                    if es and es.get('orderRefNum') == ref:
                        order = o
                        break
                except Exception:
                    continue
        except Exception:
            order = None

    if not order:
        raise CustomException(code=404, name=ErrorMessages.ORDER_NOT_FOUND, details="Order not found for ref")

    # persist auth_token and set status PENDING_CONFIRM
    try:
        bundle_data_raw = getattr(order, 'bundle_data', None)
        if bundle_data_raw is None or bundle_data_raw == '':
            bundle_data = {}
        else:
            bundle_data = json.loads(bundle_data_raw) if isinstance(bundle_data_raw, str) else bundle_data_raw
        easypay_meta = bundle_data.get('easypay_session', {})
        easypay_meta.update({'auth_token': auth_token, 'status': 'PENDING_CONFIRM'})
        bundle_data['easypay_session'] = easypay_meta
        user_order_repo.update(record_id=order.id, data={"bundle_data": json.dumps(bundle_data)})
    except Exception as e:
        logger.error(f"Failed to persist easypay auth token for order {getattr(order, 'id', 'unknown')}: {e}")

    post_back = f"{backend_base.rstrip('/')}/api/payments/easypaisa/result"
    fields = {
        'auth_token': auth_token,
        'postBackURL': post_back
    }
    html = html_autopost(EASYPAY_CONFIRM_URL, fields)
    return HTMLResponse(content=html)


@router.post("/api/payments/easypaisa/result")
async def easypaisa_result(request: Request):
    """Final callback from Easypaisa with payment status. Persist status and raw payload and redirect user to frontend."""
    # Avoid dependency on python-multipart by parsing form body manually if form parsing is unavailable
    data = {}
    try:
        # try native parsing first (works when python-multipart is installed)
        form = await request.form()
        data = {k: v for k, v in form.items()}
    except AssertionError:
        # python-multipart missing; parse raw body
        try:
            content_type = request.headers.get('content-type', '')
            raw = await request.body()
            if raw:
                body = raw.decode('utf-8', errors='ignore')
                if 'application/x-www-form-urlencoded' in content_type:
                    parsed = parse_qs(body)
                    # parse_qs returns lists
                    data = {k: v[0] if isinstance(v, list) and v else v for k, v in parsed.items()}
                elif 'application/json' in content_type:
                    data = json.loads(body)
                else:
                    # fallback attempt: try parse as query-string
                    parsed = parse_qs(body)
                    data = {k: v[0] if isinstance(v, list) and v else v for k, v in parsed.items()}
        except Exception as e:
            logger.error(f"Failed to parse easypay result body: {e}")
            data = {}
    except Exception:
        # any other error, attempt again via raw body
        try:
            raw = await request.body()
            body = raw.decode('utf-8', errors='ignore')
            parsed = parse_qs(body)
            data = {k: v[0] if isinstance(v, list) and v else v for k, v in parsed.items()}
        except Exception:
            data = {}

    status = data.get('status')
    desc = data.get('desc')
    order_ref = data.get('orderRefNumber') or data.get('orderRefNum')

    # Try to find order similar to token endpoint logic
    user_order_repo = UserOrderRepo()
    order = None
    if order_ref and str(order_ref).startswith('ORD-'):
        try:
            order_id = order_ref.split('-')[1]
            order = user_order_repo.get_by_id(record_id=order_id)
        except Exception:
            order = None
    if not order:
        # attempt scan as fallback
        try:
            orders = user_order_repo.list(where={}, limit=100)
            for o in orders:
                bd = getattr(o, 'bundle_data', None)
                if not bd:
                    continue
                try:
                    bd_obj = json.loads(bd) if isinstance(bd, str) else bd
                    es = bd_obj.get('easypay_session', {})
                    if es and es.get('orderRefNum') == order_ref:
                        order = o
                        break
                except Exception:
                    continue
        except Exception:
            order = None

    if not order:
        logger.error(f"Easypay result received but order not found for ref {order_ref}")
        # Still redirect to frontend to surface result
        frontend = _get_setting('FRONTEND_RETURN_URL') or '/'
        redirect_url = f"{frontend}?orderRef={order_ref or ''}&status={status or 'UNKNOWN'}"
        return RedirectResponse(url=redirect_url)

    # update bundle_data.easypay_session with status and raw payload
    try:
        bd_raw = getattr(order, 'bundle_data', None)
        if bd_raw is None or bd_raw == '':
            bd_obj = {}
        else:
            bd_obj = json.loads(bd_raw) if isinstance(bd_raw, str) else bd_raw
        es = bd_obj.get('easypay_session', {})
        es.update({'status': str(status), 'desc': str(desc), 'raw_callback': data})
        bd_obj['easypay_session'] = es
        user_order_repo.update(record_id=order.id, data={"bundle_data": json.dumps(bd_obj)})
    except Exception as e:
        logger.error(f"Failed to persist easypay result for order {order.id}: {e}")

    # Map easypay status to success/fail (Easypaisa typically uses 'SUCCESS' or similar)
    mapped = 'FAILED'
    if status and str(status).upper() in ('SUCCESS', 'OK', '1'):
        mapped = 'SUCCESS'

    frontend = _get_setting('FRONTEND_RETURN_URL') or '/'
    redirect_url = f"{frontend}?orderRef={order_ref or order.id}&status={mapped}"
    return RedirectResponse(url=redirect_url)
