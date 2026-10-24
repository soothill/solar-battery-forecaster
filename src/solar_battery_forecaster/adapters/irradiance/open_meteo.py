from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx

from solar_battery_forecaster.adapters.forecast.open_meteo import (
    compass_to_open_meteo_azimuth,
    geometry_id,
    irradiance_value,
)
from solar_battery_forecaster.config import PropertyConfig
from solar_battery_forecaster.models import ArrayIrradiance, HistoricalIrradianceInterval
from solar_battery_forecaster.outbound import RequestPacer

API_URL = "https://satellite-api.open-meteo.com/v1/archive"
MODEL = "satellite_radiation_seamless"
METRICS = ("shortwave_radiation", "global_tilted_irradiance")


class OpenMeteoSatellite:
    name = "open_meteo_satellite"
    model = MODEL

    def __init__(self, client: httpx.AsyncClient, pacer: RequestPacer) -> None:
        self._client = client
        self._pacer = pacer

    async def close(self) -> None:
        # The worker owns the shared connection and pacing lifecycle.
        pass

    async def fetch(
        self, prop: PropertyConfig, day: date
    ) -> list[HistoricalIrradianceInterval]:
        start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        stop = start + timedelta(days=1)
        collected_at = datetime.now(UTC)
        samples: dict[datetime, list[ArrayIrradiance]] = {
            start + timedelta(hours=hour): [] for hour in range(24)
        }
        for array in prop.arrays:
            payload = await self._pacer.request_json(
                self._client, "GET", API_URL, service="satellite irradiance provider",
                params={
                    "latitude": prop.latitude, "longitude": prop.longitude,
                    "models": self.model, "hourly": ",".join(METRICS),
                    "tilt": array.tilt_degrees,
                    "azimuth": compass_to_open_meteo_azimuth(array.azimuth_degrees),
                    "timezone": "UTC", "start_date": day.isoformat(),
                    # Include the following midnight: values describe the preceding hour.
                    "end_date": (day + timedelta(days=1)).isoformat(),
                },
            )
            if not isinstance(payload, dict) or payload.get("utc_offset_seconds", 0) != 0:
                raise ValueError("satellite provider returned invalid UTC response")
            hourly, units = payload.get("hourly"), payload.get("hourly_units")
            if not isinstance(hourly, dict) or not isinstance(units, dict):
                raise ValueError("satellite provider returned invalid hourly data")
            if any(units.get(metric) != "W/m²" for metric in METRICS):
                raise ValueError("satellite provider returned invalid irradiance units")
            times = hourly.get("time")
            values = [hourly.get(metric) for metric in METRICS]
            if (
                not isinstance(times, list) or len(times) > 48
                or any(not isinstance(items, list) or len(items) != len(times) for items in values)
            ):
                raise ValueError("satellite provider returned invalid hourly arrays")
            seen: set[datetime] = set()
            by_start: dict[datetime, tuple[float | None, float | None]] = {}
            for time_text, ghi, gti in zip(times, *values, strict=True):
                if not isinstance(time_text, str):
                    raise ValueError("satellite provider returned invalid timestamp")
                end = datetime.fromisoformat(time_text)
                if end.tzinfo is None:
                    end = end.replace(tzinfo=UTC)
                if end.utcoffset() != timedelta(0):
                    raise ValueError("satellite provider returned non-UTC timestamp")
                if (
                    end in seen or end.minute or end.second or end.microsecond
                    or not start <= end < stop + timedelta(days=1)
                ):
                    raise ValueError("satellite provider returned duplicate or unaligned hours")
                seen.add(end)
                pair = (irradiance_value(ghi), irradiance_value(gti))
                interval_start = end - timedelta(hours=1)
                if start <= interval_start < stop:
                    by_start[interval_start] = pair
            geometry = geometry_id(prop, array)
            for at in samples:
                ghi, gti = by_start.get(at, (None, None))
                samples[at].append(ArrayIrradiance(
                    array.name, array.tilt_degrees, array.azimuth_degrees, ghi, gti, geometry,
                ))
        return [HistoricalIrradianceInterval(
            at, at + timedelta(hours=1), collected_at, self.name, self.model, tuple(items)
        ) for at, items in samples.items()]
