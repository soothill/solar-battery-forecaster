from datetime import UTC, datetime, timedelta

import httpx
import pytest

from solar_battery_forecaster.adapters.tariff.octopus import OctopusTariff
from solar_battery_forecaster.config import TariffConfig
from solar_battery_forecaster.models import TariffInterval
from solar_battery_forecaster.tariffs import validated_tariff_timeline


@pytest.mark.asyncio
async def test_fetch_marks_threshold_as_cheap_and_sorts_results() -> None:
    observed_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_request
        observed_request = request
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "valid_from": "2026-09-06T00:30:00Z",
                        "valid_to": "2026-09-06T01:00:00Z",
                        "value_inc_vat": 15,
                    },
                    {
                        "valid_from": "2026-09-06T00:00:00Z",
                        "valid_to": "2026-09-06T00:30:00Z",
                        "value_inc_vat": 10,
                    },
                ]
            },
        )

    config = TariffConfig(product_code="AGILE-TEST", tariff_code="E-1R-TEST-A")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OctopusTariff(config, client).fetch(
            datetime(2026, 9, 5, 12, tzinfo=UTC)
        )

    assert observed_request is not None
    assert "/products/AGILE-TEST/electricity-tariffs/E-1R-TEST-A/" in str(
        observed_request.url
    )
    assert observed_request.url.params["period_from"] == "2026-09-05T10:00:00Z"
    assert observed_request.url.params["period_to"] == "2026-09-07T12:00:00Z"
    assert [item.price_pence_per_kwh for item in result] == [10, 15]
    assert [item.is_cheap for item in result] == [True, False]


@pytest.mark.asyncio
async def test_fetch_rejects_non_finite_price() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "valid_from": "2026-09-06T00:00:00Z",
                        "valid_to": "2026-09-06T00:30:00Z",
                        "value_inc_vat": "NaN",
                    }
                ]
            },
        )

    config = TariffConfig(product_code="AGILE-TEST", tariff_code="E-1R-TEST-A")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="invalid price"):
            await OctopusTariff(config, client).fetch(datetime(2026, 9, 5, tzinfo=UTC))


@pytest.mark.asyncio
async def test_fetch_rejects_overlapping_intervals() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "valid_from": "2026-09-06T00:00:00Z",
                        "valid_to": "2026-09-06T01:00:00Z",
                        "value_inc_vat": 10,
                    },
                    {
                        "valid_from": "2026-09-06T00:30:00Z",
                        "valid_to": "2026-09-06T01:30:00Z",
                        "value_inc_vat": 5,
                    },
                ]
            },
        )

    config = TariffConfig(product_code="AGILE-TEST", tariff_code="E-1R-TEST-A")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="overlap"):
            await OctopusTariff(config, client).fetch(datetime(2026, 9, 5, tzinfo=UTC))


@pytest.mark.asyncio
async def test_long_and_open_ended_rates_paginate_and_clip():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            rows = [{"valid_from": "2026-09-04T00:00:00Z",
                     "valid_to": "2026-09-05T06:00:00Z", "value_inc_vat": 7}]
            next_page = str(request.url.copy_set_param("page", "2"))
        else:
            rows = [{"valid_from": "2026-09-05T06:00:00Z",
                     "valid_to": None, "value_inc_vat": 25}]
            next_page = None
        return httpx.Response(200, json={"results": rows, "next": next_page})

    start = datetime(2026, 9, 5, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OctopusTariff(TariffConfig(product_code="P", tariff_code="T"), client)
        result = await adapter.fetch(start)
    assert len(calls) == 2
    assert result[0].start == start - timedelta(hours=2)
    assert result[0].end == result[1].start == start + timedelta(hours=6)
    assert result[1].end == start + timedelta(days=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("next_page", [
    "https://example.invalid/", "/v1/accounts/", "?page=2#fragment",
])
async def test_pagination_rejects_changed_endpoint(next_page):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"results": [], "next": next_page})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="pagination"):
            await OctopusTariff(TariffConfig(product_code="P", tariff_code="T"), client).fetch()
    assert calls == 1


@pytest.mark.parametrize("price", [float("nan"), float("inf"), 1001])
def test_stored_tariff_rejects_invalid_price(price):
    start = datetime(2026, 9, 5, tzinfo=UTC)
    with pytest.raises(ValueError, match="price"):
        validated_tariff_timeline([TariffInterval(start, start + timedelta(hours=6), price, False)])


def test_stored_tariff_rejects_naive_timestamp():
    start = datetime(2026, 9, 5)
    with pytest.raises(ValueError, match="timezone-aware"):
        validated_tariff_timeline([TariffInterval(start, start + timedelta(hours=6), 7, True)])
