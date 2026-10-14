"""In-memory cache for the LinkAja B2B access token.

Spec v1.1 p.10: the token lives 900 seconds (the sandbox reports `expiresIn`
"899"). Fetching a fresh one for every call is wasteful and doubles the latency
of every payment, so the token is kept in memory and refreshed shortly before
it expires.

Concurrency: the cache is shared by every `LinkAjaService` built from the same
credentials, and this project mixes event loops (FastAPI's loop for requests,
`asyncio.run` inside task-executor threads). So:

* the cached value is guarded by a `threading.Lock` (loop-agnostic);
* the "someone is already fetching" dedupe is an `asyncio.Lock` that is
  re-created whenever it is used from a different event loop, because an
  `asyncio.Lock` cannot be shared across loops.

Only the token value is cached - never the private key or the client secret -
and the token itself is never logged.
"""

import asyncio
import threading
import time
from typing import Awaitable, Callable, Optional

# Refresh this many seconds before LinkAja's stated expiry, so a token that is
# about to expire is never used for a call that may take a few seconds.
REFRESH_MARGIN_SECONDS = 60.0

# Tokens whose expiry LinkAja reports as shorter than this are not worth
# caching; a non-positive remaining lifetime is treated the same way.
MIN_CACHEABLE_SECONDS = 5.0


class TokenCache:
    """One cached access token plus its absolute expiry."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._state_lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._fetch_lock: Optional[asyncio.Lock] = None
        self._fetch_loop: Optional[asyncio.AbstractEventLoop] = None

    # -- cached value --------------------------------------------------------

    def peek(self) -> Optional[str]:
        """The cached token if it is still comfortably valid, else None."""
        with self._state_lock:
            if self._token and self._clock() < self._expires_at - REFRESH_MARGIN_SECONDS:
                return self._token
            return None

    def store(self, token: str, expires_in_seconds: Optional[float]) -> None:
        """Cache `token`; a missing or too-short lifetime disables caching."""
        with self._state_lock:
            if token and expires_in_seconds and expires_in_seconds >= MIN_CACHEABLE_SECONDS:
                self._token = token
                self._expires_at = self._clock() + float(expires_in_seconds)
            else:
                self._token = None
                self._expires_at = 0.0

    def invalidate(self) -> None:
        """Drop the token, e.g. after LinkAja answered 401 (token expired)."""
        with self._state_lock:
            self._token = None
            self._expires_at = 0.0

    # -- fetch coordination --------------------------------------------------

    def _lock_for_current_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._fetch_lock is None or self._fetch_loop is not loop:
            self._fetch_lock = asyncio.Lock()
            self._fetch_loop = loop
        return self._fetch_lock

    async def get(self, fetch: Callable[[], Awaitable[tuple[str, Optional[float]]]]) -> str:
        """Return a valid token, calling `fetch` at most once per expiry.

        `fetch` returns `(token, expires_in_seconds)`. Concurrent callers on the
        same event loop wait for the in-flight fetch instead of starting their
        own; callers on other loops/threads at worst fetch once more, which is
        harmless.
        """
        token = self.peek()
        if token:
            return token
        async with self._lock_for_current_loop():
            token = self.peek()           # re-check: another coroutine may have fetched
            if token:
                return token
            token, expires_in = await fetch()
            self.store(token, expires_in)
            return token


# Shared caches keyed by (base_url, client_id): every service instance built
# from the same credentials reuses the same token.
_caches: dict[tuple[str, str], TokenCache] = {}
_caches_lock = threading.Lock()


def shared_token_cache(base_url: str, client_id: str) -> TokenCache:
    key = (base_url, client_id)
    with _caches_lock:
        cache = _caches.get(key)
        if cache is None:
            cache = _caches[key] = TokenCache()
        return cache


def reset_token_caches() -> None:
    """Forget every cached token (tests, credential rotation)."""
    with _caches_lock:
        _caches.clear()
