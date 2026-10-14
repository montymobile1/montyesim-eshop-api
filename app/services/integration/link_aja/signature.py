"""LinkAja SNAP request signing and notify verification.

Source: "LinkAja Applink SNAP" v1.1 §5.1 (and the pre-request scripts of the
LinkAja "Applink SNAP" Postman collection, which agree with it). Both
outbound schemes were accepted by the LinkAja dev sandbox on 2026-09-28; a
deliberately wrong secret was rejected with `4015500 Unauthorized. Invalid
signature`.

Three schemes:

* Access token (§5.1.1, asymmetric) - SHA256withRSA over
  "<clientKey>|<X-TIMESTAMP>", signed with the merchant's RSA private key,
  base64-encoded.
* Every other outbound call (§5.1.2, symmetric) - HMAC-SHA512 keyed with the
  client secret over
  "<METHOD>:<PATH>:<accessToken>:<lowerhex(sha256(body))>:<X-TIMESTAMP>",
  base64-encoded.
* Inbound debit notify (§5.1.3, asymmetric) - SHA256withRSA over
  "<METHOD>:<EndpointUrl>:<lowerhex(sha256(minify(body)))>:<X-TIMESTAMP>",
  verified with LinkAja's public key.

The outbound body hash covers the exact bytes on the wire, so callers serialize
once with `serialize_body` and send those bytes unchanged.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .exceptions import LinkAjaConfigurationException

# The Postman scripts emit moment's local "YYYY-MM-DDTHH:mm:ssZ"; the collection
# does not name a timezone. Jakarta time (WIB, UTC+7) is what the sandbox was
# verified with.
JAKARTA = timezone(timedelta(hours=7))


def snap_timestamp(now: Optional[datetime] = None) -> str:
    """X-TIMESTAMP value, e.g. 2026-09-28T18:12:23+07:00."""
    return (now or datetime.now(JAKARTA)).astimezone(JAKARTA).isoformat(timespec="seconds")


def trx_date(now: Optional[datetime] = None) -> str:
    """`additionalInfo.trxDate` value, yyyyMMddHHmmss in Jakarta time (Postman sample: 20260901135300)."""
    return (now or datetime.now(JAKARTA)).astimezone(JAKARTA).strftime("%Y%m%d%H%M%S")


def external_id() -> str:
    """X-EXTERNAL-ID: numeric request id. Postman sends a random integer; we use
    a time-based prefix plus randomness so ids do not repeat within a day."""
    return f"{str(time.time_ns())[:16]}{secrets.randbelow(10_000):04d}"


def serialize_body(body: dict) -> bytes:
    """Compact JSON - the exact bytes to hash and to send."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def load_private_key(pem: str):
    """Parse the merchant RSA private key.

    Accepts PEM text with real newlines or with literal "\\n" sequences, which is
    how a multi-line key usually arrives through a single-line env var.
    """
    try:
        return serialization.load_pem_private_key(pem.replace("\\n", "\n").encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        # Never include the key material in the message.
        raise LinkAjaConfigurationException(
            details="LinkAja private key could not be parsed as an unencrypted PEM RSA key") from exc


def load_public_key(pem: str):
    """Parse LinkAja's RSA public key (SubjectPublicKeyInfo or PKCS#1 PEM).

    Same newline tolerance as `load_private_key`.
    """
    try:
        return serialization.load_pem_public_key(pem.replace("\\n", "\n").encode("utf-8"))
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise LinkAjaConfigurationException(
            details="LinkAja notify public key could not be parsed as a PEM RSA public key") from exc


def token_signature(private_key, client_key: str, timestamp: str) -> str:
    string_to_sign = f"{client_key}|{timestamp}".encode("utf-8")
    raw = private_key.sign(string_to_sign, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(raw).decode("ascii")


def service_string_to_sign(method: str, path: str, access_token: str, body: bytes, timestamp: str) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}:{path}:{access_token}:{body_hash}:{timestamp}"


def service_signature(client_secret: str, method: str, path: str, access_token: str,
                      body: bytes, timestamp: str) -> str:
    string_to_sign = service_string_to_sign(method, path, access_token, body, timestamp)
    raw = hmac.new(client_secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha512).digest()
    return base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------------------
# Inbound notify (spec §5.1.3 / §5.1.5)
# ---------------------------------------------------------------------------

def notify_body_candidates(raw_body: bytes) -> list[bytes]:
    """The byte strings LinkAja may have hashed for a notify body.

    §5.1.5 says the body is "minified" before hashing but is ambiguous about
    what that means for whitespace inside JSON string values (its rule strips
    every space; its example only shows whitespace between tokens). Rather than
    guess one reading and reject genuine notifies, verification tries each
    deterministic canonical form of the received body. This is not a weakening:
    every candidate is a pure function of the body, so forging any of them still
    requires LinkAja's private key.

    Order: bytes exactly as received, compact JSON (whitespace between tokens
    removed, string contents intact), then every whitespace character removed.
    """
    candidates = [raw_body]
    try:
        compact = json.dumps(json.loads(raw_body.decode("utf-8")), separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8")
        candidates.append(compact)
    except (ValueError, UnicodeDecodeError):
        pass
    stripped = raw_body.translate(None, b" \t\r\n")
    candidates.append(stripped)
    unique: list[bytes] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def notify_string_to_sign(method: str, endpoint_url: str, body: bytes, timestamp: str) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}:{endpoint_url}:{body_hash}:{timestamp}"


def notify_signature(private_key, method: str, endpoint_url: str, body: bytes, timestamp: str) -> str:
    """What LinkAja computes. Only used by tests and tooling - we never hold
    LinkAja's private key."""
    string_to_sign = notify_string_to_sign(method, endpoint_url, body, timestamp).encode("utf-8")
    raw = private_key.sign(string_to_sign, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(raw).decode("ascii")


def verify_notify_signature(public_key, signature_b64: str, method: str, endpoint_url: str,
                            raw_body: bytes, timestamp: str) -> bool:
    """True iff `signature_b64` is a valid SHA256withRSA signature, under
    `public_key`, of the §5.1.3 string-to-sign for any canonical form of
    `raw_body`. Never raises on bad input: malformed base64 is simply invalid."""
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError):
        return False
    if not signature:
        return False
    for body in notify_body_candidates(raw_body):
        string_to_sign = notify_string_to_sign(method, endpoint_url, body, timestamp).encode("utf-8")
        try:
            public_key.verify(signature, string_to_sign, padding.PKCS1v15(), hashes.SHA256())
            return True
        except InvalidSignature:
            continue
    return False
