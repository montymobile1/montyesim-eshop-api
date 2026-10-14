"""Async HTTP transport for LinkAja.

This is the only place in the codebase that opens a socket to LinkAja. It owns
timeouts, error translation, response validation and sanitized logging - but it
owns **no knowledge of LinkAja's API**: paths and payloads are supplied by the
caller, which in turn takes them from configuration.

Deliberate design point: `request_json` refuses to send a request when the path
it is given is empty. An empty path means "this endpoint is not in our
configuration yet", and issuing a guessed request would be worse than failing.
"""

from typing import Any, Mapping, Optional

import httpx
from loguru import logger

from .config import LinkAjaConfig
from .exceptions import LinkAjaNotConfiguredException, LinkAjaProviderException
from .sanitize import redact, safe_url


class LinkAjaHttpClient:
    """Thin, reusable async client bound to a `LinkAjaConfig`.

    A new `httpx.AsyncClient` is created per request, matching
    `EsimHubService`'s approach, which avoids reusing a client across event
    loops (this project runs some flows via `asyncio.run` from sync callbacks).
    """

    def __init__(self, config: LinkAjaConfig):
        self.__config = config

    @property
    def config(self) -> LinkAjaConfig:
        return self.__config

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.__config.connect_timeout,
            read=self.__config.read_timeout,
            write=self.__config.read_timeout,
            pool=self.__config.connect_timeout,
        )

    def _build_url(self, path: str, operation: str) -> str:
        """Join the configured base URL and path, refusing unconfigured calls."""
        if not self.__config.base_url:
            raise LinkAjaNotConfiguredException(
                details=f"LinkAja {operation} cannot be performed: LINKAJA_BASE_URL is not set"
            )
        if not path:
            raise LinkAjaNotConfiguredException(
                details=f"LinkAja {operation} cannot be performed: the endpoint path is not "
                        f"configured, and it must come from the official LinkAja specification "
                        f"rather than being guessed"
            )
        return f"{self.__config.base_url.rstrip('/')}/{path.lstrip('/')}"

    async def request_json(self, method: str, path: str, operation: str,
                           json_body: Optional[Mapping[str, Any]] = None,
                           headers: Optional[Mapping[str, str]] = None,
                           content: Optional[bytes] = None,
                           tolerated_statuses: tuple[int, ...] = ()) -> dict:
        """Perform a JSON request and return the decoded body.

        `content` sends pre-serialized bytes verbatim instead of `json_body`.
        LinkAja's request signature hashes the exact body bytes, so a signed
        request must not be re-serialized by httpx.

        `tolerated_statuses` are error statuses whose JSON body is returned to
        the caller instead of raising, for calls where LinkAja uses the HTTP
        status as part of the answer (status inquiry: 404 = no transaction).

        Translates every transport, status and decoding failure into
        `LinkAjaProviderException`, so callers never see raw `httpx` errors and
        no provider detail leaks upward.
        """
        url = self._build_url(path, operation)

        # Log the sanitized shape only: never the body values, never auth headers.
        logger.info(f"[LinkAja] {operation}: {method} {safe_url(url)} "
                    f"fields={sorted(json_body.keys()) if json_body else []}")

        body_kwargs = {"content": content} if content is not None else {"json": json_body}
        try:
            async with httpx.AsyncClient(timeout=self._timeout()) as client:
                response = await client.request(method, url, headers=dict(headers or {}), **body_kwargs)
        except httpx.TimeoutException as exc:
            logger.error(f"[LinkAja] {operation} timed out after "
                         f"connect={self.__config.connect_timeout}s read={self.__config.read_timeout}s")
            raise LinkAjaProviderException(details=f"LinkAja {operation} timed out") from exc
        except httpx.HTTPError as exc:
            # str(exc) can include the URL but never credentials; still, keep the
            # client-facing detail generic.
            logger.error(f"[LinkAja] {operation} transport error: {type(exc).__name__}")
            raise LinkAjaProviderException(details=f"LinkAja {operation} failed: provider unreachable") from exc

        return self._parse(response, operation, tolerated_statuses)

    @staticmethod
    def _provider_code(response: httpx.Response) -> str:
        """LinkAja's non-secret SNAP responseCode/responseMessage, for logs only."""
        try:
            payload = response.json()
        except ValueError:
            return ""
        if not isinstance(payload, dict):
            return ""
        return f"responseCode={payload.get('responseCode')} responseMessage={payload.get('responseMessage')}"

    def _parse(self, response: httpx.Response, operation: str,
               tolerated_statuses: tuple[int, ...] = ()) -> dict:
        """Validate status and decode the body, redacting anything logged."""
        if response.status_code >= 400 and response.status_code not in tolerated_statuses:
            logger.error(f"[LinkAja] {operation} returned HTTP {response.status_code} "
                         f"{self._provider_code(response)}")
            raise LinkAjaProviderException(
                details=f"LinkAja {operation} failed with status {response.status_code}",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            logger.error(f"[LinkAja] {operation} returned a non-JSON body "
                         f"(status={response.status_code}, content_type="
                         f"{response.headers.get('content-type', 'unknown')})")
            raise LinkAjaProviderException(
                details=f"LinkAja {operation} returned a malformed (non-JSON) response"
            ) from exc

        if not isinstance(payload, dict):
            logger.error(f"[LinkAja] {operation} returned JSON of unexpected type {type(payload).__name__}")
            raise LinkAjaProviderException(
                details=f"LinkAja {operation} returned an unexpected response shape"
            )

        logger.debug(f"[LinkAja] {operation} response keys={sorted(payload.keys())} "
                     f"payload={redact(payload)}")
        return payload
