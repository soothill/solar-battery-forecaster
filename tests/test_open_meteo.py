from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from solar_battery_forecaster.adapters.forecast.open_meteo import (
    OpenMeteoForecast,
    compass_to_open_meteo_azimuth,
)
from solar_battery_forecaster.config import PropertyConfig
from solar_battery_forecaster.storage import InfluxStore


@pytest.fixture(autouse=True)
def fixed_issue_time(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 6, 21, 30, tzinfo=UTC)
    monkeypatch.setattr("solar_battery_forecaster.adapters.forecast.open_meteo.datetime", Clock)


def hours():
    return [(datetime(2026, 9, 6) + timedelta(hours=i)).isoformat() for i in range(72)]


def test_azimuth_conversion() -> None:
    assert compass_to_open_meteo_azimuth(0) == -180
    assert compass_to_open_meteo_azimuth(90) == -90
    assert compass_to_open_meteo_azimuth(180) == 0
    assert compass_to_open_meteo_azimuth(270) == 90


def property_config() -> PropertyConfig:
    return PropertyConfig.model_validate(
        {
            "id": "test-home",
            "timezone": "Europe/London",
            "latitude": 51.5,
            "longitude": -0.1,
            "arrays": [
                {
                    "name": "east",
                    "panel_count": 10,
                    "panel_power_w": 500,
                    "tilt_degrees": 35,
                    "azimuth_degrees": 90,
                    "performance_ratio": 0.8,
                },
                {
                    "name": "west",
                    "panel_count": 10,
                    "panel_power_w": 500,
                    "tilt_degrees": 35,
                    "azimuth_degrees": 270,
                    "performance_ratio": 0.8,
                },
            ],
            "inverter": {
                "rated_power_kw": 6,
                "app_key": "key",
                "app_secret": "secret",
                "system_id": "system",
            },
            "battery": {
                "usable_capacity_kwh": 9,
                "max_charge_power_kw": 6,
            },
            "tariff": {"product_code": "P", "tariff_code": "T"},
        }
    )


@pytest.mark.asyncio
async def test_fetch_combines_arrays_and_caps_at_inverter_rating() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "hourly": {
                    "time": hours(),
                    "shortwave_radiation": [600] * 72,
                    "global_tilted_irradiance": [1000] * 72,
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenMeteoForecast(client).fetch(property_config())

    assert len(requests) == 2
    assert requests[0].url.params["azimuth"] == "-90.0"
    assert requests[1].url.params["azimuth"] == "90.0"
    assert len(result) == 72
    assert result[0].start == datetime(2026, 9, 5, 23, tzinfo=UTC)
    assert result[0].end == datetime(2026, 9, 6, tzinfo=UTC)
    assert result[0].energy_kwh == 6
    assert result[0].power_kw == 6
    assert len(result[0].irradiance) == 2
    assert result[0].irradiance[0].ghi_w_m2 == 600
    assert result[0].irradiance[0].gti_w_m2 == 1000
    assert result[0].irradiance[0].azimuth_degrees == 90
    assert result[0].irradiance[1].azimuth_degrees == 270
    assert result[0].irradiance[0].geometry != result[0].irradiance[1].geometry


@pytest.mark.asyncio
async def test_fetch_rejects_misaligned_provider_arrays() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "hourly": {
                    "time": ["2026-09-06T10:00"],
                    "global_tilted_irradiance": [100, 200],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match=r"zip\(\) argument 2 is longer"):
            await OpenMeteoForecast(client).fetch(property_config())


@pytest.mark.asyncio
async def test_fetch_rejects_non_finite_irradiance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "hourly": {
                    "time": ["2026-09-06T10:00"],
                    "global_tilted_irradiance": ["NaN"],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="invalid irradiance"):
            await OpenMeteoForecast(client).fetch(property_config())


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing_roof", "missing_hour", "duplicate", "mismatch"])
async def test_each_roof_requires_complete_unique_aligned_hours(fault):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        times, values = hours(), [100] * 72
        if calls == 2:
            if fault == "missing_roof":
                values = [None] * 72
            elif fault == "missing_hour":
                values[30] = None
            elif fault == "duplicate":
                times[30] = times[29]
            else:
                times.pop(0)
                values.pop(0)
        return httpx.Response(200, json={"hourly": {
            "time": times, "global_tilted_irradiance": values,
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="coverage|hours"):
            await OpenMeteoForecast(client).fetch(property_config())


@pytest.mark.parametrize("missing_roofs", [1, 2])
async def test_raw_only_hour_survives_fetch_and_same_batch_storage(missing_roofs):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        values = [200] * 72
        if calls <= missing_roofs:
            values[10] = None
        return httpx.Response(200, json={"hourly": {
            "time": hours(), "shortwave_radiation": [123] * 72,
            "global_tilted_irradiance": values,
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenMeteoForecast(client)
        forecasts = await adapter.fetch(property_config())
    missing_at = datetime(2026, 9, 6, 9, tzinfo=UTC)
    assert len(forecasts) == 71
    assert len(adapter.raw_intervals) == 72
    assert all(item.start != missing_at for item in forecasts)
    raw_only = next(item for item in adapter.raw_intervals if item.start == missing_at)
    assert all(item.ghi_w_m2 == 123 for item in raw_only.irradiance)
    assert not hasattr(raw_only, "energy_kwh")
    store = object.__new__(InfluxStore)
    store.config = SimpleNamespace(planning_bucket="planning")
    batches = []

    def capture(bucket, points, **kwargs):
        batches.append([point.to_line_protocol() for point in points])
        return "direct"

    store._write = capture
    store.write_forecast("test-home", forecasts, 1, 1, irradiance_intervals=adapter.raw_intervals)
    assert len(batches) == 1
    target_lines = [line for line in batches[0] if line.endswith(f" {int(missing_at.timestamp())}")]
    assert len(target_lines) == 2
    assert all(line.startswith("irradiance_forecast,") for line in target_lines)
    assert all("ghi_w_m2=123" in line for line in target_lines)
    assert all("snapshot=2026-09-06T21:30:00.000000Z" in line for line in batches[0])


async def test_failed_fetch_clears_previous_raw_snapshot():
    fail = False

    def handler(request):
        values = [200] * 72
        if fail:
            values[30] = None
        return httpx.Response(200, json={"hourly": {
            "time": hours(), "shortwave_radiation": [123] * 72,
            "global_tilted_irradiance": values,
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenMeteoForecast(client)
        await adapter.fetch(property_config())
        assert len(adapter.raw_intervals) == 72
        fail = True
        with pytest.raises(ValueError, match="coverage"):
            await adapter.fetch(property_config())
        assert adapter.raw_intervals == ()
