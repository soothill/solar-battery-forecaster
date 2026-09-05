from __future__ import annotations

import asyncio
import email.utils
import secrets
import time
from datetime import UTC, datetime
from typing import Any

import httpx


class ExternalServiceError(RuntimeError):
    """An intentionally URL-free error safe for operational logs."""


class RequestPacer:
    def __init__(
        self,
        *,
        minimum_spacing_seconds: float,
        max_attempts: int,
        retry_base_seconds: float,
        retry_max_seconds: float,
        retry_after_max_seconds: float,
        jitter_seconds: float,
    ) -> None:
        self.minimum_spacing_seconds = minimum_spacing_seconds
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.retry_after_max_seconds = retry_after_max_seconds
        self.jitter_seconds = jitter_seconds
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._defer_until = 0.0

    async def request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        service: str,
        **kwargs: Any,
    ) -> httpx.Response:
        async with self._lock:
            if time.monotonic() < self._defer_until:
                raise ExternalServiceError(f"{service} request is deferred")
            for attempt in range(1, self.max_attempts + 1):
                await self._wait_for_spacing()
                try:
                    response = await client.request(method, url, **kwargs)
                except httpx.HTTPError as exc:
                    if attempt == self.max_attempts:
                        raise ExternalServiceError(
                            f"{service} request failed after {attempt} attempts"
                        ) from exc
                    await asyncio.sleep(self._backoff(attempt, None))
                    continue
                finally:
                    self._last_request_at = time.monotonic()
                if response.status_code < 400:
                    return response
                if response.status_code not in {429, 500, 502, 503, 504}:
                    raise ExternalServiceError(
                        f"{service} returned HTTP {response.status_code}"
                    )
                if attempt == self.max_attempts:
                    raise ExternalServiceError(
                        f"{service} remained unavailable after {attempt} attempts"
                    )
                await asyncio.sleep(
                    self._backoff(attempt, response.headers.get("Retry-After"))
                )
        raise AssertionError("unreachable")

    async def _wait_for_spacing(self) -> None:
        remaining = self.minimum_spacing_seconds - (
            time.monotonic() - self._last_request_at
        )
        if remaining > 0:
            await asyncio.sleep(remaining)

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        parsed = self._retry_after_seconds(retry_after)
        if parsed is not None:
            if parsed > self.retry_after_max_seconds:
                self._defer_until = max(self._defer_until, time.monotonic() + parsed)
                raise ExternalServiceError(
                    "provider requested a retry delay beyond the inline wait limit"
                )
            base = parsed
        else:
            base = min(self.retry_max_seconds, self.retry_base_seconds * 2 ** (attempt - 1))
        return base + secrets.SystemRandom().uniform(
            0, self.jitter_seconds
        )

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return max(0.0, (parsed - datetime.now(UTC)).total_seconds())


def default_pacer() -> RequestPacer:
    return RequestPacer(
        minimum_spacing_seconds=0.5,
        max_attempts=3,
        retry_base_seconds=1,
        retry_max_seconds=30,
        retry_after_max_seconds=300,
        jitter_seconds=0.5,
    )
