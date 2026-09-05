import asyncio
import contextlib
import email.utils
import gzip
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from solar_battery_forecaster.outbound import ExternalServiceError, RequestPacer


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.closed = False

    async def __aiter__(self):
        yield self.content

    async def aclose(self) -> None:
        self.closed = True


def pacer(
    attempts: int = 3,
    retry_after_max: float = 0.01,
    max_response_bytes: int = 16_384,
) -> RequestPacer:
    return RequestPacer(
        minimum_spacing_seconds=0,
        max_attempts=attempts,
        retry_base_seconds=0.01,
        retry_max_seconds=0.01,
        retry_after_max_seconds=retry_after_max,
        jitter_seconds=0,
        max_response_bytes=max_response_bytes,
    )


@pytest.mark.asyncio
async def test_pacer_retries_retryable_status_without_leaking_url() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429 if calls == 1 else 200,
            headers={"Retry-After": "0.005"} if calls == 1 else None,
            json={},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        payload = await pacer().request_json(
            client, "GET", "https://secret.invalid/id/123", service="provider"
        )
    assert payload == {}
    assert calls == 2


@pytest.mark.asyncio
async def test_pacer_serializes_concurrent_requests() -> None:
    active = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        limiter = pacer()
        await asyncio.gather(
            limiter.request_json(client, "GET", "https://one.invalid", service="provider"),
            limiter.request_json(client, "GET", "https://two.invalid", service="provider"),
        )
    assert peak == 1


@pytest.mark.asyncio
async def test_non_retryable_error_is_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExternalServiceError, match="provider returned HTTP 404") as caught:
            await pacer().request_json(
                client, "GET", "https://secret.invalid/id/123", service="provider"
            )
    assert "secret.invalid" not in str(caught.value)


def test_retry_after_has_a_separate_explicit_cap() -> None:
    limiter = pacer(retry_after_max=90)
    assert limiter._backoff(1, "60") == 60  # noqa: SLF001
    with pytest.raises(ExternalServiceError, match="beyond the inline wait limit"):
        limiter._backoff(1, "120")  # noqa: SLF001
    assert limiter._backoff(8, None) == 0.01  # noqa: SLF001


def test_retry_after_http_date_is_supported() -> None:
    future = email.utils.format_datetime(
        datetime.now(UTC) + timedelta(seconds=30), usegmt=True
    )
    parsed = RequestPacer._retry_after_seconds(future)  # noqa: SLF001
    assert parsed is not None
    assert 0 < parsed <= 30


@pytest.mark.asyncio
async def test_over_limit_retry_after_defers_without_retrying_early() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        limiter = pacer(retry_after_max=90)
        with pytest.raises(ExternalServiceError, match="inline wait limit"):
            await limiter.request_json(
                client, "GET", "https://secret.invalid/id/123", service="provider"
            )
        with pytest.raises(ExternalServiceError, match="request is deferred"):
            await limiter.request_json(
                client, "GET", "https://secret.invalid/id/123", service="provider"
            )

    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("compressed", [False, True])
async def test_streaming_reader_caps_decompressed_response(compressed: bool) -> None:
    raw = json.dumps({"value": "x" * 20_000}).encode()
    body = gzip.compress(raw) if compressed else raw

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Content-Encoding": "gzip"} if compressed else None
        return httpx.Response(200, content=body, headers=headers)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExternalServiceError, match="response exceeded the size limit"):
            await pacer().request_json(
                client, "GET", "https://secret.invalid/data", service="provider"
            )


@pytest.mark.asyncio
async def test_streaming_reader_rejects_invalid_and_deep_json() -> None:
    bodies = [b"not-json", ("[" * 40 + "0" + "]" * 40).encode()]

    for body in bodies:
        def handler(request: httpx.Request, response_body: bytes = body) -> httpx.Response:
            return httpx.Response(200, content=response_body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ExternalServiceError, match="invalid JSON|overly complex"):
                await pacer().request_json(
                    client, "GET", "https://secret.invalid/data", service="provider"
                )


@pytest.mark.asyncio
async def test_streaming_reader_accepts_exact_byte_limit() -> None:
    limit = 16_384
    body = b'{"x":"' + b"x" * (limit - 8) + b'"}'
    assert len(body) == limit

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        payload = await pacer(max_response_bytes=limit).request_json(
            client, "GET", "https://secret.invalid/data", service="provider"
        )
    assert len(payload["x"]) == limit - 8


def test_response_size_limit_has_strict_bounds() -> None:
    with pytest.raises(ValueError, match="max_response_bytes"):
        pacer(max_response_bytes=16_383)
    with pytest.raises(ValueError, match="max_response_bytes"):
        pacer(max_response_bytes=8_388_609)


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "body"), [(200, b'{}'), (404, b'{}'), (200, b'x' * 20_000)])
async def test_streaming_response_is_closed_on_every_path(status: int, body: bytes) -> None:
    stream = TrackingStream(body)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with contextlib.suppress(ExternalServiceError):
            await pacer().request_json(
                client, "GET", "https://secret.invalid/data", service="provider"
            )
    assert stream.closed is True
