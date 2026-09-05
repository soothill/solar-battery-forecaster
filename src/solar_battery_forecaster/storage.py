from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from solar_battery_forecaster.config import InfluxConfig
from solar_battery_forecaster.models import (
    BatteryDecision,
    ForecastInterval,
    ForecastSnapshot,
    TariffInterval,
    Telemetry,
    forecast_snapshot_id,
)


class InfluxStore:
    def __init__(self, config: InfluxConfig) -> None:
        self.config = config
        self.client = InfluxDBClient(url=config.url, token=config.token, org=config.org)
        self.writer = self.client.write_api(write_options=SYNCHRONOUS)

    def close(self) -> None:
        self.client.close()

    def ping(self) -> bool:
        return bool(self.client.ping())

    def _write(self, points: Iterable[Point]) -> None:
        records = list(points)
        if records:
            self.writer.write(bucket=self.config.bucket, org=self.config.org, record=records)

    def write_forecast(
        self,
        property_id: str,
        intervals: list[ForecastInterval],
        correction_factor: float,
        conservative_multiplier: float,
    ) -> None:
        points = []
        for interval in intervals:
            corrected = interval.energy_kwh * correction_factor
            snapshot_id = forecast_snapshot_id(interval.issued_at)
            points.append(
                Point("pv_forecast")
                .tag("property", property_id)
                .tag("provider", interval.provider)
                .tag("role", "overnight")
                .tag("snapshot", snapshot_id)
                .field("raw_energy_kwh", interval.energy_kwh)
                .field("corrected_energy_kwh", corrected)
                .field("conservative_energy_kwh", corrected * conservative_multiplier)
                .field("raw_power_kw", interval.power_kw)
                .field("corrected_power_kw", interval.power_kw * correction_factor)
                .field(
                    "conservative_power_kw",
                    interval.power_kw * correction_factor * conservative_multiplier,
                )
                .field("correction_factor", correction_factor)
                .field(
                    "interval_minutes",
                    int((interval.end - interval.start).total_seconds() / 60),
                )
                .field("issued_at_epoch", int(interval.issued_at.timestamp()))
                .time(interval.start, WritePrecision.S)
            )
        self._write(points)

    def write_telemetry(self, property_id: str, source: str, item: Telemetry) -> None:
        point = Point("energy_telemetry").tag("property", property_id).tag("source", source)
        fields = {
            "pv_power_kw": item.pv_power_kw,
            "grid_power_kw": item.grid_power_kw,
            "battery_power_kw": item.battery_power_kw,
            "load_power_kw": item.load_power_kw,
            "battery_soc_percent": item.battery_soc_percent,
            "daily_pv_kwh": item.daily_pv_kwh,
            "lifetime_pv_kwh": item.lifetime_pv_kwh,
        }
        for name, value in fields.items():
            if value is not None:
                point = point.field(name, value)
        point = point.field("inverter_online", item.inverter_online)
        self._write([point.time(item.observed_at, WritePrecision.S)])

    def write_tariffs(
        self, property_id: str, provider: str, intervals: list[TariffInterval]
    ) -> None:
        points = [
            Point("electricity_tariff")
            .tag("property", property_id)
            .tag("provider", provider)
            .field("price_pence_per_kwh", item.price_pence_per_kwh)
            .field("is_cheap", item.is_cheap)
            .field("interval_minutes", int((item.end - item.start).total_seconds() / 60))
            .time(item.start, WritePrecision.S)
            for item in intervals
        ]
        self._write(points)

    def write_decision(self, property_id: str, item: BatteryDecision) -> None:
        point = (
            Point("battery_decision")
            .tag("property", property_id)
            .field("current_soc_percent", item.current_soc_percent)
            .field("target_soc_percent", item.target_soc_percent)
            .field("grid_charge_kwh", item.grid_charge_kwh)
            .field("raw_solar_kwh", item.raw_solar_kwh)
            .field("corrected_solar_kwh", item.corrected_solar_kwh)
            .field("conservative_solar_kwh", item.conservative_solar_kwh)
            .field("expected_load_kwh", item.expected_load_kwh)
            .field("reserve_kwh", item.reserve_kwh)
            .field("correction_factor", item.correction_factor)
            .field("forecast_snapshot_id", item.forecast_snapshot_id)
            .field("forecast_issued_at", item.forecast_issued_at.astimezone(UTC).isoformat())
            .field("soc_observed_at", item.soc_observed_at.astimezone(UTC).isoformat())
            .field(
                "tariff_coverage_start",
                item.tariff_coverage_start.astimezone(UTC).isoformat(),
            )
            .field(
                "tariff_coverage_stop",
                item.tariff_coverage_stop.astimezone(UTC).isoformat(),
            )
            .field("tariff_coverage_hours", item.tariff_coverage_hours)
            .field("cheap_duration_hours", item.cheap_duration_hours)
            .field("charge_limited_by_window", item.charge_limited_by_window)
            .field("reason", item.reason)
            .field("automation_enabled", False)
            .time(item.created_at, WritePrecision.S)
        )
        if item.cheap_rate_average_pence is not None:
            point = point.field(
                "cheap_rate_average_pence", item.cheap_rate_average_pence
            )
        if item.estimated_charge_cost_pence is not None:
            point = point.field(
                "estimated_charge_cost_pence", item.estimated_charge_cost_pence
            )
        self._write([point])

    def write_daily_result(
        self,
        property_id: str,
        day: date,
        forecast_kwh: float,
        actual_kwh: float,
        ratio: float,
        factor_after_update: float,
    ) -> None:
        timestamp = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        point = (
            Point("pv_daily")
            .tag("property", property_id)
            .field("forecast_kwh", forecast_kwh)
            .field("actual_kwh", actual_kwh)
            .field("daily_ratio", ratio)
            .field("bias_kwh", actual_kwh - forecast_kwh)
            .field("absolute_error_kwh", abs(actual_kwh - forecast_kwh))
            .field("correction_factor_after_update", factor_after_update)
            .time(timestamp, WritePrecision.S)
        )
        self._write([point])

    def latest_soc(self, property_id: str) -> float | None:
        reading = self.latest_soc_reading(property_id)
        return reading[0] if reading else None

    def latest_soc_reading(self, property_id: str) -> tuple[float, datetime] | None:
        query = f'''
from(bucket: "{self.config.bucket}")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "energy_telemetry")
  |> filter(fn: (r) => r.property == "{property_id}")
  |> filter(fn: (r) => r._field == "battery_soc_percent")
  |> last()
'''
        records = self._records(query)
        if not records:
            return None
        record = records[-1]
        return float(record.get_value()), record.get_time()

    def complete_forecast_snapshot(
        self,
        property_id: str,
        provider: str,
        start: datetime,
        stop: datetime,
        expected_points: int,
    ) -> ForecastSnapshot | None:
        start_text = start.astimezone(UTC).isoformat()
        stop_text = stop.astimezone(UTC).isoformat()
        query = f'''
from(bucket: "{self.config.bucket}")
  |> range(start: time(v: "{start_text}"), stop: time(v: "{stop_text}"))
  |> filter(fn: (r) => r._measurement == "pv_forecast")
  |> filter(fn: (r) => r.property == "{property_id}" and r.provider == "{provider}")
  |> filter(fn: (r) => r.role == "overnight")
  |> filter(fn: (r) => r._field == "raw_energy_kwh" or
      r._field == "correction_factor" or r._field == "issued_at_epoch")
  |> pivot(rowKey: ["_time", "snapshot"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
        grouped: dict[str, list[object]] = {}
        for record in self._records(query):
            snapshot_id = str(record.values.get("snapshot", ""))
            grouped.setdefault(snapshot_id, []).append(record)
        complete: list[ForecastSnapshot] = []
        for snapshot_id, records in grouped.items():
            if len(records) != expected_points:
                continue
            issued_epoch = float(records[0].values["issued_at_epoch"])
            complete.append(
                ForecastSnapshot(
                    provider=provider,
                    snapshot_id=snapshot_id,
                    issued_at=datetime.fromtimestamp(issued_epoch, UTC),
                    point_count=len(records),
                    raw_energy_kwh=sum(
                        float(record.values["raw_energy_kwh"]) for record in records
                    ),
                    correction_factor=float(records[0].values["correction_factor"]),
                )
            )
        return min(complete, key=lambda item: item.issued_at) if complete else None

    def forecast_point_count(
        self,
        property_id: str,
        provider: str,
        start: datetime,
        stop: datetime,
    ) -> int:
        start_text = start.astimezone(UTC).isoformat()
        stop_text = stop.astimezone(UTC).isoformat()
        query = f'''
from(bucket: "{self.config.bucket}")
  |> range(start: time(v: "{start_text}"), stop: time(v: "{stop_text}"))
  |> filter(fn: (r) => r._measurement == "pv_forecast")
  |> filter(fn: (r) => r.property == "{property_id}" and r.provider == "{provider}")
  |> filter(fn: (r) => r.role == "overnight" and r._field == "raw_energy_kwh")
  |> count()
'''
        values = self._values(query)
        return int(values[-1]) if values else 0

    def forecast_total(
        self, property_id: str, provider: str, start: datetime, stop: datetime
    ) -> float:
        start_text = start.astimezone(UTC).isoformat()
        stop_text = stop.astimezone(UTC).isoformat()
        query = f'''
from(bucket: "{self.config.bucket}")
  |> range(start: time(v: "{start_text}"), stop: time(v: "{stop_text}"))
  |> filter(fn: (r) => r._measurement == "pv_forecast")
  |> filter(fn: (r) => r.property == "{property_id}" and r.provider == "{provider}")
  |> filter(fn: (r) => r.role == "overnight" and r._field == "raw_energy_kwh")
  |> sum()
'''
        values = self._values(query)
        return float(values[-1]) if values else 0.0

    def recent_daily_ratios(self, property_id: str, days: int = 60) -> list[float]:
        query = f'''
from(bucket: "{self.config.bucket}")
  |> range(start: -{days}d)
  |> filter(fn: (r) => r._measurement == "pv_daily")
  |> filter(fn: (r) => r.property == "{property_id}")
  |> filter(fn: (r) => r._field == "daily_ratio")
'''
        return [float(value) for value in self._values(query)]

    def day_totals(
        self,
        property_id: str,
        start: datetime,
        stop: datetime,
        expected_interval_seconds: int = 300,
        forecast_provider: str = "open_meteo",
    ) -> tuple[float, float, float]:
        start_text = start.astimezone(UTC).isoformat()
        stop_text = stop.astimezone(UTC).isoformat()
        actual_query = f'''
from(bucket: "{self.config.bucket}")
  |> range(start: time(v: "{start_text}"), stop: time(v: "{stop_text}"))
  |> filter(fn: (r) => r._measurement == "energy_telemetry")
  |> filter(fn: (r) => r.property == "{property_id}")
  |> filter(fn: (r) => r._field == "daily_pv_kwh")
  |> last()
'''
        count_query = f'''
from(bucket: "{self.config.bucket}")
  |> range(start: time(v: "{start_text}"), stop: time(v: "{stop_text}"))
  |> filter(fn: (r) => r._measurement == "energy_telemetry")
  |> filter(fn: (r) => r.property == "{property_id}")
  |> filter(fn: (r) => r._field == "daily_pv_kwh")
  |> count()
'''
        utc_duration = stop.astimezone(UTC) - start.astimezone(UTC)
        expected_forecast_points = int(utc_duration.total_seconds() / 3600)
        snapshot = self.complete_forecast_snapshot(
            property_id,
            forecast_provider,
            start,
            stop,
            expected_forecast_points,
        )
        actual = self._values(actual_query)
        count = self._values(count_query)
        expected_points = max(
            1, int(utc_duration.total_seconds() / expected_interval_seconds)
        )
        completeness = min(1.0, (int(count[-1]) if count else 0) / expected_points)
        return (
            snapshot.raw_energy_kwh if snapshot else 0.0,
            float(actual[-1]) if actual else 0.0,
            completeness,
        )

    def _values(self, query: str) -> list[object]:
        return [record.get_value() for record in self._records(query)]

    def _records(self, query: str) -> list[object]:
        tables = self.client.query_api().query(query=query, org=self.config.org)
        return [record for table in tables for record in table.records]
