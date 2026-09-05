from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from solar_battery_forecaster.config import InfluxConfig, PropertyConfig

PROPERTY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


@dataclass(frozen=True)
class CurvePoint:
    at: datetime
    value: float

    def as_dict(self) -> dict[str, str | float]:
        return {"at": self.at.astimezone(UTC).isoformat(), "value": round(self.value, 3)}


@dataclass(frozen=True)
class TariffWindow:
    start: datetime
    stop: datetime

    def as_dict(self) -> dict[str, str]:
        return {
            "start": self.start.astimezone(UTC).isoformat(),
            "stop": self.stop.astimezone(UTC).isoformat(),
        }


def local_day_bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def project_battery_soc(
    forecast_energy: list[CurvePoint],
    starting_soc_percent: float,
    usable_capacity_kwh: float,
    expected_load_kwh: float,
    minimum_soc_percent: float,
    maximum_soc_percent: float,
) -> list[CurvePoint]:
    """Build a transparent first-pass SoC plan from conservative forecast energy.

    Until a learned interval load model exists, the configured daily load is spread
    evenly across the forecast intervals. The line is a projection, never a control input.
    """
    if not forecast_energy:
        return []
    load_per_interval = expected_load_kwh / len(forecast_energy)
    soc_span = maximum_soc_percent - minimum_soc_percent
    stored_kwh = (
        (starting_soc_percent - minimum_soc_percent) / soc_span * usable_capacity_kwh
    )
    stored_kwh = min(usable_capacity_kwh, max(0.0, stored_kwh))
    result: list[CurvePoint] = []
    for point in forecast_energy:
        stored_kwh = min(
            usable_capacity_kwh,
            max(0.0, stored_kwh + point.value - load_per_interval),
        )
        projected_soc = minimum_soc_percent + stored_kwh / usable_capacity_kwh * soc_span
        result.append(CurvePoint(point.at, projected_soc))
    return result


class InfluxDashboardRepository:
    """Narrow, read-only view over the collector's InfluxDB measurements."""

    def __init__(self, influx: InfluxConfig, client: Any | None = None) -> None:
        if client is None:
            from influxdb_client import InfluxDBClient

            client = InfluxDBClient(url=influx.url, token=influx.token, org=influx.org)
        self.client = client
        self.influx = influx

    def close(self) -> None:
        self.client.close()

    def _points(
        self,
        measurement: str,
        field: str,
        property_id: str,
        start: datetime,
        stop: datetime,
        aggregate: str | None = None,
    ) -> list[CurvePoint]:
        if not PROPERTY_ID_PATTERN.fullmatch(property_id):
            raise ValueError("invalid property ID")
        start_text = start.astimezone(UTC).isoformat()
        stop_text = stop.astimezone(UTC).isoformat()
        aggregation = ""
        if aggregate:
            aggregation = (
                f"  |> aggregateWindow(every: {aggregate}, fn: mean, createEmpty: false)\n"
            )
        query = f'''from(bucket: "{self.influx.bucket}")
  |> range(start: time(v: "{start_text}"), stop: time(v: "{stop_text}"))
  |> filter(fn: (r) => r._measurement == "{measurement}")
  |> filter(fn: (r) => r.property == "{property_id}")
  |> filter(fn: (r) => r._field == "{field}")
{aggregation}  |> keep(columns: ["_time", "_value"])
'''
        tables = self.client.query_api().query(query=query, org=self.influx.org)
        return [
            CurvePoint(record.get_time(), float(record.get_value()))
            for table in tables
            for record in table.records
            if record.get_value() is not None
        ]

    def _latest_before(
        self,
        measurement: str,
        field: str,
        property_id: str,
        before: datetime,
    ) -> float | None:
        points = self._points(
            measurement,
            field,
            property_id,
            before - timedelta(days=2),
            before,
        )
        return points[-1].value if points else None

    def _forecast_points(
        self,
        field: str,
        property_id: str,
        start: datetime,
        stop: datetime,
    ) -> list[CurvePoint]:
        if not PROPERTY_ID_PATTERN.fullmatch(property_id):
            raise ValueError("invalid property ID")
        start_text = start.astimezone(UTC).isoformat()
        stop_text = stop.astimezone(UTC).isoformat()
        query = f'''from(bucket: "{self.influx.bucket}")
  |> range(start: time(v: "{start_text}"), stop: time(v: "{stop_text}"))
  |> filter(fn: (r) => r._measurement == "pv_forecast")
  |> filter(fn: (r) => r.property == "{property_id}" and r.role == "overnight")
  |> filter(fn: (r) => r._field == "{field}")
  |> keep(columns: ["_time", "_value", "snapshot"])
  |> sort(columns: ["_time"])
'''
        tables = self.client.query_api().query(query=query, org=self.influx.org)
        grouped: dict[str, list[CurvePoint]] = {}
        for table in tables:
            for record in table.records:
                snapshot = str(record.values.get("snapshot", ""))
                grouped.setdefault(snapshot, []).append(
                    CurvePoint(record.get_time(), float(record.get_value()))
                )
        expected_points = int((stop - start).total_seconds() / 3600)
        complete = {
            snapshot: points
            for snapshot, points in grouped.items()
            if len(points) == expected_points
        }
        if not complete:
            return []
        snapshot = min(complete)
        return complete[snapshot]

    def _tariff_windows(
        self, property_id: str, start: datetime, stop: datetime
    ) -> list[TariffWindow]:
        if not PROPERTY_ID_PATTERN.fullmatch(property_id):
            raise ValueError("invalid property ID")
        start_text = start.astimezone(UTC).isoformat()
        stop_text = stop.astimezone(UTC).isoformat()
        query = f'''from(bucket: "{self.influx.bucket}")
  |> range(start: time(v: "{start_text}"), stop: time(v: "{stop_text}"))
  |> filter(fn: (r) => r._measurement == "electricity_tariff")
  |> filter(fn: (r) => r.property == "{property_id}")
  |> filter(fn: (r) => r._field == "is_cheap" or r._field == "interval_minutes")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> filter(fn: (r) => r.is_cheap == true)
  |> keep(columns: ["_time", "interval_minutes"])
'''
        tables = self.client.query_api().query(query=query, org=self.influx.org)
        return [
            TariffWindow(
                record.get_time(),
                record.get_time() + timedelta(minutes=float(record.values["interval_minutes"])),
            )
            for table in tables
            for record in table.records
        ]

    def curve(self, prop: PropertyConfig, day: date) -> dict[str, Any]:
        start, stop = local_day_bounds(day, prop.timezone)
        forecast_power = self._forecast_points(
            "conservative_power_kw", prop.id, start, stop
        )
        forecast_energy = self._forecast_points(
            "conservative_energy_kwh", prop.id, start, stop
        )
        actual_power = self._points("energy_telemetry", "pv_power_kw", prop.id, start, stop, "15m")
        actual_soc = self._points(
            "energy_telemetry", "battery_soc_percent", prop.id, start, stop, "15m"
        )
        cheap_intervals = self._tariff_windows(prop.id, start, stop)
        issued_at = self._forecast_points(
            "issued_at_epoch", prop.id, start, stop
        )
        target_soc = self._latest_before("battery_decision", "target_soc_percent", prop.id, start)
        grid_charge = self._latest_before("battery_decision", "grid_charge_kwh", prop.id, start)
        charge_cost = self._latest_before(
            "battery_decision", "estimated_charge_cost_pence", prop.id, start
        )
        correction_factor = self._latest_before(
            "battery_decision", "correction_factor", prop.id, start
        )
        if target_soc is None:
            target_soc = actual_soc[0].value if actual_soc else prop.battery.minimum_soc_percent
        planned_soc = project_battery_soc(
            forecast_energy=forecast_energy,
            starting_soc_percent=target_soc,
            usable_capacity_kwh=prop.battery.usable_capacity_kwh,
            expected_load_kwh=prop.load.expected_kwh_until_next_cheap_window,
            minimum_soc_percent=prop.battery.minimum_soc_percent,
            maximum_soc_percent=prop.battery.maximum_soc_percent,
        )
        # Telemetry is aggregated into 15-minute mean power buckets above.
        actual_generation_kwh = sum(point.value * 0.25 for point in actual_power)
        return {
            "property_id": prop.id,
            "local_date": day.isoformat(),
            "timezone": prop.timezone,
            "generated_at": datetime.now(UTC).isoformat(),
            "window": {"start": start.isoformat(), "stop": stop.isoformat()},
            "limits": {"minimum_soc_percent": prop.battery.minimum_soc_percent},
            "assumptions": {
                "soc_projection": "Configured load is spread evenly across forecast intervals.",
                "starting_soc_percent": round(target_soc, 2),
            },
            "summary": {
                "forecast_generation_kwh": round(sum(p.value for p in forecast_energy), 2),
                "actual_generation_kwh": (
                    round(actual_generation_kwh, 2) if actual_power else None
                ),
                "latest_actual_soc_percent": (
                    round(actual_soc[-1].value, 1) if actual_soc else None
                ),
                "target_soc_percent": round(target_soc, 1),
                "recommended_grid_charge_kwh": (
                    round(grid_charge, 2) if grid_charge is not None else None
                ),
                "estimated_charge_cost_pence": (
                    round(charge_cost, 1) if charge_cost is not None else None
                ),
                "correction_factor": (
                    round(correction_factor, 3) if correction_factor is not None else None
                ),
                "forecast_issued_at": (
                    datetime.fromtimestamp(issued_at[-1].value, UTC).isoformat()
                    if issued_at
                    else None
                ),
            },
            "series": {
                "forecast_generation_kw": [p.as_dict() for p in forecast_power],
                "actual_generation_kw": [p.as_dict() for p in actual_power],
                "planned_soc_percent": [p.as_dict() for p in planned_soc],
                "actual_soc_percent": [p.as_dict() for p in actual_soc],
                "cheap_rate_intervals": [
                    interval.as_dict() for interval in cheap_intervals
                ],
            },
        }
