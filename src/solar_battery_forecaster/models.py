from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime


def forecast_snapshot_id(issued_at: datetime) -> str:
    return issued_at.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class ForecastInterval:
    start: datetime
    end: datetime
    energy_kwh: float
    power_kw: float
    issued_at: datetime
    provider: str
    corrected_energy_kwh: float | None = None
    conservative_energy_kwh: float | None = None


@dataclass(frozen=True)
class TariffInterval:
    start: datetime
    end: datetime
    price_pence_per_kwh: float
    is_cheap: bool


@dataclass(frozen=True)
class StoredTariffs:
    intervals: list[TariffInterval]
    retrieved_at: datetime


@dataclass(frozen=True)
class Telemetry:
    observed_at: datetime
    pv_power_kw: float | None = None
    grid_power_kw: float | None = None
    battery_power_kw: float | None = None
    load_power_kw: float | None = None
    battery_soc_percent: float | None = None
    daily_pv_kwh: float | None = None
    lifetime_pv_kwh: float | None = None
    inverter_online: bool = True


@dataclass(frozen=True)
class PlanPoint:
    at: datetime
    soc_percent: float
    stored_kwh: float


@dataclass(frozen=True)
class BatteryDecision:
    created_at: datetime
    current_soc_percent: float
    target_soc_percent: float
    grid_charge_kwh: float
    raw_solar_kwh: float
    corrected_solar_kwh: float
    conservative_solar_kwh: float
    expected_load_kwh: float
    reserve_kwh: float
    correction_factor: float
    forecast_day: date
    forecast_snapshot_id: str
    forecast_issued_at: datetime
    soc_observed_at: datetime
    tariff_coverage_start: datetime
    tariff_coverage_stop: datetime
    tariff_coverage_hours: float
    reason: str
    cheap_duration_hours: float = 0.0
    cheap_rate_average_pence: float | None = None
    estimated_charge_cost_pence: float | None = None
    charge_limited_by_window: bool = False
    decision_id: str = ""
    plan_version: int = 0
    plan_start: datetime | None = None
    plan_stop: datetime | None = None
    target_soc_at: datetime | None = None
    plan_points: tuple[PlanPoint, ...] = ()
    capacity_shortfall_kwh: float = 0.0
    window_shortfall_kwh: float = 0.0
    unavoidable_grid_import_kwh: float = 0.0
    reserve_shortfall_kwh: float = 0.0
    horizon_grid_charge_kwh: float = 0.0
    load_model: str = "uniform_elapsed"


@dataclass(frozen=True)
class ForecastSnapshot:
    provider: str
    snapshot_id: str
    issued_at: datetime
    point_count: int
    raw_energy_kwh: float
    correction_factor: float
    intervals: tuple[ForecastInterval, ...] = ()
