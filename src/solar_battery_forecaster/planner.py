from __future__ import annotations

import statistics
from datetime import UTC, date, datetime

from solar_battery_forecaster.config import BatteryConfig
from solar_battery_forecaster.models import BatteryDecision


def correction_factor(ratios: list[float], default: float = 1.0) -> float:
    """Return a robust factor while ignoring implausible and near-zero-day ratios."""
    valid = [value for value in ratios if 0.25 <= value <= 2.0]
    if len(valid) < 7:
        return default
    return min(1.5, max(0.5, statistics.median(valid[-60:])))


def make_decision(
    *,
    battery: BatteryConfig,
    current_soc_percent: float,
    raw_solar_kwh: float,
    factor: float,
    conservative_multiplier: float,
    expected_load_kwh: float,
    forecast_day: date,
    forecast_snapshot_id: str,
    forecast_issued_at: datetime,
    soc_observed_at: datetime,
    tariff_coverage_start: datetime,
    tariff_coverage_stop: datetime,
    tariff_coverage_hours: float,
    cheap_duration_hours: float | None = None,
    cheap_rate_average_pence: float | None = None,
) -> BatteryDecision:
    corrected = raw_solar_kwh * factor
    conservative = corrected * conservative_multiplier
    required_stored = min(
        battery.usable_capacity_kwh,
        max(0.0, expected_load_kwh + battery.reserve_kwh - conservative),
    )
    soc_span = battery.maximum_soc_percent - battery.minimum_soc_percent
    stored_now = max(
        0.0,
        (current_soc_percent - battery.minimum_soc_percent)
        / soc_span
        * battery.usable_capacity_kwh,
    )
    requested_grid_charge = max(0.0, required_stored - stored_now) / battery.charge_efficiency
    if cheap_duration_hours is None:
        grid_charge = requested_grid_charge
        duration = 0.0
    else:
        duration = max(0.0, cheap_duration_hours)
        grid_charge = min(requested_grid_charge, battery.max_charge_power_kw * duration)
    target_stored = required_stored
    if grid_charge + 1e-9 < requested_grid_charge:
        target_stored = min(
            battery.usable_capacity_kwh,
            stored_now + grid_charge * battery.charge_efficiency,
        )
    target_soc = battery.minimum_soc_percent + (
        target_stored / battery.usable_capacity_kwh * soc_span
    )
    target_soc = min(battery.maximum_soc_percent, max(battery.minimum_soc_percent, target_soc))
    limited = grid_charge + 1e-9 < requested_grid_charge
    return BatteryDecision(
        created_at=datetime.now(UTC),
        current_soc_percent=current_soc_percent,
        target_soc_percent=target_soc,
        grid_charge_kwh=grid_charge,
        raw_solar_kwh=raw_solar_kwh,
        corrected_solar_kwh=corrected,
        conservative_solar_kwh=conservative,
        expected_load_kwh=expected_load_kwh,
        reserve_kwh=battery.reserve_kwh,
        correction_factor=factor,
        forecast_day=forecast_day,
        forecast_snapshot_id=forecast_snapshot_id,
        forecast_issued_at=forecast_issued_at,
        soc_observed_at=soc_observed_at,
        tariff_coverage_start=tariff_coverage_start,
        tariff_coverage_stop=tariff_coverage_stop,
        tariff_coverage_hours=tariff_coverage_hours,
        reason=(
            "recommendation_only: cheap window limits requested charge"
            if limited
            else "recommendation_only: conservative solar minus expected load and reserve"
        ),
        cheap_duration_hours=duration,
        cheap_rate_average_pence=cheap_rate_average_pence,
        estimated_charge_cost_pence=(
            grid_charge * cheap_rate_average_pence
            if cheap_rate_average_pence is not None
            else None
        ),
        charge_limited_by_window=limited,
    )
