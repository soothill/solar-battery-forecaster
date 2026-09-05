import asyncio
import email.utils
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from solar_battery_forecaster.outbound import ExternalServiceError, RequestPacer


def pacer(attempts: int = 3, retry_after_max: float = 0.01) -> RequestPacer:
    return RequestPacer(
        minimum_spacing_seconds=0,
        max_attempts=attempts,
        retry_base_seconds=0.01,
        retry_max_seconds=0.01,
        retry_after_max_seconds=retry_after_max,
        jitter_seconds=0,
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
        response = await pacer().request(
            client, "GET", "https://secret.invalid/id/123", service="provider"
        )
    assert response.status_code == 200
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
            limiter.request(client, "GET", "https://one.invalid", service="provider"),
            limiter.request(client, "GET", "https://two.invalid", service="provider"),
        )
    assert peak == 1


@pytest.mark.asyncio
async def test_non_retryable_error_is_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExternalServiceError, match="provider returned HTTP 404") as caught:
            await pacer().request(
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
            await limiter.request(
                client, "GET", "https://secret.invalid/id/123", service="provider"
            )
        with pytest.raises(ExternalServiceError, match="request is deferred"):
            await limiter.request(
                client, "GET", "https://secret.invalid/id/123", service="provider"
            )

    assert calls == 1
