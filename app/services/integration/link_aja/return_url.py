"""URLs around the single LinkAja `urlParams[0].url`.

LinkAja uses that one URL twice: it POSTs the Inform Payment notify to it
(server to server), and after the payment the LinkAja app opens it to send the
customer back (a GET). We therefore send LINKAJA_NOTIFY_URL with the order id
appended, and the GET on our callback route redirects to the frontend page in
LINKAJA_FRONTEND_RETURN_URL. The notify itself never trusts the query string:
the order comes from the signed body.
"""

import uuid
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ORDER_ID_PARAM = "order_id"
ORDER_ID_PLACEHOLDER = "{order_id}"


def notify_url_for_order(notify_url: str, order_id: str) -> str:
    """`notify_url` with `order_id=<order_id>` added to its query string.

    Any query string already in `notify_url` is kept; an existing `order_id`
    is replaced.
    """
    if not notify_url:
        return notify_url
    parts = urlsplit(notify_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != ORDER_ID_PARAM]
    query.append((ORDER_ID_PARAM, str(order_id)))
    return urlunsplit(parts._replace(query=urlencode(query)))


def valid_order_id(raw: Optional[str]) -> Optional[str]:
    """The canonical UUID string for `raw`, or None if it is not a UUID."""
    if not raw:
        return None
    try:
        return str(uuid.UUID(str(raw).strip()))
    except (ValueError, AttributeError, TypeError):
        return None


def frontend_return_url(template: str, order_id: Optional[str]) -> Optional[str]:
    """Where the GET return redirects to, or None if no usable page is configured.

    `template` is LINKAJA_FRONTEND_RETURN_URL, e.g.
    "https://<frontend>/plans?order_id={order_id}". With a valid order id the
    placeholder is replaced; without one, every query parameter holding the
    placeholder is dropped (and a placeholder in the path removed), so the
    customer still lands on the page. Only http(s) URLs are accepted: the
    target always comes from configuration, never from the request.
    """
    template = (template or "").strip()
    parts = urlsplit(template)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None

    order_id = valid_order_id(order_id)
    if order_id:
        return template.replace(ORDER_ID_PLACEHOLDER, order_id)

    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if ORDER_ID_PLACEHOLDER not in v]
    return urlunsplit(parts._replace(path=parts.path.replace(ORDER_ID_PLACEHOLDER, ""),
                                     query=urlencode(query)))
