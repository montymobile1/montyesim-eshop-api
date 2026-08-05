"""Idempotency-key validation, canonical request building and hashing for MCP purchases.

Nothing in this module touches the legacy purchase flow: it only turns an MCP
request into stable digests that can be stored and compared.

Security notes:
  * The raw ``Idempotency-Key`` never leaves this module: callers receive an
    HMAC-SHA256 digest keyed with ``MCP_IDEMPOTENCY_HASH_SECRET`` plus a short
    fingerprint for logs.
  * While ``MCP_PURCHASE_ENABLED`` is true the secret is **mandatory**. There is
    no silent fall back to unkeyed SHA-256: an unusable secret raises before any
    digest is produced, so a misconfigured deployment cannot write records that a
    correctly configured one would compute differently.
  * The digest is bound to the authenticated user id and the operation, so two
    different users may safely present the same external key.
  * The secret's value is never logged, echoed or included in an error.
"""

import hashlib
import hmac
import json
import os
import re
from typing import Any, Mapping, Optional, Sequence

from app.config.feature_flags import is_mcp_purchase_enabled
from app.config.mcp_constants import (
    IDEMPOTENCY_HASH_SECRET_ENV,
    IDEMPOTENCY_HASH_SECRET_MIN_LENGTH,
    IDEMPOTENCY_HASH_SECRET_PLACEHOLDERS,
    IDEMPOTENCY_KEY_MAX_LENGTH,
    IDEMPOTENCY_KEY_MIN_LENGTH,
    IDEMPOTENCY_KEY_PATTERN,
    McpErrorMessages,
)
from app.exceptions import CustomException

_KEY_RE = re.compile(IDEMPOTENCY_KEY_PATTERN)

#: Domain separation strings; changing them invalidates every stored digest.
_KEY_DOMAIN = "mcp.idempotency.key.v1"
_REQUEST_DOMAIN = "mcp.idempotency.request.v1"

#: Length of the non-reversible fingerprint that may appear in logs.
FINGERPRINT_LENGTH = 12


def validate_idempotency_key(raw_key: Optional[str]) -> str:
    """Validate the caller supplied Idempotency-Key and return it trimmed.

    Raises a 400 ``CustomException`` for missing, blank, wrongly sized or
    non-opaque keys. The raw value is never included in the error details.
    """
    if raw_key is None or not str(raw_key).strip():
        raise CustomException(code=400, name=McpErrorMessages.IDEMPOTENCY_KEY_REQUIRED,
                              details="Idempotency-Key header is required for this operation")
    key = str(raw_key).strip()
    if not (IDEMPOTENCY_KEY_MIN_LENGTH <= len(key) <= IDEMPOTENCY_KEY_MAX_LENGTH):
        raise CustomException(
            code=400, name=McpErrorMessages.IDEMPOTENCY_KEY_INVALID,
            details=(f"Idempotency-Key must be between {IDEMPOTENCY_KEY_MIN_LENGTH} and "
                     f"{IDEMPOTENCY_KEY_MAX_LENGTH} characters"))
    if not _KEY_RE.match(key):
        raise CustomException(
            code=400, name=McpErrorMessages.IDEMPOTENCY_KEY_INVALID,
            details="Idempotency-Key may only contain letters, digits, '-', '_', '.' and '~'")
    return key


def _raw_hash_secret() -> Optional[str]:
    secret = os.getenv(IDEMPOTENCY_HASH_SECRET_ENV)
    if secret and secret.strip():
        return secret.strip()
    return None


def hash_secret_problem() -> Optional[str]:
    """Return why the configured hash secret is unusable, or ``None`` when it is fine.

    The reason names the *category* of problem only. It never contains the secret,
    any part of it, or its length beyond the published minimum.
    """
    secret = _raw_hash_secret()
    if secret is None:
        return f"{IDEMPOTENCY_HASH_SECRET_ENV} is not set"
    if len(secret) < IDEMPOTENCY_HASH_SECRET_MIN_LENGTH:
        return (f"{IDEMPOTENCY_HASH_SECRET_ENV} must be at least "
                f"{IDEMPOTENCY_HASH_SECRET_MIN_LENGTH} characters")
    normalized = secret.lower()
    if any(normalized == placeholder or normalized.startswith(placeholder)
           for placeholder in IDEMPOTENCY_HASH_SECRET_PLACEHOLDERS):
        return f"{IDEMPOTENCY_HASH_SECRET_ENV} still looks like a placeholder value"
    if len(set(secret)) == 1:
        return f"{IDEMPOTENCY_HASH_SECRET_ENV} is a single repeated character"
    return None


def is_hash_secret_usable() -> bool:
    """True when a real, sufficiently strong hash secret is configured."""
    return hash_secret_problem() is None


def require_hash_secret() -> bytes:
    """Return the secret as bytes, or fail closed.

    Raises a 503 ``CustomException`` when the secret is missing, blank,
    placeholder-like, too short or a single repeated character. The exception
    carries the category of problem, never the value.
    """
    problem = hash_secret_problem()
    if problem is not None:
        raise CustomException(
            code=503, name=McpErrorMessages.MCP_IDEMPOTENCY_SECRET_MISCONFIGURED,
            details=(f"MCP purchase is enabled but its idempotency hash secret is unusable: "
                     f"{problem}. Configure a stable, high-entropy secret and restart."))
    return _raw_hash_secret().encode("utf-8")


def hash_idempotency_key(raw_key: str, user_id: str, operation: str) -> str:
    """Return the storable digest of an idempotency key, bound to user + operation.

    While the feature is enabled a valid secret is mandatory and this raises rather
    than degrading to an unkeyed digest. When the feature is disabled (tests, and
    deployments that never serve MCP traffic) an unkeyed digest is still produced
    so the pure hashing helpers stay usable.
    """
    message = f"{_KEY_DOMAIN}|{operation}|{user_id}|{raw_key}".encode("utf-8")
    if is_mcp_purchase_enabled():
        return hmac.new(require_hash_secret(), message, hashlib.sha256).hexdigest()
    secret = _raw_hash_secret()
    if secret:
        return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hashlib.sha256(message).hexdigest()


def fingerprint(digest: str) -> str:
    """Short, non-reversible fingerprint of a digest, safe for correlation logs."""
    if not digest:
        return "-"
    return digest[:FINGERPRINT_LENGTH]


def user_fingerprint(user_id: str) -> str:
    """Short fingerprint of a user id so logs never carry email/phone/user ids."""
    if not user_id:
        return "-"
    return hashlib.sha256(f"mcp.user.v1|{user_id}".encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def _normalize_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_related_search(related_search: Any) -> Optional[dict]:
    """Normalize ``related_search`` into a stable, comparable structure.

    * ``None`` and an "empty" search normalize to ``None`` so both express the
      same execution input.
    * ``iso3_code`` is upper-cased and country names are trimmed.
    * The country list is order-insensitive (search order does not change what is
      purchased), therefore it is sorted and de-duplicated.
    """
    if related_search is None:
        return None

    if isinstance(related_search, Mapping):
        region = related_search.get("region")
        countries = related_search.get("countries")
    else:
        region = getattr(related_search, "region", None)
        countries = getattr(related_search, "countries", None)

    normalized_region = None
    if region is not None:
        if isinstance(region, Mapping):
            iso_code, region_name = region.get("iso_code"), region.get("region_name")
        else:
            iso_code, region_name = getattr(region, "iso_code", None), getattr(region, "region_name", None)
        iso_code = _normalize_text(iso_code)
        region_name = _normalize_text(region_name)
        if iso_code or region_name:
            normalized_region = {
                "iso_code": iso_code.upper() if iso_code else None,
                "region_name": region_name,
            }

    normalized_countries: list[dict] = []
    if isinstance(countries, Sequence) and not isinstance(countries, (str, bytes)):
        for country in countries:
            if isinstance(country, Mapping):
                iso3_code, country_name = country.get("iso3_code"), country.get("country_name")
            else:
                iso3_code = getattr(country, "iso3_code", None)
                country_name = getattr(country, "country_name", None)
            iso3_code = _normalize_text(iso3_code)
            country_name = _normalize_text(country_name)
            if not iso3_code and not country_name:
                continue
            normalized_countries.append({
                "iso3_code": iso3_code.upper() if iso3_code else None,
                "country_name": country_name,
            })

    deduplicated = {
        (entry["iso3_code"], entry["country_name"]): entry for entry in normalized_countries
    }
    ordered = [deduplicated[key] for key in sorted(deduplicated, key=lambda k: (k[0] or "", k[1] or ""))]

    if normalized_region is None and not ordered:
        return None
    return {"region": normalized_region, "countries": ordered}


def system_currency() -> str:
    """The one currency the backend prices and settles MCP purchases in."""
    return normalize_currency(os.getenv("SYSTEM_CURRENCY", "USD")) or "USD"


def normalize_currency(currency: Any) -> Optional[str]:
    """Normalize a currency code: trimmed and upper-cased. Blank becomes ``None``."""
    text = _normalize_text(currency)
    return text.upper() if text else None


def effective_currency(x_currency: Any) -> str:
    """Resolve the currency that actually applies, after backend defaults.

    An absent or blank ``X-Currency`` means "whatever the backend settles in", which
    is the system currency - deliberately *not* ``DEFAULT_CURRENCY``, which is a
    presentation default (it is ``EUR`` in this deployment) and has never driven what
    an MCP purchase costs.
    """
    return normalize_currency(x_currency) or system_currency()


def build_canonical_request(user_id: str, operation: str, bundle_code: str, payment_type: str,
                            related_search: Any, currency: Any) -> str:
    """Build the deterministic canonical representation of an MCP purchase.

    Only fields that change what is executed are included.

    ``currency`` is the *effective* currency (after backend defaults, normalized).
    It is bound into the identity so that a key can never be replayed across a
    materially different currency, even if the endpoint later stops being
    single-currency. ``usd`` and ``USD`` normalize to the same value and therefore
    replay.

    Deliberately excluded: access tokens, device ids, correlation ids, the caller's
    ``quote_reference``, and ``Accept-Language``. Locale is excluded because it is
    provably inert here - ``UserBundleService.assign`` accepts ``locale`` but never
    reads it, and the wallet payment path it delegates to takes no locale at all.
    """
    canonical = {
        "version": 2,
        "operation": operation,
        "user_id": _normalize_text(user_id),
        "bundle_code": _normalize_text(bundle_code),
        "payment_type": _normalize_text(payment_type),
        "currency": normalize_currency(currency),
        "related_search": normalize_related_search(related_search),
    }
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_canonical_request(canonical_request: str) -> str:
    """SHA-256 digest of a canonical request string."""
    return hashlib.sha256(f"{_REQUEST_DOMAIN}|{canonical_request}".encode("utf-8")).hexdigest()


def build_request_hash(user_id: str, operation: str, bundle_code: str, payment_type: str,
                       related_search: Any, currency: Any) -> str:
    """Convenience wrapper: canonicalize then hash."""
    return hash_canonical_request(
        build_canonical_request(user_id=user_id, operation=operation, bundle_code=bundle_code,
                                payment_type=payment_type, related_search=related_search,
                                currency=currency))


def build_card_canonical_request(user_id: str, operation: str, bundle_code: str, currency: Any,
                                 related_search: Any, quote_reference: Any) -> str:
    """Canonical representation of an MCP *card checkout* request.

    Separate from the wallet canonicaliser on purpose:

    * ``quote_reference`` IS part of the card identity. A card checkout is a quote being
      taken to payment, so presenting the same key with a different quote must conflict
      rather than replay someone else's priced session. For wallet it stays excluded,
      and changing that would silently invalidate every stored wallet digest.
    * ``payment_type`` is pinned to ``Card`` here rather than read from input, because
      the caller cannot choose it.
    """
    canonical = {
        "version": 1,
        "operation": operation,
        "user_id": _normalize_text(user_id),
        "bundle_code": _normalize_text(bundle_code),
        "payment_type": "Card",
        "currency": normalize_currency(currency),
        "quote_reference": _normalize_text(quote_reference),
        "related_search": normalize_related_search(related_search),
    }
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_card_request_hash(user_id: str, operation: str, bundle_code: str, currency: Any,
                            related_search: Any, quote_reference: Any) -> str:
    """Canonicalize then hash an MCP card checkout request."""
    return hash_canonical_request(
        build_card_canonical_request(user_id=user_id, operation=operation, bundle_code=bundle_code,
                                     currency=currency, related_search=related_search,
                                     quote_reference=quote_reference))
