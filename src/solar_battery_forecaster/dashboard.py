from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from solar_battery_forecaster.actual_energy import evaluate_actual_energy
from solar_battery_forecaster.config import InfluxConfig, PropertyConfig

PROPERTY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


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
        return {"start": self.start.isoformat(), "stop": self.stop.isoformat()}


def local_day_bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def _instant(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return result.astimezone(UTC)


class InfluxDashboardRepository:
    """Read-only views: a recommendation is displayed only with its complete plan."""

    def __init__(
        self, influx: InfluxConfig, client: Any | None = None, expected_interval_seconds: int = 300
    ) -> None:
        if client is None:
            from influxdb_client import InfluxDBClient

            client = InfluxDBClient(url=influx.url, token=influx.token, org=influx.org)
        self.client = client
        self.influx = influx
        self.expected_interval_seconds = expected_interval_seconds

    def close(self) -> None:
        self.client.close()

    def _query(self, query: str) -> list[Any]:
        return [
            record
            for table in self.client.query_api().query(query=query, org=self.influx.org)
            for record in table.records
        ]

    def _base(self, measurement: str, property_id: str, start: datetime, stop: datetime) -> str:
        if not PROPERTY_ID_PATTERN.fullmatch(property_id):
            raise ValueError("invalid property ID")
        bucket = {
            "energy_telemetry": self.influx.telemetry_bucket,
            "electricity_tariff": self.influx.tariff_bucket,
        }.get(measurement, self.influx.planning_bucket)
        return f'''from(bucket: "{bucket}")
  |> range(start: time(v: "{start.astimezone(UTC).isoformat()}"),
           stop: time(v: "{stop.astimezone(UTC).isoformat()}"))
  |> filter(fn: (r) => r._measurement == "{measurement}" and r.property == "{property_id}")
'''

    def _points(
        self,
        measurement: str,
        field: str,
        property_id: str,
        start: datetime,
        stop: datetime,
        aggregate: str | None = None,
    ) -> list[CurvePoint]:
        query = self._base(measurement, property_id, start, stop)
        query += f'  |> filter(fn: (r) => r._field == "{field}")\n'
        if aggregate:
            query += f"  |> aggregateWindow(every: {aggregate}, fn: mean, createEmpty: false)\n"
        return sorted(
            [
                CurvePoint(record.get_time(), float(record.get_value()))
                for record in self._query(query)
                if record.get_value() is not None
                and (field == "daily_pv_kwh" or math.isfinite(float(record.get_value())))
            ],
            key=lambda point: point.at,
        )

    def _forecast(
        self, property_id: str, provider: str, start: datetime, stop: datetime
    ) -> tuple[str | None, list[Any]]:
        if not PROVIDER_PATTERN.fullmatch(provider):
            raise ValueError("invalid forecast provider")
        query = (
            self._base("pv_forecast", property_id, start, stop)
            + f'''
  |> filter(fn: (r) => r.provider == "{provider}" and r.role == "overnight")
  |> filter(fn: (r) => r._field == "conservative_power_kw" or
      r._field == "conservative_energy_kwh" or r._field == "issued_at_epoch")
  |> pivot(rowKey: ["_time", "snapshot"], columnKey: ["_field"], valueColumn: "_value")
'''
        )
        grouped: dict[str, list[Any]] = {}
        for record in self._query(query):
            grouped.setdefault(str(record.values.get("snapshot", "")), []).append(record)
        expected = [
            start + timedelta(hours=i) for i in range(int((stop - start).total_seconds() / 3600))
        ]
        complete = []
        for snapshot, records in grouped.items():
            records.sort(key=lambda row: row.get_time())
            try:
                issued = {float(row.values["issued_at_epoch"]) for row in records}
                valid = all(
                    math.isfinite(float(row.values[field])) and float(row.values[field]) >= 0
                    for row in records
                    for field in ("conservative_power_kw", "conservative_energy_kwh")
                )
                if (
                    snapshot
                    and valid
                    and len(issued) == 1
                    and math.isfinite(next(iter(issued)))
                    and [row.get_time() for row in records] == expected
                ):
                    complete.append((next(iter(issued)), snapshot, records))
            except (KeyError, TypeError, ValueError):
                continue
        if not complete:
            return None, []
        _, snapshot, records = max(complete, key=lambda item: (item[0], item[1]))
        return snapshot, records

    def _forecast_points(
        self, field: str, property_id: str, provider: str, start: datetime, stop: datetime
    ) -> list[CurvePoint]:
        _, records = self._forecast(property_id, provider, start, stop)
        return [CurvePoint(row.get_time(), float(row.values[field])) for row in records]

    def _decision_plan(
        self, prop: PropertyConfig, day: date, snapshot: str | None, start: datetime, stop: datetime
    ) -> tuple[dict[str, Any] | None, list[CurvePoint]]:
        if snapshot is None:
            return None, []
        query = (
            self._base("battery_decision", prop.id, start - timedelta(days=2), stop)
            + """
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
"""
        )
        matching = [
            r
            for r in self._query(query)
            if r.values.get("property") == prop.id
            and r.values.get("forecast_day") == day.isoformat()
            and r.values.get("snapshot") == snapshot
            and r.values.get("forecast_snapshot_id") == snapshot
        ]
        if not matching:
            return None, []
        decision = max(matching, key=lambda row: row.get_time()).values
        try:
            identity = decision["decision"]
            if not identity or identity != decision["decision_id"] or decision["plan_version"] != 1:
                return None, []
            plan_start, plan_stop = (
                _instant(decision["plan_start"]),
                _instant(decision["plan_stop"]),
            )
            target_at = _instant(decision["target_soc_at"])
            count = int(decision["plan_point_count"])
            if (
                not 2 <= count <= 10000
                or count != decision["plan_point_count"]
                or not start - timedelta(days=2) <= plan_start <= start < stop <= plan_stop
                or plan_stop > stop + timedelta(days=1)
                or not plan_start <= target_at <= plan_stop
                or _instant(decision["soc_observed_at"]) != plan_start
            ):
                return None, []
            for field in (
                "current_soc_percent",
                "target_soc_percent",
                "grid_charge_kwh",
                "correction_factor",
                "capacity_shortfall_kwh",
                "window_shortfall_kwh",
                "unavoidable_grid_import_kwh",
                "reserve_shortfall_kwh",
            ):
                if not math.isfinite(float(decision[field])):
                    return None, []
            if decision.get("estimated_charge_cost_pence") is not None and not math.isfinite(
                float(decision["estimated_charge_cost_pence"])
            ):
                return None, []
            query = (
                self._base("battery_plan", prop.id, plan_start, plan_stop + timedelta(seconds=1))
                + """
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
"""
            )
            rows = [
                r
                for r in self._query(query)
                if r.values.get("property") == prop.id
                and r.values.get("forecast_day") == day.isoformat()
                and r.values.get("decision") == identity
                and r.values.get("snapshot") == snapshot
            ]
            rows.sort(key=lambda r: r.get_time())
            times = [r.get_time() for r in rows]
            if (
                len(rows) != count
                or len(set(times)) != count
                or times[0] != plan_start
                or times[-1] != plan_stop
                or target_at not in times
            ):
                return None, []
            for row in rows:
                soc, stored = float(row.values["soc_percent"]), float(row.values["stored_kwh"])
                if (
                    not math.isfinite(soc)
                    or not math.isfinite(stored)
                    or not 0 <= soc <= 100
                    or not 0 <= stored <= prop.battery.usable_capacity_kwh
                ):
                    return None, []
            if not math.isclose(
                float(rows[0].values["soc_percent"]),
                float(decision["current_soc_percent"]),
                abs_tol=0.001,
            ):
                return None, []
            target = rows[times.index(target_at)]
            if not math.isclose(
                float(target.values["soc_percent"]),
                float(decision["target_soc_percent"]),
                abs_tol=0.001,
            ):
                return None, []
            return decision, [
                CurvePoint(r.get_time(), float(r.values["soc_percent"])) for r in rows
            ]
        except (KeyError, ValueError, TypeError, OverflowError):
            return None, []

    def _tariff_windows(
        self, property_id: str, start: datetime, stop: datetime
    ) -> list[TariffWindow]:
        query = (
            self._base("electricity_tariff", property_id, start - timedelta(days=2), stop)
            + """
  |> filter(fn: (r) => r._field == "is_cheap" or r._field == "interval_minutes")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> filter(fn: (r) => r.is_cheap == true)
"""
        )
        result = []
        for record in self._query(query):
            end = record.get_time() + timedelta(minutes=float(record.values["interval_minutes"]))
            if end > start:
                result.append(TariffWindow(max(start, record.get_time()), min(stop, end)))
        return result

    def curve(self, prop: PropertyConfig, day: date) -> dict[str, Any]:
        start, stop = local_day_bounds(day, prop.timezone)
        snapshot, forecast = self._forecast(prop.id, prop.forecast.adapter, start, stop)
        decision, planned = self._decision_plan(prop, day, snapshot, start, stop)
        # Raw observation timestamps preserve age and gaps; sparse means never imply coverage.
        actual_power = self._points("energy_telemetry", "pv_power_kw", prop.id, start, stop)
        actual_soc = self._points("energy_telemetry", "battery_soc_percent", prop.id, start, stop)
        counters = self._points("energy_telemetry", "daily_pv_kwh", prop.id, start, stop)
        now = datetime.now(UTC)
        actual = evaluate_actual_energy(
            [(p.at, p.value) for p in counters],
            start,
            stop,
            self.expected_interval_seconds,
            now=now,
        )

        def value(field: str) -> Any:
            return decision.get(field) if decision else None

        return {
            "property_id": prop.id,
            "local_date": day.isoformat(),
            "timezone": prop.timezone,
            "generated_at": now.isoformat(),
            "window": {"start": start.isoformat(), "stop": stop.isoformat()},
            "limits": {"minimum_soc_percent": prop.battery.minimum_soc_percent},
            "telemetry_expected_interval_seconds": self.expected_interval_seconds,
            "plan": {
                "available": decision is not None,
                "decision_id": value("decision_id"),
                "forecast_snapshot_id": snapshot,
                "start": value("plan_start"),
                "stop": value("plan_stop"),
                "target_soc_at": value("target_soc_at"),
            },
            "assumptions": {
                "soc_projection": (
                    "Configured daily load is spread evenly over elapsed time."
                    if value("load_model") == "uniform_elapsed"
                    else "Uses the supplied interval load profile."
                    if value("load_model") == "supplied_intervals"
                    else "Plan unavailable"
                ),
                "starting_soc_percent": value("current_soc_percent"),
            },
            "summary": {
                "forecast_generation_kwh": round(
                    sum(float(r.values["conservative_energy_kwh"]) for r in forecast), 2
                )
                if forecast
                else None,
                "actual_generation_kwh": actual.energy_kwh,
                "actual_energy_quality": actual.quality,
                "actual_energy_reason_codes": actual.reason_codes,
                "actual_energy_coverage_fraction": actual.coverage_fraction,
                "actual_energy_calibration_eligible": actual.calibration_eligible,
                "latest_observed_at": actual_soc[-1].at.isoformat() if actual_soc else None,
                "latest_actual_soc_percent": actual_soc[-1].value if actual_soc else None,
                "target_soc_percent": value("target_soc_percent"),
                "recommended_grid_charge_kwh": value("grid_charge_kwh"),
                "estimated_charge_cost_pence": value("estimated_charge_cost_pence"),
                "correction_factor": value("correction_factor"),
                "capacity_shortfall_kwh": value("capacity_shortfall_kwh"),
                "window_shortfall_kwh": value("window_shortfall_kwh"),
                "unavoidable_grid_import_kwh": value("unavoidable_grid_import_kwh"),
                "reserve_shortfall_kwh": value("reserve_shortfall_kwh"),
                "forecast_issued_at": datetime.fromtimestamp(
                    float(forecast[0].values["issued_at_epoch"]), UTC
                ).isoformat()
                if forecast
                else None,
            },
            "series": {
                "forecast_generation_kw": [
                    CurvePoint(r.get_time(), float(r.values["conservative_power_kw"])).as_dict()
                    for r in forecast
                ],
                "actual_generation_kw": [p.as_dict() for p in actual_power],
                "planned_soc_percent": [p.as_dict() for p in planned],
                "actual_soc_percent": [p.as_dict() for p in actual_soc],
                "cheap_rate_intervals": [
                    w.as_dict() for w in self._tariff_windows(prop.id, start, stop)
                ],
            },
        }
