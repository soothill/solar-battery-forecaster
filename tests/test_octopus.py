from datetime import UTC, datetime

import httpx
import pytest

from solar_battery_forecaster.adapters.tariff.octopus import OctopusTariff
from solar_battery_forecaster.config import TariffConfig


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
