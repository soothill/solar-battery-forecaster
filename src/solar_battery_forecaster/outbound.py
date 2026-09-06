from __future__ import annotations

import asyncio
import email.utils
import json
import math
import secrets
import time
import zlib
from datetime import UTC, datetime
from typing import Any

import httpx

MIN_RESPONSE_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_RESPONSE_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 50_000
MAX_RETRY_AFTER_SECONDS = 7 * 24 * 3600


class ExternalServiceError(RuntimeError):
    """An intentionally URL-free error safe for operational logs."""


def _validate_json_shape(value: Any) -> None:
    """Bound parsed JSON complexity without recursively walking attacker input."""
    nodes = 0
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ExternalServiceError("provider returned overly complex JSON")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


async def _read_bounded_json(
    response: httpx.Response, *, service: str, max_response_bytes: int
) -> Any:
    content = bytearray()
    try:
        encoding = response.headers.get("Content-Encoding", "identity").lower().strip()
        if response.is_stream_consumed:
            # HTTPX has already decoded content returned pre-buffered by a custom transport.
            encoding = "identity"
        if encoding not in {"", "identity", "gzip", "deflate"}:
            raise ExternalServiceError(f"{service} returned an unsupported encoding")
        decoder = (
            zlib.decompressobj(16 + zlib.MAX_WBITS)
            if encoding == "gzip"
            else zlib.decompressobj()
            if encoding == "deflate"
            else None
        )
        raw_bytes = 0

        def consume(raw_chunk: bytes) -> None:
            nonlocal raw_bytes
            raw_bytes += len(raw_chunk)
            if raw_bytes > max_response_bytes + 64 * 1024:
                raise ExternalServiceError(f"{service} response exceeded the size limit")
            if decoder is None:
                decoded = raw_chunk
            else:
                decoded = decoder.decompress(
                    raw_chunk, max_response_bytes - len(content) + 1
                )
                if decoder.unconsumed_tail:
                    raise ExternalServiceError(
                        f"{service} response exceeded the size limit"
                    )
            if len(content) + len(decoded) > max_response_bytes:
                raise ExternalServiceError(f"{service} response exceeded the size limit")
            content.extend(decoded)

        # Mock/custom transports may legally return an already-consumed response. Production
        # HTTPTransport responses take the streamed branch below.
        if response.is_stream_consumed:
            consume(response.content)
        else:
            async for raw_chunk in response.aiter_raw(chunk_size=64 * 1024):
                consume(raw_chunk)
        if decoder is not None:
            decoded = decoder.flush(max_response_bytes - len(content) + 1)
            if len(content) + len(decoded) > max_response_bytes:
                raise ExternalServiceError(f"{service} response exceeded the size limit")
            content.extend(decoded)
            if not decoder.eof or decoder.unused_data:
                raise ExternalServiceError(f"{service} returned invalid encoded content")
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            raise ExternalServiceError(f"{service} returned invalid JSON") from exc
        _validate_json_shape(payload)
        return payload
    except (httpx.DecodingError, zlib.error) as exc:
        raise ExternalServiceError(f"{service} returned invalid encoded content") from exc


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
        max_response_bytes: int = DEFAULT_RESPONSE_BYTES,
    ) -> None:
        pacing = (
            minimum_spacing_seconds, retry_base_seconds, retry_max_seconds,
            retry_after_max_seconds, jitter_seconds,
        )
        if any(not math.isfinite(value) or not 0 <= value <= 3600 for value in pacing):
            raise ValueError("request pacing values must be finite and bounded")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not (
            1 <= max_attempts <= 10
        ):
            raise ValueError("request attempts must be between 1 and 10")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not MIN_RESPONSE_BYTES <= max_response_bytes <= MAX_RESPONSE_BYTES
        ):
            raise ValueError(
                f"max_response_bytes must be between {MIN_RESPONSE_BYTES} and "
                f"{MAX_RESPONSE_BYTES}"
            )
        self.minimum_spacing_seconds = minimum_spacing_seconds
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.retry_after_max_seconds = retry_after_max_seconds
        self.jitter_seconds = jitter_seconds
        self.max_response_bytes = max_response_bytes
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._defer_until = 0.0

    async def request_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        service: str,
        **kwargs: Any,
    ) -> Any:
        request_kwargs = dict(kwargs)
        headers = httpx.Headers(request_kwargs.pop("headers", None))
        headers["Accept-Encoding"] = "gzip"
        async with self._lock:
            if time.monotonic() < self._defer_until:
                raise ExternalServiceError(f"{service} request is deferred")
            for attempt in range(1, self.max_attempts + 1):
                await self._wait_for_spacing()
                response: httpx.Response | None = None
                delay: float | None = None
                try:
                    request = client.build_request(
                        method, url, headers=headers, **request_kwargs
                    )
                    response = await client.send(request, stream=True)
                    if response.status_code < 400:
                        return await _read_bounded_json(
                            response,
                            service=service,
                            max_response_bytes=self.max_response_bytes,
                        )
                    if response.status_code not in {429, 500, 502, 503, 504}:
                        raise ExternalServiceError(
                            f"{service} returned HTTP {response.status_code}"
                        )
                    if attempt == self.max_attempts:
                        parsed = self._retry_after_seconds(response.headers.get("Retry-After"))
                        if parsed is not None:
                            self._defer_until = max(
                                self._defer_until, time.monotonic() + parsed
                            )
                        raise ExternalServiceError(
                            f"{service} remained unavailable after {attempt} attempts"
                        )
                    delay = self._backoff(attempt, response.headers.get("Retry-After"))
                except ExternalServiceError:
                    raise
                except httpx.HTTPError as exc:
                    if attempt == self.max_attempts:
                        raise ExternalServiceError(
                            f"{service} request failed after {attempt} attempts"
                        ) from exc
                    delay = self._backoff(attempt, None)
                finally:
                    if response is not None:
                        try:
                            await response.aclose()
                        except httpx.HTTPError as exc:
                            raise ExternalServiceError(
                                f"{service} response could not be closed"
                            ) from exc
                    self._last_request_at = time.monotonic()
                if delay is not None:
                    await asyncio.sleep(delay)
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
        return base + secrets.SystemRandom().uniform(0, self.jitter_seconds)

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float | None:
        if not value:
            return None
        try:
            seconds = float(value)
            if not math.isfinite(seconds):
                return None
            return min(MAX_RETRY_AFTER_SECONDS, max(0.0, seconds))
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return min(
                MAX_RETRY_AFTER_SECONDS,
                max(0.0, (parsed - datetime.now(UTC)).total_seconds()),
            )


def default_pacer() -> RequestPacer:
    return RequestPacer(
        minimum_spacing_seconds=0.5,
        max_attempts=3,
        retry_base_seconds=1,
        retry_max_seconds=30,
        retry_after_max_seconds=300,
        jitter_seconds=0.5,
    )
