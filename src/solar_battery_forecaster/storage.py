from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Literal, TypeAlias

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from solar_battery_forecaster.actual_energy import evaluate_actual_energy
from solar_battery_forecaster.config import InfluxConfig, OutboxConfig
from solar_battery_forecaster.models import (
    BatteryDecision,
    ForecastInterval,
    ForecastSnapshot,
    StoredTariffs,
    TariffInterval,
    Telemetry,
    forecast_snapshot_id,
)
from solar_battery_forecaster.outbox import DurableOutbox, OutboxFullError, OutboxStatus

LOGGER = logging.getLogger(__name__)
DeliveryDisposition: TypeAlias = Literal["direct", "buffered", "ignored"]


class InfluxStore:
    def __init__(
        self,
        config: InfluxConfig,
        outbox_config: OutboxConfig | None = None,
        worker: str | None = None,
    ) -> None:
        self.config = config
        self.outbox = None
        if outbox_config is not None and worker is not None:
            state_directory = getattr(outbox_config, "state_directory", None)
            if state_directory is None:
                raise ValueError("writer requires an outbox state directory")
            self.outbox = DurableOutbox(state_directory, outbox_config, worker)
        self._last_delivery_status = self.outbox.status() if self.outbox is not None else None
        self.client = InfluxDBClient(url=config.url, token=config.token, org=config.org)
        self.writer = self.client.write_api(write_options=SYNCHRONOUS)

    def close(self) -> None:
        try:
            if self.outbox is not None:
                self.outbox.close()
        finally:
            self.client.close()

    def ping(self) -> bool:
        return bool(self.client.ping())

    def _write(
        self,
        bucket: str,
        points: Iterable[Point],
        *,
        property_id: str,
        logical_kind: str,
        logical_key: str,
        min_timestamp: datetime,
        max_timestamp: datetime,
        metadata: dict[str, object] | None = None,
    ) -> DeliveryDisposition:
        records = list(points)
        if not records:
            return "ignored"
        if self.outbox is None:
            raise RuntimeError("InfluxDB writes require a durable outbox")
        payload = "\n".join(point.to_line_protocol() for point in records).encode("utf-8")
        if not payload or len(payload) > self.outbox.config.max_record_bytes:
            raise OutboxFullError("outbox record reserve is invalid")
        self.outbox.admit_collection(property_id, len(payload))
        enqueue_arguments = {
            "property_id": property_id,
            "org": self.config.org,
            "bucket": bucket,
            "logical_kind": logical_kind,
            "logical_key": logical_key,
            "min_timestamp": min_timestamp,
            "max_timestamp": max_timestamp,
            "payload": payload,
            "metadata": metadata,
        }
        if self.outbox.can_attempt_direct(property_id):
            try:
                self._deliver(bucket, self.config.org, payload.decode("utf-8"))
            except Exception as exc:
                event_id = self.outbox.enqueue(**enqueue_arguments)
                self.outbox.record_failure(event_id, exc)
            else:
                try:
                    self.outbox.record_direct_success()
                except Exception as exc:
                    LOGGER.error(
                        "delivery counter update failed for %s (%s)",
                        self.outbox.worker,
                        type(exc).__name__,
                    )
                return "direct"
        else:
            self.outbox.enqueue(**enqueue_arguments)
        status = self.outbox.status()
        self._last_delivery_status = status
        LOGGER.warning(
            "InfluxDB delivery deferred for %s: records=%d bytes=%d paused=%s",
            self.outbox.worker,
            status.pending_records,
            status.pending_bytes,
            status.delivery_paused,
        )
        return "buffered"

    def replay(self, force: bool = False) -> int:
        if self.outbox is None:
            return 0
        delivered = self.outbox.drain(self._deliver, force=force)
        self._last_delivery_status = self.outbox.status()
        return delivered

    def _deliver(self, bucket: str, org: str, payload: str) -> None:
        self.writer.write(
            bucket=bucket,
            org=org,
            record=payload,
            write_precision=WritePrecision.S,
        )

    def admit_collection(self, property_id: str | None = None) -> None:
        if self.outbox is None:
            raise RuntimeError("writer requires a durable outbox")
        self.outbox.admit_collection(property_id)

    def delivery_status(self) -> OutboxStatus | None:
        return self._last_delivery_status

    def has_undelivered(self) -> bool:
        status = self.delivery_status()
        return bool(
            status
            and (
                status.pending_records
                or status.quarantined_records
                or status.blocked_streams
                or status.delivery_paused
            )
        )

    def write_forecast(
        self,
        property_id: str,
        intervals: list[ForecastInterval],
        correction_factor: float,
        conservative_multiplier: float,
        *,
        forecast_day_start: datetime | None = None,
        forecast_day_stop: datetime | None = None,
        inverter_limit_kw: float | None = None,
    ) -> DeliveryDisposition:
        points = []
        corrected_by_start: dict[datetime, float] = {}
        for interval in intervals:
            duration = (
                interval.end.astimezone(UTC) - interval.start.astimezone(UTC)
            ).total_seconds() / 3600
            corrected = min(
                interval.energy_kwh * correction_factor,
                inverter_limit_kw * duration if inverter_limit_kw else float("inf"),
            )
            corrected_by_start[interval.start] = corrected
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
                .field("corrected_power_kw", corrected / duration)
                .field(
                    "conservative_power_kw",
                    corrected / duration * conservative_multiplier,
                )
                .field("correction_factor", correction_factor)
                .field(
                    "interval_minutes",
                    int((interval.end - interval.start).total_seconds() / 60),
                )
                .field("issued_at_epoch", int(interval.issued_at.timestamp()))
                .time(interval.start, WritePrecision.S)
            )
        if not intervals:
            return "ignored"
        day_start = forecast_day_start or intervals[0].start
        day_stop = forecast_day_stop or intervals[-1].end
        day_intervals = [item for item in intervals if day_start <= item.start < day_stop]
        snapshot_id = forecast_snapshot_id(intervals[0].issued_at)
        return self._write(
            self.config.planning_bucket,
            points,
            property_id=property_id,
            logical_kind="forecast_snapshot",
            logical_key=f"{property_id}:{intervals[0].provider}:{day_start.date()}:{snapshot_id}",
            min_timestamp=intervals[0].start,
            max_timestamp=intervals[-1].end,
            metadata={
                "provider": intervals[0].provider,
                "snapshot_id": snapshot_id,
                "issued_at": intervals[0].issued_at.astimezone(UTC).isoformat(),
                "point_count": len(day_intervals),
                "raw_energy_kwh": sum(item.energy_kwh for item in day_intervals),
                "correction_factor": correction_factor,
                "forecast_start": day_start.astimezone(UTC).isoformat(),
                "forecast_stop": day_stop.astimezone(UTC).isoformat(),
                "intervals": [
                    {
                        "start": item.start.astimezone(UTC).isoformat(),
                        "end": item.end.astimezone(UTC).isoformat(),
                        "energy_kwh": item.energy_kwh,
                        "power_kw": item.power_kw,
                        "issued_at": item.issued_at.astimezone(UTC).isoformat(),
                        "provider": item.provider,
                        "corrected_energy_kwh": corrected_by_start[item.start],
                        "conservative_energy_kwh": corrected_by_start[item.start]
                        * conservative_multiplier,
                    }
                    for item in intervals
                ],
            },
        )

    def write_telemetry(
        self, property_id: str, source: str, item: Telemetry
    ) -> DeliveryDisposition:
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
        return self._write(
            self.config.telemetry_bucket,
            [point.time(item.observed_at, WritePrecision.S)],
            property_id=property_id,
            logical_kind="telemetry",
            logical_key=f"{property_id}:{item.observed_at.astimezone(UTC).isoformat()}",
            min_timestamp=item.observed_at,
            max_timestamp=item.observed_at,
        )

    def write_tariffs(
        self, property_id: str, provider: str, intervals: list[TariffInterval]
    ) -> DeliveryDisposition:
        retrieved_at_epoch = int(datetime.now(UTC).timestamp())
        points = [
            Point("electricity_tariff")
            .tag("property", property_id)
            .tag("provider", provider)
            .field("price_pence_per_kwh", item.price_pence_per_kwh)
            .field("is_cheap", item.is_cheap)
            .field("interval_minutes", int((item.end - item.start).total_seconds() / 60))
            .field("retrieved_at_epoch", retrieved_at_epoch)
            .time(item.start, WritePrecision.S)
            for item in intervals
        ]
        if not intervals:
            return "ignored"
        return self._write(
            self.config.tariff_bucket,
            points,
            property_id=property_id,
            logical_kind="tariff_batch",
            logical_key=f"{property_id}:{provider}:{intervals[0].start.astimezone(UTC).isoformat()}",
            min_timestamp=intervals[0].start,
            max_timestamp=intervals[-1].end,
        )

    def write_decision(self, property_id: str, item: BatteryDecision) -> DeliveryDisposition:
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
            .field("forecast_day", item.forecast_day.isoformat())
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
            point = point.field("cheap_rate_average_pence", item.cheap_rate_average_pence)
        if item.estimated_charge_cost_pence is not None:
            point = point.field("estimated_charge_cost_pence", item.estimated_charge_cost_pence)
        points = [point]
        if item.plan_version:
            # Zero-energy plans must not introduce integer field types into series
            # whose nonzero energy is represented as a float by InfluxDB.
            for name in (
                "current_soc_percent",
                "target_soc_percent",
                "grid_charge_kwh",
                "raw_solar_kwh",
                "corrected_solar_kwh",
                "conservative_solar_kwh",
                "expected_load_kwh",
                "reserve_kwh",
                "correction_factor",
                "tariff_coverage_hours",
                "cheap_duration_hours",
            ):
                point.field(name, float(getattr(item, name)))
            if item.estimated_charge_cost_pence is not None:
                point.field("estimated_charge_cost_pence", float(item.estimated_charge_cost_pence))
            point.tag("decision", item.decision_id).tag("snapshot", item.forecast_snapshot_id)
            point.field("decision_id", item.decision_id)
            point.field("plan_version", item.plan_version)
            point.field("plan_point_count", len(item.plan_points))
            point.field("load_model", item.load_model)
            for name in ("plan_start", "plan_stop", "target_soc_at"):
                value = getattr(item, name)
                if value is not None:
                    point.field(name, value.astimezone(UTC).isoformat())
            for name in (
                "capacity_shortfall_kwh",
                "window_shortfall_kwh",
                "unavoidable_grid_import_kwh",
                "reserve_shortfall_kwh",
                "horizon_grid_charge_kwh",
            ):
                point.field(name, float(getattr(item, name)))
            points.extend(
                Point("battery_plan")
                .tag("property", property_id)
                .tag("forecast_day", item.forecast_day.isoformat())
                .tag("decision", item.decision_id)
                .tag("snapshot", item.forecast_snapshot_id)
                .field("soc_percent", float(position.soc_percent))
                .field("stored_kwh", float(position.stored_kwh))
                .time(position.at, WritePrecision.S)
                for position in item.plan_points
            )
        return self._write(
            self.config.planning_bucket,
            points,
            property_id=property_id,
            logical_kind="battery_decision",
            logical_key=f"{property_id}:{item.forecast_day.isoformat()}",
            min_timestamp=min(item.created_at, item.plan_start or item.created_at),
            max_timestamp=max(item.created_at, item.plan_stop or item.created_at),
        )

    def tariff_intervals(
        self, property_id: str, start: datetime, stop: datetime
    ) -> StoredTariffs | None:
        query_start = start.astimezone(UTC) - timedelta(hours=2)
        start_text = query_start.isoformat()
        stop_text = stop.astimezone(UTC).isoformat()
        query = f'''
from(bucket: "{self.config.tariff_bucket}")
  |> range(start: time(v: "{start_text}"), stop: time(v: "{stop_text}"))
  |> filter(fn: (r) => r._measurement == "electricity_tariff")
  |> filter(fn: (r) => r.property == "{property_id}")
  |> filter(fn: (r) => r._field == "price_pence_per_kwh" or r._field == "is_cheap" or
      r._field == "interval_minutes" or r._field == "retrieved_at_epoch")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
        records = self._records(query)
        if not records:
            return None
        intervals = [
            TariffInterval(
                start=record.get_time(),
                end=record.get_time() + timedelta(minutes=float(record.values["interval_minutes"])),
                price_pence_per_kwh=float(record.values["price_pence_per_kwh"]),
                is_cheap=bool(record.values["is_cheap"]),
            )
            for record in records
        ]
        retrieved_at = datetime.fromtimestamp(
            min(float(record.values["retrieved_at_epoch"]) for record in records), UTC
        )
        return StoredTariffs(intervals=intervals, retrieved_at=retrieved_at)

    def decision_exists(self, property_id: str, forecast_day: date) -> bool:
        logical_key = f"{property_id}:{forecast_day.isoformat()}"
        outbox = getattr(self, "outbox", None)
        if outbox is not None and outbox.pending("battery_decision", logical_key):
            return True
        query = f'''
from(bucket: "{self.config.planning_bucket}")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "battery_decision")
  |> filter(fn: (r) => r.property == "{property_id}")
  |> filter(fn: (r) => r._field == "forecast_day" and r._value == "{forecast_day.isoformat()}")
  |> limit(n: 1)
'''
        return bool(self._records(query))

    def daily_result_exists(self, property_id: str, day: date) -> bool:
        logical_key = f"{property_id}:{day.isoformat()}"
        outbox = getattr(self, "outbox", None)
        if outbox is not None and outbox.pending("pv_daily", logical_key):
            return True
        start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        stop = start + timedelta(days=1)
        query = f'''
from(bucket: "{self.config.planning_bucket}")
  |> range(start: time(v: "{start.isoformat()}"), stop: time(v: "{stop.isoformat()}"))
  |> filter(fn: (r) => r._measurement == "pv_daily")
  |> filter(fn: (r) => r.property == "{property_id}")
  |> limit(n: 1)
'''
        return bool(self._records(query))

    def write_daily_result(
        self,
        property_id: str,
        day: date,
        forecast_kwh: float,
        actual_kwh: float,
        ratio: float,
        factor_after_update: float,
    ) -> DeliveryDisposition:
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
        return self._write(
            self.config.planning_bucket,
            [point],
            property_id=property_id,
            logical_kind="pv_daily",
            logical_key=f"{property_id}:{day.isoformat()}",
            min_timestamp=timestamp,
            max_timestamp=timestamp,
        )

    def latest_soc(self, property_id: str) -> float | None:
        reading = self.latest_soc_reading(property_id)
        return reading[0] if reading else None

    def latest_soc_reading(self, property_id: str) -> tuple[float, datetime] | None:
        query = f'''
from(bucket: "{self.config.telemetry_bucket}")
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
        # Retrieve the same snapshot's preceding bridge as well as its forecast day.
        start_text = (start.astimezone(UTC) - timedelta(days=1)).isoformat()
        stop_text = stop.astimezone(UTC).isoformat()
        query = f'''
from(bucket: "{self.config.planning_bucket}")
  |> range(start: time(v: "{start_text}"), stop: time(v: "{stop_text}"))
  |> filter(fn: (r) => r._measurement == "pv_forecast")
  |> filter(fn: (r) => r.property == "{property_id}" and r.provider == "{provider}")
  |> filter(fn: (r) => r.role == "overnight")
  |> filter(fn: (r) => r._field == "raw_energy_kwh" or
      r._field == "correction_factor" or r._field == "issued_at_epoch" or
      r._field == "interval_minutes" or r._field == "corrected_energy_kwh" or
      r._field == "conservative_energy_kwh")
  |> pivot(rowKey: ["_time", "snapshot"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
        grouped: dict[str, list[object]] = {}
        for record in self._records(query):
            snapshot_id = str(record.values.get("snapshot", ""))
            grouped.setdefault(snapshot_id, []).append(record)
        complete: list[ForecastSnapshot] = []
        for snapshot_id, records in grouped.items():
            day_records = [r for r in records if start <= r.get_time() < stop]
            if len(day_records) != expected_points:
                continue
            issued_epoch = float(day_records[0].values["issued_at_epoch"])
            issued = datetime.fromtimestamp(issued_epoch, UTC)
            intervals = tuple(
                ForecastInterval(
                    start=r.get_time(),
                    end=r.get_time()
                    + timedelta(minutes=float(r.values.get("interval_minutes", 60))),
                    energy_kwh=float(r.values["raw_energy_kwh"]),
                    power_kw=float(r.values["raw_energy_kwh"])
                    * 60
                    / float(r.values.get("interval_minutes", 60)),
                    issued_at=datetime.fromtimestamp(float(r.values["issued_at_epoch"]), UTC),
                    provider=provider,
                    corrected_energy_kwh=(
                        float(r.values["corrected_energy_kwh"])
                        if "corrected_energy_kwh" in r.values
                        else None
                    ),
                    conservative_energy_kwh=(
                        float(r.values["conservative_energy_kwh"])
                        if "conservative_energy_kwh" in r.values
                        else None
                    ),
                )
                for r in sorted(records, key=lambda r: r.get_time())
            )
            if any(item.issued_at != issued for item in intervals):
                continue
            if len({r.get_time() for r in day_records}) != expected_points:
                continue
            day_intervals = [item for item in intervals if start <= item.start < stop]
            if (
                day_intervals[0].start != start
                or day_intervals[-1].end != stop
                or any(
                    a.end != b.start for a, b in zip(day_intervals, day_intervals[1:], strict=False)
                )
                or any(
                    not math.isfinite(item.energy_kwh) or item.energy_kwh < 0
                    for item in day_intervals
                )
                or len({float(r.values["correction_factor"]) for r in records}) != 1
            ):
                continue
            complete.append(
                ForecastSnapshot(
                    provider=provider,
                    snapshot_id=snapshot_id,
                    issued_at=issued,
                    point_count=len(day_records),
                    raw_energy_kwh=sum(
                        float(record.values["raw_energy_kwh"]) for record in day_records
                    ),
                    correction_factor=float(day_records[0].values["correction_factor"]),
                    intervals=intervals,
                )
            )
        outbox = getattr(self, "outbox", None)
        if outbox is not None:
            complete.extend(
                item
                for item in outbox.pending_forecasts(property_id, provider, start, stop)
                if item.point_count == expected_points
            )
        return max(complete, key=lambda item: item.issued_at) if complete else None

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
from(bucket: "{self.config.planning_bucket}")
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
from(bucket: "{self.config.planning_bucket}")
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
from(bucket: "{self.config.planning_bucket}")
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
from(bucket: "{self.config.telemetry_bucket}")
  |> range(start: time(v: "{start_text}"), stop: time(v: "{stop_text}"))
  |> filter(fn: (r) => r._measurement == "energy_telemetry")
  |> filter(fn: (r) => r.property == "{property_id}")
  |> filter(fn: (r) => r._field == "daily_pv_kwh")
  |> keep(columns: ["_time", "_value"])
  |> sort(columns: ["_time"])
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
        samples = [(r.get_time(), float(r.get_value())) for r in self._records(actual_query)]
        actual = evaluate_actual_energy(samples, start, stop, expected_interval_seconds)
        return (
            snapshot.raw_energy_kwh if snapshot else 0.0,
            actual.energy_kwh if actual.energy_kwh is not None else -1.0,
            actual.coverage_fraction if actual.calibration_eligible else 0.0,
        )

    def _values(self, query: str) -> list[object]:
        return [record.get_value() for record in self._records(query)]

    def _records(self, query: str) -> list[object]:
        tables = self.client.query_api().query(query=query, org=self.config.org)
        return [record for table in tables for record in table.records]
