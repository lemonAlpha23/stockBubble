"""Tiny resilient JSON HTTP client used by every remote adapter."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Mapping
from contextlib import suppress
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Any

import httpx

from stockbubble.domain.errors import ProviderRateLimited, ProviderUnavailable

LOGGER = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class JsonHttpClient:
    """GET/JSON helper with bounded retries, backoff and uniform errors."""

    def __init__(
        self,
        base_url: str,
        *,
        provider: str,
        timeout: float = 12.0,
        retries: int = 2,
        headers: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        rate_limit_cooldown: float = 0,
    ) -> None:
        self._provider = provider
        self._retries = max(0, retries)
        self._rate_limit_cooldown = max(0.0, rate_limit_cooldown)
        self._blocked_until = 0.0
        self._owns_client = client is None
        if client is not None:
            self._client = client
        else:
            merged: dict[str, str] = {
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": DEFAULT_USER_AGENT,
            }
            if headers:
                merged.update(headers)
            self._client = httpx.AsyncClient(
                base_url=base_url,
                timeout=httpx.Timeout(timeout),
                headers=merged,
                follow_redirects=True,
            )

    @property
    def http(self) -> httpx.AsyncClient:
        """Expose the underlying session (needed for cookie handling)."""
        return self._client

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int | float] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Perform a request, retrying transport errors and retryable statuses."""
        attempts = self._retries + 1
        last_status: int | None = None
        last_error: str = "no attempt executed"
        for attempt in range(attempts):
            remaining = self._blocked_until - monotonic()
            if remaining > 0:
                raise ProviderRateLimited(self._provider, math.ceil(remaining))
            retry_after: float | None = None
            try:
                response = await self._client.request(method, path, params=params, headers=headers)
            except httpx.HTTPError as exc:  # transport / timeout
                last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.warning("%s: %s %s failed (%s)", self._provider, method, path, last_error)
            else:
                if response.status_code == 429 and self._rate_limit_cooldown:
                    cooldown = self._rate_limit_cooldown
                    raw = response.headers.get("retry-after")
                    if raw:
                        try:
                            cooldown = max(cooldown, float(raw))
                        except ValueError:
                            with suppress(ValueError, TypeError, OverflowError):
                                cooldown = max(
                                    cooldown, parsedate_to_datetime(raw).timestamp() - time.time()
                                )
                    cooldown = min(3600.0, cooldown)
                    self._blocked_until = monotonic() + cooldown
                    raise ProviderRateLimited(self._provider, math.ceil(cooldown))
                if response.status_code not in RETRY_STATUS:
                    return response
                last_status = response.status_code
                last_error = f"HTTP {response.status_code}"
                retry_after = self._retry_after(response)
                LOGGER.warning(
                    "%s: %s %s -> %s (attempt %d/%d)",
                    self._provider,
                    method,
                    path,
                    response.status_code,
                    attempt + 1,
                    attempts,
                )
            if attempt + 1 < attempts:
                await asyncio.sleep(self._backoff(attempt, retry_after))
        detail = f"HTTP {last_status}" if last_status is not None else last_error
        raise ProviderUnavailable(self._provider, f"{method} {path} failed: {detail}")

    async def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, str | int | float] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        response = await self.request("GET", path, params=params, headers=headers)
        if response.status_code >= 400:
            snippet = response.text[:200].replace("\n", " ")
            raise ProviderUnavailable(
                self._provider,
                f"GET {path} -> HTTP {response.status_code}: {snippet}",
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderUnavailable(
                self._provider,
                f"GET {path} returned non-JSON payload",
            ) from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        try:
            return min(5.0, max(0.0, float(raw)))
        except ValueError:
            return None

    @staticmethod
    def _backoff(attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return retry_after
        return min(4.0, 0.25 * 2.0**attempt)
