from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import httpx

from solar_battery_forecaster.config import PropertyConfig
from solar_battery_forecaster.models import ForecastInterval
from solar_battery_forecaster.outbound import RequestPacer, default_pacer

API_URL = "https://api.open-meteo.com/v1/forecast"
MAX_HOURLY_POINTS = 72
MAX_IRRADIANCE_W_M2 = 2_000


def compass_to_open_meteo_azimuth(compass_degrees: float) -> float:
    """Convert north-clockwise compass degrees to Open-Meteo's south-zero system."""
    converted = compass_degrees - 180
    return 180 if converted == -180 and compass_degrees > 0 else converted


class OpenMeteoForecast:
    name = "open_meteo"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        pacer: RequestPacer | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=30)
        self._owns_client = client is None
        self._pacer = pacer or default_pacer()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, prop: PropertyConfig) -> list[ForecastInterval]:
        issued_at = datetime.now(UTC)
        energy_by_end: dict[datetime, float] = defaultdict(float)

        for array in prop.arrays:
            payload = await self._pacer.request_json(
                self._client,
                "GET",
                API_URL,
                service="forecast provider",
                params={
                    "latitude": prop.latitude,
                    "longitude": prop.longitude,
                    "hourly": "global_tilted_irradiance",
                    "tilt": array.tilt_degrees,
                    "azimuth": compass_to_open_meteo_azimuth(array.azimuth_degrees),
                    "timezone": "UTC",
                    "forecast_days": 3,
                },
            )
            if not isinstance(payload, dict):
                raise ValueError("forecast provider returned an invalid response")
            hourly = payload.get("hourly", {})
            times = hourly.get("time", [])
            irradiances = hourly.get("global_tilted_irradiance", [])
            if not isinstance(times, list) or not isinstance(irradiances, list):
                raise ValueError("forecast provider returned invalid hourly arrays")
            if len(times) > MAX_HOURLY_POINTS or len(irradiances) > MAX_HOURLY_POINTS:
                raise ValueError("forecast provider returned too many hourly points")
            for time_text, irradiance in zip(
                times,
                irradiances,
                strict=True,
            ):
                if irradiance is None:
                    continue
                irradiance_value = float(irradiance)
                if not math.isfinite(irradiance_value) or not (
                    0 <= irradiance_value <= MAX_IRRADIANCE_W_M2
                ):
                    raise ValueError("forecast provider returned invalid irradiance")
                interval_end = datetime.fromisoformat(time_text).replace(tzinfo=UTC)
                # GTI is the mean W/m2 over the preceding hour. kWp is rated at 1000 W/m2.
                energy_by_end[interval_end] += (
                    irradiance_value / 1000
                    * array.capacity_kwp
                    * array.performance_ratio
                )

        result: list[ForecastInterval] = []
        for end, uncapped_energy in sorted(energy_by_end.items()):
            energy = min(uncapped_energy, prop.inverter.rated_power_kw)
            result.append(
                ForecastInterval(
                    start=end - timedelta(hours=1),
                    end=end,
                    energy_kwh=energy,
                    power_kw=energy,
                    issued_at=issued_at,
                    provider=self.name,
                )
            )
        return result
