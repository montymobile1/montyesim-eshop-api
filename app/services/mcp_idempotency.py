"""Idempotency-key validation, canonical request building and hashing for MCP purchases.

Nothing in this module touches the legacy purchase flow: it only turns an MCP
request into stable digests that can be stored and compared.

Security notes:
  * The raw ``Idempotency-Key`` never leaves this module: callers receive a
    keyed digest (HMAC-SHA256 when ``MCP_IDEMPOTENCY_HASH_SECRET`` is configured,
    plain domain-separated SHA-256 otherwise) plus a short fingerprint for logs.
  * The digest is bound to the authenticated user id and the operation, so two
    different users may safely present the same external key.
"""

import hashlib
import hmac
import json
import os
import re
from typing import Any, Mapping, Optional, Sequence

from app.config.mcp_constants import (
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


def _hash_secret() -> Optional[bytes]:
    secret = os.getenv("MCP_IDEMPOTENCY_HASH_SECRET")
    if secret and secret.strip():
        return secret.strip().encode("utf-8")
    return None


def hash_idempotency_key(raw_key: str, user_id: str, operation: str) -> str:
    """Return the storable digest of an idempotency key, bound to user + operation."""
    message = f"{_KEY_DOMAIN}|{operation}|{user_id}|{raw_key}".encode("utf-8")
    secret = _hash_secret()
    if secret:
        return hmac.new(secret, message, hashlib.sha256).hexdigest()
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


def build_canonical_request(user_id: str, operation: str, bundle_code: str, payment_type: str,
                            related_search: Any) -> str:
    """Build the deterministic canonical representation of an MCP purchase.

    Only fields that change what is executed are included. Access tokens,
    currency/locale headers, device ids, correlation ids and the caller's
    ``quote_reference`` are deliberately excluded.
    """
    canonical = {
        "version": 1,
        "operation": operation,
        "user_id": _normalize_text(user_id),
        "bundle_code": _normalize_text(bundle_code),
        "payment_type": _normalize_text(payment_type),
        "related_search": normalize_related_search(related_search),
    }
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_canonical_request(canonical_request: str) -> str:
    """SHA-256 digest of a canonical request string."""
    return hashlib.sha256(f"{_REQUEST_DOMAIN}|{canonical_request}".encode("utf-8")).hexdigest()


def build_request_hash(user_id: str, operation: str, bundle_code: str, payment_type: str,
                       related_search: Any) -> str:
    """Convenience wrapper: canonicalize then hash."""
    return hash_canonical_request(
        build_canonical_request(user_id=user_id, operation=operation, bundle_code=bundle_code,
                                payment_type=payment_type, related_search=related_search))
